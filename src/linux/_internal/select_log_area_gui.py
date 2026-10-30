from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox
from ctypes import wintypes
from pathlib import Path

import cv2
import mss
import numpy as np

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"

REFERENCE_CAPTURE_WIDTH = 885
REFERENCE_CAPTURE_HEIGHT = 522
MIN_CAPTURE_RATIO = 0.70
MIN_RECOMMENDED_WIDTH = REFERENCE_CAPTURE_WIDTH * MIN_CAPTURE_RATIO
MIN_RECOMMENDED_HEIGHT = REFERENCE_CAPTURE_HEIGHT * MIN_CAPTURE_RATIO

if sys.platform.startswith("linux"):
    from linux_x11 import (
        find_ddo_window as platform_find_ddo_window,
        window_rect as platform_window_rect,
        focus_ddo as platform_focus_ddo,
    )

if os.name == "nt":
    user32 = ctypes.windll.user32
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    SW_RESTORE = 9
    FindWindowW = user32.FindWindowW
    FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    FindWindowW.restype = wintypes.HWND
    ShowWindow = user32.ShowWindow
    ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    ShowWindow.restype = wintypes.BOOL
    GetWindowRect = user32.GetWindowRect
    GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    GetWindowRect.restype = wintypes.BOOL
    SetForegroundWindow = user32.SetForegroundWindow
    SetForegroundWindow.argtypes = [wintypes.HWND]
    SetForegroundWindow.restype = wintypes.BOOL
else:
    SW_RESTORE = 9
    ShowWindow = None
    SetForegroundWindow = None


def find_ddo_window():
    if sys.platform.startswith("linux"):
        return platform_find_ddo_window(force=True)
    if os.name == "nt":
        hwnd = FindWindowW("Turbine Device Class", None)
        if hwnd:
            return hwnd
        hwnd = FindWindowW("Turbine Device Class Name", None)
        return hwnd if hwnd else None
    return None


def get_window_rect(hwnd):
    if sys.platform.startswith("linux"):
        return platform_window_rect(hwnd)
    if os.name == "nt":
        rect = wintypes.RECT()
        if not GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    return None


def focus_ddo():
    if sys.platform.startswith("linux"):
        return platform_focus_ddo()
    hwnd = find_ddo_window()
    if hwnd and os.name == "nt":
        try:
            ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.10)
            SetForegroundWindow(hwnd)
            return True
        except Exception:
            pass
    return False

def safe_imwrite(path: Path, image):
    path = Path(path)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"Could not encode screenshot: {path}")
    encoded.tofile(str(path))
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Could not save screenshot: {path}")


def choose_monitor(monitors, window_rect):
    if not window_rect:
        return monitors[1]

    left, top, right, bottom = window_rect
    cx = (left + right) // 2
    cy = (top + bottom) // 2

    for mon in monitors[1:]:
        if (
            mon["left"] <= cx < mon["left"] + mon["width"]
            and mon["top"] <= cy < mon["top"] + mon["height"]
        ):
            return mon

    return monitors[1]


class FrozenScreenshotSelector:
    def __init__(self, image_path: Path, monitor: dict):
        self.image_path = image_path
        self.monitor = monitor
        self.result = None
        self.start_x = None
        self.start_y = None
        self.rect_id = None

        self.root = tk.Tk()
        self.root.title("DDO Combat Analyzer - Select Combat Log Area")
        self.root.configure(bg="black")

        # Borderless window exactly over the monitor from which the screenshot came.
        self.root.overrideredirect(True)
        self.root.geometry(
            f"{monitor['width']}x{monitor['height']}"
            f"{monitor['left']:+d}{monitor['top']:+d}"
        )
        self.root.attributes("-topmost", True)

        self.photo = tk.PhotoImage(file=str(image_path))

        self.canvas = tk.Canvas(
            self.root,
            width=monitor["width"],
            height=monitor["height"],
            highlightthickness=0,
            bd=0,
            cursor="crosshair",
            bg="black",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        # Dark instruction panel on top of the frozen screenshot.
        panel = self.canvas.create_rectangle(
            20, 20, 650, 92,
            fill="#111111",
            outline="#dddddd",
            width=1,
        )
        self.canvas.create_text(
            36, 38,
            anchor="nw",
            fill="white",
            font=("Segoe UI", 14, "bold"),
            text="Select the DDO Combat Log area",
        )
        self.canvas.create_text(
            36, 66,
            anchor="nw",
            fill="#dddddd",
            font=("Segoe UI", 10),
            text="Drag a rectangle around ONLY the combat-log text.  ESC = cancel.",
        )

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Escape>", self.on_cancel)

        # Reassert foreground/topmost after the window has been mapped.
        self.root.after(100, self.force_foreground)
        self.root.after(400, self.force_foreground)

    def force_foreground(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            if os.name == "nt" and SetForegroundWindow is not None:
                hwnd = self.root.winfo_id()
                SetForegroundWindow(hwnd)
        except Exception:
            pass

    def on_press(self, event):
        self.start_x = max(0, min(event.x, self.monitor["width"] - 1))
        self.start_y = max(0, min(event.y, self.monitor["height"] - 1))

        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)

        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.start_x,
            self.start_y,
            outline="#ff3030",
            width=3,
        )

    def on_drag(self, event):
        if self.start_x is None or self.rect_id is None:
            return
        x = max(0, min(event.x, self.monitor["width"] - 1))
        y = max(0, min(event.y, self.monitor["height"] - 1))
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, x, y)

    def on_release(self, event):
        if self.start_x is None:
            return

        end_x = max(0, min(event.x, self.monitor["width"]))
        end_y = max(0, min(event.y, self.monitor["height"]))

        x1 = min(self.start_x, end_x)
        y1 = min(self.start_y, end_y)
        x2 = max(self.start_x, end_x)
        y2 = max(self.start_y, end_y)

        w = x2 - x1
        h = y2 - y1

        if w < 30 or h < 30:
            # Ignore accidental tiny clicks/selections.
            if self.rect_id is not None:
                self.canvas.delete(self.rect_id)
                self.rect_id = None
            self.start_x = None
            self.start_y = None
            return

        too_narrow = w < MIN_RECOMMENDED_WIDTH
        too_short = h < MIN_RECOMMENDED_HEIGHT
        if too_narrow or too_short:
            reasons = []
            if too_narrow:
                reasons.append(f"width {w} px (recommended at least {int(MIN_RECOMMENDED_WIDTH + 0.999)} px)")
            if too_short:
                reasons.append(f"height {h} px (recommended at least {int(MIN_RECOMMENDED_HEIGHT + 0.999)} px)")
            use_anyway = messagebox.askyesno(
                "Combat Log area may be too small",
                "The selected Combat Log area is smaller than 70% of the validated "
                "885 x 522 px reference area in at least one dimension.\n\n"
                + "Selected: " + f"{w} x {h} px\n"
                + "Too small: " + ", ".join(reasons) + "\n\n"
                + "A small area can reduce OCR/reconstruction reliability, especially "
                  "if text is clipped or too few log lines are visible.\n\n"
                + "Use this area anyway?",
                parent=self.root,
                icon=messagebox.WARNING,
                default=messagebox.NO,
            )
            if not use_anyway:
                if self.rect_id is not None:
                    self.canvas.delete(self.rect_id)
                    self.rect_id = None
                self.start_x = None
                self.start_y = None
                return

        self.result = (int(x1), int(y1), int(w), int(h))
        self.root.destroy()

    def on_cancel(self, _event=None):
        self.result = None
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


def main():
    hwnd = find_ddo_window()
    if not hwnd:
        print("ERROR: DDO window was not found.")
        raise SystemExit(2)

    # Bring DDO forward so the screenshot contains the actual game/log.
    focus_ddo()
    time.sleep(1.25)

    rect = get_window_rect(hwnd)

    with mss.MSS() as sct:
        mon = choose_monitor(sct.monitors, rect)
        shot = np.array(sct.grab(mon))
        image = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)

    screenshot_path = HERE / "last_selector_screenshot.png"
    safe_imwrite(screenshot_path, image)

    # Keep DDO visible behind the frozen selector. This avoids an unnecessary
    # minimize/restore flash while still letting the selector show a stable image.
    try:
        selector = FrozenScreenshotSelector(screenshot_path, mon)
        result = selector.run()

        if not result:
            print("CANCELLED")
            raise SystemExit(1)

        x, y, w, h = result

        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        data["capture"] = {
            "left": int(mon["left"] + x),
            "top": int(mon["top"] + y),
            "width": int(w),
            "height": int(h),
        }
        data["capture_configured"] = True
        CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")

        print("OK")
        print(json.dumps(data["capture"]))

    finally:
        # Return focus to DDO as the selector closes. During first-run setup the
        # parent immediately launches overlay placement on top of DDO.
        try:
            focus_ddo()
        except Exception:
            pass


if __name__ == "__main__":
    main()
