from __future__ import annotations

import bisect
import csv
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"

from cpu_utils import physical_core_count, resolve_indra_threads

if sys.platform.startswith('linux'):
    from linux_x11 import (
        LinuxHotkeyManager, find_ddo_window as linux_find_ddo_window,
        is_ddo_foreground as linux_is_ddo_foreground, focus_ddo as linux_focus_ddo,
        linux_beep,
    )

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SW_RESTORE = 9

# ----------------------------- helpers -------------------------------------

def dknum(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def find_exe(name):
    p = shutil.which(name)
    if p:
        return p
    return None


def find_ffmpeg():
    cfg = {}
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        pass
    configured = str(cfg.get("ffmpeg_path", "")).strip()
    if configured and Path(configured).exists():
        return configured

    for p in [HERE / "ffmpeg.exe", HERE / "ffmpeg" / "bin" / "ffmpeg.exe"]:
        if p.exists():
            return str(p)

    p = shutil.which("ffmpeg")
    if p:
        return p

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if packages.exists():
            try:
                for root in packages.glob("Gyan.FFmpeg*"):
                    for exe in root.rglob("ffmpeg.exe"):
                        if exe.exists():
                            return str(exe)
            except Exception:
                pass
    return None



def dependency_status():
    modules = {
        "mss": "mss",
        "OpenCV": "cv2",
        "NumPy": "numpy",
        "Indra OCR": "indra_ocr",
    }
    if sys.platform.startswith("linux"):
        modules["python-xlib"] = "Xlib"
    missing = [label for label, module in modules.items() if importlib.util.find_spec(module) is None]
    return {
        "missing_python": missing,
        "ffmpeg": find_ffmpeg(),
    }


def find_ddo_window():
    if sys.platform.startswith("linux"):
        return linux_find_ddo_window()
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("Turbine Device Class", None)
        if not hwnd:
            hwnd = user32.FindWindowW("Turbine Device Class Name", None)
        return hwnd if hwnd else None
    except Exception:
        return None


def is_ddo_foreground():
    if sys.platform.startswith("linux"):
        return linux_is_ddo_foreground()
    hwnd = find_ddo_window()
    if not hwnd or os.name != "nt":
        return False
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def focus_ddo():
    if sys.platform.startswith("linux"):
        return linux_focus_ddo()
    hwnd = find_ddo_window()
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.12)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def latest_session():
    pointer = HERE / "LATEST_SESSION.txt"
    if pointer.exists():
        try:
            p = Path(pointer.read_text(encoding="utf-8").strip())
            if p.exists():
                return p
        except Exception:
            pass
    sessions = [p for p in HERE.glob("combat_*") if p.is_dir()]
    return max(sessions, key=lambda p: p.stat().st_mtime) if sessions else None


def list_sessions():
    """Return recorded combat sessions newest first."""
    sessions = [p for p in HERE.glob("combat_*") if p.is_dir()]
    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


def set_latest_session_pointer(session):
    session = Path(session)
    (HERE / "LATEST_SESSION.txt").write_text(str(session.resolve()), encoding="utf-8")


def _session_timestamp(session):
    name = Path(session).name
    try:
        stamp = name.split("combat_", 1)[1]
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    except Exception:
        try:
            return datetime.fromtimestamp(Path(session).stat().st_mtime)
        except Exception:
            return None


def read_session_catalog_info(session):
    """Read only small summary files so the session picker opens quickly."""
    session = Path(session)
    info = {
        "path": session,
        "date": "",
        "duration": None,
        "damage": None,
        "dps": None,
        "targets": "",
        "status": "Recorded",
    }
    dt = _session_timestamp(session)
    if dt is not None:
        info["date"] = dt.strftime("%Y-%m-%d %H:%M:%S")

    summary_path = session / "analysis_summary.json"
    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            info["duration"] = data.get("recording_duration_seconds")
            info["damage"] = data.get("total_damage")
            info["dps"] = data.get("session_dps", data.get("elapsed_dps"))
            info["status"] = "Analyzed"
        except Exception:
            pass

    if info["duration"] is None:
        try:
            for line in (session / "recording_summary.txt").read_text(encoding="utf-8").splitlines():
                if line.startswith("Elapsed seconds:"):
                    info["duration"] = float(line.split(":", 1)[1].strip())
                    break
        except Exception:
            pass

    try:
        targets = []
        with (session / "damage_by_target.csv").open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                target = str(row.get("target", "")).strip()
                if target and target not in targets:
                    targets.append(target)
                if len(targets) >= 3:
                    break
        info["targets"] = ", ".join(targets)
    except Exception:
        pass
    return info


def read_capture_area():
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
        cap = c.get("capture", {})
        return cap, bool(c.get("capture_configured", False))
    except Exception:
        return {}, False


LINE_HEIGHT_CHOICES = (10, 12, 14, 16, 18, 20)


def read_line_height_setting():
    """Return the user-facing line-height mode stored in config.json."""
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
        a = c.get("analysis", {})
        if bool(a.get("auto_line_height", True)):
            return "Auto"
        h = int(a.get("line_height", 16))
        if h in LINE_HEIGHT_CHOICES:
            return f"{h} px"
    except Exception:
        pass
    return "Auto"


def save_line_height_setting(value):
    """Persist Auto / fixed line height without disturbing other settings."""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    a = cfg.setdefault("analysis", {})
    value = str(value).strip()
    fixed = None
    if value.endswith(" px"):
        try:
            candidate = int(value[:-3].strip())
            if candidate in LINE_HEIGHT_CHOICES:
                fixed = candidate
        except Exception:
            fixed = None

    if fixed is not None:
        a["auto_line_height"] = False
        a["line_height"] = fixed
        a["line_height_mode"] = "manual"
    else:
        a["auto_line_height"] = True
        a["line_height_mode"] = "auto"
        # Seed Auto with the most recently detected good value when available.
        try:
            last = int(a.get("last_detected_line_height", a.get("line_height", 16)))
            if 8 <= last <= 30:
                a["line_height"] = last
        except Exception:
            pass
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)


HOTKEY_CHOICES = (
    "Pause/Break",
    "Ctrl+F8", "Ctrl+F9", "Ctrl+F10", "Ctrl+F11", "Ctrl+F12",
    "Shift+F8", "Shift+F9", "Shift+F10", "Shift+F11", "Shift+F12",
    "Alt+F8", "Alt+F9", "Alt+F10", "Alt+F11", "Alt+F12",
    "Ctrl+Shift+F9", "Ctrl+Shift+F10",
)
OVERLAY_POSITION_CHOICES = {
    "Top left": "top_left", "Top center": "top_center", "Top right": "top_right",
    "Bottom left": "bottom_left", "Bottom center": "bottom_center", "Bottom right": "bottom_right",
    "Custom": "custom",
}
OVERLAY_POSITION_LABELS = {v: k for k, v in OVERLAY_POSITION_CHOICES.items()}
ROLLING_WINDOW_CHOICES = ("5 s", "10 s", "15 s", "20 s", "30 s", "60 s")
FONT_SIZE_CHOICES = ("12", "18", "24", "30", "36")
OCR_THREAD_CHOICES = ("Auto", "2", "4", "6", "8")


def rolling_window_label(seconds):
    try:
        value = float(seconds)
    except Exception:
        value = 15.0
    return f"{int(value) if value.is_integer() else value:g} s"


def rolling_window_short(seconds):
    try:
        value = float(seconds)
    except Exception:
        value = 15.0
    return f"{int(value) if value.is_integer() else value:g}s"


def read_live_user_settings():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    live = cfg.get("live_dps", {})
    return {
        "continuous": bool(live.get("continuous_mode", True)),
        "hotkey": str(live.get("recording_hotkey", "Pause/Break")),
        "position": str(live.get("overlay_position", "top_center")),
        "result_seconds": float(live.get("result_display_seconds", 10.0)),
        "window_seconds": float(live.get("rolling_window_seconds", 15.0)),
        "font_size": int(live.get("font_size", 24)),
        "indra_threads": live.get("indra_threads", "auto"),
    }


def save_live_user_settings(**updates):
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    live = cfg.setdefault("live_dps", {})
    live.update(updates)
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)


class LowLevelHotkeyManager:
    """Windows low-level keyboard hook for the recording toggle.

    Unlike RegisterHotKey, this sees the same VK_PAUSE event as the dedicated
    key test. When the configured key is pressed while DDO is foreground, the
    key-down/key-up pair is consumed so DDO does not receive it.
    """
    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008

    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_PAUSE = 0x13
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C

    def __init__(self, fired_event):
        self.fired_event = fired_event
        self.thread = None
        self.thread_id = None
        self.hotkey = None
        self.registered = False
        self.error = None
        self._ready = threading.Event()
        self._hook_proc = None
        self._hook = None
        self._target_vk = None
        self._required_mods = 0
        self._target_is_down = False
        self._suppress_until_up = False
        self._last_fire = 0.0

    @staticmethod
    def parse(spec):
        parts = [x.strip() for x in str(spec).split("+") if x.strip()]
        mods = 0
        vk = None
        for part in parts:
            low = part.lower()
            if low in ("ctrl", "control"):
                mods |= LowLevelHotkeyManager.MOD_CONTROL
            elif low == "shift":
                mods |= LowLevelHotkeyManager.MOD_SHIFT
            elif low == "alt":
                mods |= LowLevelHotkeyManager.MOD_ALT
            elif low in ("win", "windows"):
                mods |= LowLevelHotkeyManager.MOD_WIN
            elif low in ("pause", "break", "pause/break"):
                vk = LowLevelHotkeyManager.VK_PAUSE
            elif low.startswith("f") and low[1:].isdigit():
                n = int(low[1:])
                if 1 <= n <= 24:
                    vk = 0x70 + n - 1
        if vk is None:
            raise ValueError("Choose Pause/Break or an F-key hotkey (F1-F24).")
        return mods, vk

    @classmethod
    def _current_mods(cls, user32):
        mods = 0
        try:
            if user32.GetAsyncKeyState(cls.VK_CONTROL) & 0x8000:
                mods |= cls.MOD_CONTROL
            if user32.GetAsyncKeyState(cls.VK_SHIFT) & 0x8000:
                mods |= cls.MOD_SHIFT
            if user32.GetAsyncKeyState(cls.VK_MENU) & 0x8000:
                mods |= cls.MOD_ALT
            if ((user32.GetAsyncKeyState(cls.VK_LWIN) & 0x8000) or
                    (user32.GetAsyncKeyState(cls.VK_RWIN) & 0x8000)):
                mods |= cls.MOD_WIN
        except Exception:
            pass
        return mods

    def start(self, spec):
        self.stop()
        self.hotkey = spec
        if os.name != "nt":
            return

        self._required_mods, self._target_vk = self.parse(spec)
        self._target_is_down = False
        self._suppress_until_up = False
        self._last_fire = 0.0
        self.error = None
        self._ready.clear()

        def worker():
            hook = None
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self.thread_id = int(kernel32.GetCurrentThreadId())

                LRESULT = ctypes.c_ssize_t
                ULONG_PTR = ctypes.c_size_t
                HHOOK = wintypes.HANDLE

                class KBDLLHOOKSTRUCT(ctypes.Structure):
                    _fields_ = [
                        ("vkCode", wintypes.DWORD),
                        ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR),
                    ]

                LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
                    LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
                )

                user32.SetWindowsHookExW.argtypes = [
                    ctypes.c_int, LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD
                ]
                user32.SetWindowsHookExW.restype = HHOOK
                user32.CallNextHookEx.argtypes = [
                    HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
                ]
                user32.CallNextHookEx.restype = LRESULT
                user32.UnhookWindowsHookEx.argtypes = [HHOOK]
                user32.UnhookWindowsHookEx.restype = wintypes.BOOL
                user32.GetMessageW.argtypes = [
                    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
                ]
                user32.GetMessageW.restype = wintypes.BOOL
                user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
                user32.GetAsyncKeyState.restype = wintypes.SHORT
                user32.GetForegroundWindow.restype = wintypes.HWND

                def ddo_is_foreground_fast():
                    hwnd = find_ddo_window()
                    if not hwnd:
                        return False
                    try:
                        return int(user32.GetForegroundWindow()) == int(hwnd)
                    except Exception:
                        return False

                @LowLevelKeyboardProc
                def hook_proc(nCode, wParam, lParam):
                    if nCode == self.HC_ACTION:
                        info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = int(info.vkCode)
                        if vk == self._target_vk:
                            is_down = int(wParam) in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                            is_up = int(wParam) in (self.WM_KEYUP, self.WM_SYSKEYUP)

                            if is_down:
                                mods_match = self._current_mods(user32) == self._required_mods
                                if mods_match and ddo_is_foreground_fast():
                                    # The ordinary F-keys generate repeated WM_KEYDOWN while held.
                                    # Fire once and consume repeats until key-up. Pause/Break is odd
                                    # on some keyboards, so also keep a short time guard.
                                    now = time.monotonic()
                                    if (not self._target_is_down or
                                            (self._target_vk == self.VK_PAUSE and now - self._last_fire > 0.75)):
                                        self._target_is_down = True
                                        self._last_fire = now
                                        self.fired_event.set()
                                    self._suppress_until_up = True
                                    return 1

                            elif is_up:
                                was_suppressed = self._suppress_until_up
                                self._target_is_down = False
                                self._suppress_until_up = False
                                if was_suppressed:
                                    return 1

                    return user32.CallNextHookEx(None, nCode, wParam, lParam)

                self._hook_proc = hook_proc  # keep callback alive
                ctypes.set_last_error(0)
                hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, hook_proc, None, 0)
                self._hook = hook
                if not hook:
                    err = ctypes.get_last_error()
                    self.error = f"Low-level keyboard hook failed (Windows error {err})."
                    self.registered = False
                    return

                self.registered = True
                self._ready.set()
                msg = wintypes.MSG()
                while True:
                    result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if result == 0:
                        break
                    if result == -1:
                        self.error = f"Keyboard message loop failed (Windows error {ctypes.get_last_error()})."
                        break
            except Exception as exc:
                self.error = str(exc)
            finally:
                self._ready.set()
                if hook:
                    try:
                        user32.UnhookWindowsHookEx(hook)
                    except Exception:
                        pass
                self._hook = None
                self._hook_proc = None
                self.registered = False

        self.thread = threading.Thread(target=worker, name="low-level-hotkey", daemon=True)
        self.thread.start()
        self._ready.wait(timeout=0.75)
        if not self.registered:
            raise RuntimeError(self.error or "Low-level keyboard hook could not be installed.")

    def stop(self):
        old_thread = self.thread
        if os.name == "nt" and self.thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
        if old_thread is not None and old_thread.is_alive() and old_thread is not threading.current_thread():
            old_thread.join(timeout=0.6)
        self.thread = None
        self.thread_id = None
        self.registered = False
        self._hook = None
        self._hook_proc = None
        self._target_is_down = False
        self._suppress_until_up = False


if sys.platform.startswith("linux"):
    LowLevelHotkeyManager = LinuxHotkeyManager


# ----------------------------- application --------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DDO Combat Analyzer")
        self.geometry("1020x805")
        self.minsize(900, 690)

        try:
            self.iconname("DDO Combat Analyzer")
        except Exception:
            pass

        self.proc = None
        self.proc_kind = None
        self.live_proc = None
        self.live_log_handle = None
        self.recording_session = None
        self.output_queue = queue.Queue()
        self.start_time = None
        self.timer_job = None
        self.current_session = None
        self.line_height_var = tk.StringVar(value=read_line_height_setting())
        live_settings = read_live_user_settings()
        self.continuous_live_var = tk.BooleanVar(value=live_settings["continuous"])
        self.hotkey_var = tk.StringVar(value=live_settings["hotkey"] if live_settings["hotkey"] in HOTKEY_CHOICES else "Pause/Break")
        self.overlay_position_var = tk.StringVar(value=OVERLAY_POSITION_LABELS.get(live_settings["position"], "Custom"))
        self.result_display_seconds = float(live_settings["result_seconds"])
        self.rolling_window_var = tk.StringVar(value=rolling_window_label(live_settings["window_seconds"]))
        initial_font_size = str(live_settings.get("font_size", 24))
        if initial_font_size not in FONT_SIZE_CHOICES:
            initial_font_size = "24"
        self.overlay_font_size_var = tk.StringVar(value=initial_font_size)
        raw_ocr_threads = str(live_settings.get("indra_threads", "auto")).strip()
        initial_ocr_threads = "Auto" if raw_ocr_threads.lower() == "auto" else raw_ocr_threads
        if initial_ocr_threads not in OCR_THREAD_CHOICES:
            initial_ocr_threads = "Auto"
        self.ocr_threads_var = tk.StringVar(value=initial_ocr_threads)
        self.detected_physical_cores = physical_core_count()
        self.graph_window_seconds = float(live_settings["window_seconds"])
        self.recording_started_by_hotkey = False
        self.recording_live_boundary_epoch = None
        self.analysis_background = False
        self.setup_chain_active = False
        self.suppress_continuous_live = False
        self.hotkey_event = threading.Event()
        self.hotkey_manager = LowLevelHotkeyManager(self.hotkey_event)

        self._build_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.after(150, self.poll_output)
        self.after(250, self.refresh_system_status)
        self.after(300, self.refresh_capture_label)
        self.after(500, self.try_load_latest_result)
        self.after(650, self.configure_global_hotkey)
        self.after(800, self.monitor_continuous_live)
        self.after(100, self.poll_hotkey_event)
        self.after(1100, self.show_first_run_guide_if_needed)

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Big.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Small.TLabel", font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 10))
        style.configure("Stop.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 10))
        style.configure("Treeview", rowheight=27, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("ResultsTitle.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("ResultsHint.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Results.TNotebook", tabmargins=(2, 6, 2, 0))
        style.configure("Results.TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=(11, 8))
        style.map(
            "Results.TNotebook.Tab",
            foreground=[("selected", "#0b4f8a"), ("!selected", "#222222")],
            background=[("selected", "#ffffff"), ("!selected", "#d9e2f3")],
        )

    def _build_ui(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        # Header
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="DDO Combat Analyzer", style="Title.TLabel").pack(side="left")

        self.system_label = ttk.Label(top, text="System check ...")
        self.system_label.pack(side="right", padx=(8, 0))
        self.system_button = ttk.Button(top, text="System check", command=self.show_system_check)
        self.system_button.pack(side="right")
        self.about_button = ttk.Button(top, text="About", command=self.show_about)
        self.about_button.pack(side="right", padx=(0, 8))

        # Setup / control panel
        control = ttk.LabelFrame(outer, text="Recording", padding=12)
        control.pack(fill="x", pady=(12, 10))

        row1 = ttk.Frame(control)
        row1.pack(fill="x")

        self.capture_label = ttk.Label(row1, text="Combat log area: ...")
        self.capture_label.pack(side="left", fill="x", expand=True)

        row1_tools = ttk.Frame(row1)
        row1_tools.pack(side="right")

        ttk.Label(row1_tools, text="Line height:").pack(side="left", padx=(0, 5))
        self.line_height_combo = ttk.Combobox(
            row1_tools,
            textvariable=self.line_height_var,
            values=("Auto", "10 px", "12 px", "14 px", "16 px", "18 px", "20 px"),
            state="readonly",
            width=7,
        )
        self.line_height_combo.pack(side="left", padx=(0, 10))
        self.line_height_combo.bind("<<ComboboxSelected>>", self.on_line_height_changed)

        self.calibrate_btn = ttk.Button(
            row1_tools, text="Select / adjust log area", command=self.configure_area
        )
        self.calibrate_btn.pack(side="left")

        reminder = ttk.Label(
            control,
            text="Before recording: make sure the DDO Combat Log tab is visible.",
            style="Small.TLabel"
        )
        reminder.pack(anchor="w", pady=(8, 0))

        row2 = ttk.Frame(control)
        row2.pack(fill="x", pady=(12, 0))

        self.status_dot = tk.Canvas(row2, width=16, height=16, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 7))
        self.status_circle = self.status_dot.create_oval(3, 3, 13, 13, fill="#777777", outline="")

        self.status_label = ttk.Label(row2, text="Ready", style="Section.TLabel")
        self.status_label.pack(side="left")

        self.elapsed_label = ttk.Label(row2, text="", style="Section.TLabel")
        self.elapsed_label.pack(side="left", padx=(12, 0))

        actions = ttk.Frame(row2)
        actions.pack(side="right")

        self.start_btn = ttk.Button(
            actions, text="START RECORDING", style="Accent.TButton", command=self.start_recording
        )
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(
            actions, text="STOP & ANALYZE", style="Stop.TButton",
            command=self.stop_recording, state="disabled"
        )
        self.stop_btn.pack(side="left")

        # Session actions kept near the top so old analyses are easy to revisit.
        row3 = ttk.Frame(control)
        row3.pack(fill="x", pady=(10, 0))

        self.open_session_btn = ttk.Button(
            row3, text="Open session...", command=self.open_session_browser
        )
        self.open_session_btn.pack(side="left")

        self.open_folder_btn = ttk.Button(
            row3, text="Session folder", command=self.open_session_folder, state="disabled"
        )
        self.open_folder_btn.pack(side="left", padx=(8, 0))

        self.reprocess_btn = ttk.Button(
            row3, text="Reanalyze selected", command=self.reprocess_selected, state="disabled"
        )
        self.reprocess_btn.pack(side="left", padx=(8, 0))

        self.replay_live_btn = ttk.Button(
            row3, text="Replay live test", command=self.replay_live_selected, state="disabled"
        )
        self.replay_live_btn.pack(side="left", padx=(8, 0))

        self.details_btn = ttk.Button(
            row3, text="Detailed Results", command=self.show_detailed_results, state="disabled"
        )
        self.details_btn.pack(side="left", padx=(8, 0))

        self.detail_label = ttk.Label(row3, text="")
        self.detail_label.pack(side="right")

        row4 = ttk.Frame(control)
        row4.pack(fill="x", pady=(10, 0))
        self.continuous_check = ttk.Checkbutton(
            row4, text="Continuous live DPS", variable=self.continuous_live_var,
            command=self.on_continuous_live_changed
        )
        self.continuous_check.pack(side="left")
        ttk.Label(row4, text="Recording hotkey:").pack(side="left", padx=(18, 5))
        self.hotkey_combo = ttk.Combobox(
            row4, textvariable=self.hotkey_var, values=HOTKEY_CHOICES,
            state="readonly", width=16
        )
        self.hotkey_combo.pack(side="left")
        self.hotkey_combo.bind("<<ComboboxSelected>>", self.on_hotkey_changed)

        ttk.Label(row4, text="Overlay:").pack(side="left", padx=(18, 5))
        self.overlay_position_combo = ttk.Combobox(
            row4, textvariable=self.overlay_position_var,
            values=tuple(OVERLAY_POSITION_CHOICES.keys()), state="readonly", width=14
        )
        self.overlay_position_combo.pack(side="left")
        self.overlay_position_combo.bind("<<ComboboxSelected>>", self.on_overlay_position_changed)
        self.position_overlay_btn = ttk.Button(row4, text="Position overlay", command=self.position_overlay)
        self.position_overlay_btn.pack(side="left", padx=(8, 0))

        row5 = ttk.Frame(control)
        row5.pack(fill="x", pady=(8, 0))
        ttk.Label(row5, text="Rolling DPS/APM window:").pack(side="left")
        self.rolling_window_combo = ttk.Combobox(
            row5, textvariable=self.rolling_window_var, values=ROLLING_WINDOW_CHOICES,
            state="readonly", width=7
        )
        self.rolling_window_combo.pack(side="left", padx=(6, 14))
        self.rolling_window_combo.bind("<<ComboboxSelected>>", self.on_rolling_window_changed)

        ttk.Label(row5, text="Overlay font:").pack(side="left")
        self.overlay_font_size_combo = ttk.Combobox(
            row5, textvariable=self.overlay_font_size_var, values=FONT_SIZE_CHOICES,
            state="readonly", width=4
        )
        self.overlay_font_size_combo.pack(side="left", padx=(6, 14))
        self.overlay_font_size_combo.bind("<<ComboboxSelected>>", self.on_overlay_font_size_changed)

        ttk.Label(
            row5, text="15 s is the default rolling window. Overlay font sizes: 12–36.",
            style="Small.TLabel"
        ).pack(side="left")

        row6 = ttk.Frame(control)
        row6.pack(fill="x", pady=(8, 0))
        ttk.Label(row6, text="OCR threads:").pack(side="left")
        self.ocr_threads_combo = ttk.Combobox(
            row6, textvariable=self.ocr_threads_var, values=OCR_THREAD_CHOICES,
            state="readonly", width=6
        )
        self.ocr_threads_combo.pack(side="left", padx=(6, 14))
        self.ocr_threads_combo.bind("<<ComboboxSelected>>", self.on_ocr_threads_changed)
        auto_now = resolve_indra_threads("auto")
        ttk.Label(
            row6,
            text=(
                f"Auto = 2 threads on 1–2 physical cores, otherwise 4. "
                f"Detected: {self.detected_physical_cores} physical core(s) → Auto uses {auto_now}."
            ),
            style="Small.TLabel"
        ).pack(side="left")

        # Progress line
        prog = ttk.Frame(outer)
        prog.pack(fill="x", pady=(0, 10))
        self.progress = ttk.Progressbar(prog, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_text = ttk.Label(prog, text="Ready")
        self.progress_text.pack(side="left", padx=(10, 0))

        # Results: make it visually obvious that several selectable views are available.
        results_header = ttk.Frame(outer)
        results_header.pack(fill="x", pady=(2, 4))
        ttk.Label(
            results_header, text="RESULTS — choose a tab below", style="ResultsTitle.TLabel"
        ).pack(side="left")
        self.results_active_var = tk.StringVar(value="Active tab: Overview")
        ttk.Label(
            results_header, textvariable=self.results_active_var, style="ResultsHint.TLabel"
        ).pack(side="right")

        self.notebook = ttk.Notebook(outer, style="Results.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        self.overview_tab = ttk.Frame(self.notebook, padding=12)
        self.target_tab = ttk.Frame(self.notebook, padding=12)
        self.type_tab = ttk.Frame(self.notebook, padding=12)
        self.taken_tab = ttk.Frame(self.notebook, padding=12)
        self.mitigation_tab = ttk.Frame(self.notebook, padding=12)
        self.attack_tab = ttk.Frame(self.notebook, padding=12)
        self.log_tab = ttk.Frame(self.notebook, padding=12)
        self.diag_tab = ttk.Frame(self.notebook, padding=12)
        self.dps_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.overview_tab, text="Overview")
        self.notebook.add(self.target_tab, text="Targets")
        self.notebook.add(self.type_tab, text="Damage types")
        self.notebook.add(self.taken_tab, text="Damage taken")
        self.notebook.add(self.mitigation_tab, text="Blocked / DR")
        self.notebook.add(self.attack_tab, text="Attacks / Crits")
        self.notebook.add(self.log_tab, text="Event log")
        self.notebook.add(self.diag_tab, text="Data quality")
        self.notebook.add(self.dps_tab, text="DPS graph")
        self.notebook.bind("<<NotebookTabChanged>>", self.on_result_tab_changed)

        self._build_overview()
        self._build_target_table()
        self._build_type_table()
        self._build_taken_table()
        self._build_mitigation()
        self._build_attack_crit()
        self._build_damage_log()
        self._build_diagnostics()
        self._build_dps_graph()

        # The DPS graph is a post-analysis result, so keep it hidden until
        # time-stamped damage events have been produced.
        self.notebook.hide(self.dps_tab)

    def on_result_tab_changed(self, _event=None):
        try:
            selected = self.notebook.select()
            label = self.notebook.tab(selected, "text") if selected else "Overview"
            self.results_active_var.set(f"Active tab: {label}")
        except Exception:
            pass

    def _metric_card(self, parent, title, var, col):
        frame = ttk.LabelFrame(parent, text=title, padding=(12, 10))
        frame.grid(row=0, column=col, sticky="nsew", padx=5)
        ttk.Label(frame, textvariable=var, style="Big.TLabel").pack(anchor="center", pady=8)

    def _build_overview(self):
        self.total_dealt_var = tk.StringVar(value="—")
        self.regular_var = tk.StringVar(value="—")
        self.sneak_var = tk.StringVar(value="—")
        self.total_taken_var = tk.StringVar(value="—")

        cards = ttk.Frame(self.overview_tab)
        cards.pack(fill="x")
        for i in range(4):
            cards.columnconfigure(i, weight=1)
        self._metric_card(cards, "Total damage", self.total_dealt_var, 0)
        self._metric_card(cards, "Regular", self.regular_var, 1)
        self._metric_card(cards, "Sneak attack", self.sneak_var, 2)
        self._metric_card(cards, "Damage taken", self.total_taken_var, 3)

        netframe = ttk.LabelFrame(self.overview_tab, text="HP impact on targets", padding=12)
        netframe.pack(fill="x", pady=(14, 0))
        self.healing_var = tk.StringVar(value="Healing to targets: —")
        self.net_loss_var = tk.StringVar(value="Net target HP loss: —")
        ttk.Label(netframe, textvariable=self.healing_var, style="Section.TLabel").pack(side="left")
        ttk.Label(netframe, textvariable=self.net_loss_var, style="Section.TLabel").pack(
            side="left", padx=(30, 0)
        )

        perf = ttk.LabelFrame(self.overview_tab, text="Session performance", padding=12)
        perf.pack(fill="x", pady=(12, 0))
        self.elapsed_dps_var = tk.StringVar(value="Session DPS: —")
        self.active_dps_var = tk.StringVar(value="Active combat DPS: —")
        self.blocked_var = tk.StringVar(value="Blocked/resisted: —")
        self.sneak_stats_var = tk.StringVar(value="Sneak: —")
        for var in (self.elapsed_dps_var, self.active_dps_var, self.blocked_var, self.sneak_stats_var):
            ttk.Label(perf, textvariable=var, style="Section.TLabel").pack(side="left", padx=(0, 24))

        self.overview_note = ttk.Label(
            self.overview_tab,
            text="Results will appear here automatically when analysis is complete.",
            wraplength=800
        )
        self.overview_note.pack(anchor="w", pady=(18, 0))

    @staticmethod
    def _tree_sort_key(value, numeric=False):
        text = str(value).strip()
        if text in ("", "—", "-"):
            return None
        if not numeric:
            return text.casefold()

        cleaned = text.replace(",", "").replace("%", "").strip()
        if cleaned.lower().endswith(" s"):
            cleaned = cleaned[:-2].strip()
        # Time columns are displayed as m:ss. Treat them numerically.
        if ":" in cleaned:
            parts = cleaned.split(":")
            if len(parts) == 2:
                try:
                    return float(parts[0]) * 60.0 + float(parts[1])
                except Exception:
                    pass
        try:
            return float(cleaned)
        except Exception:
            return text.casefold()

    def _enable_tree_sorting(self, tree, numeric_columns=()):
        numeric_columns = set(numeric_columns or ())
        columns = tuple(tree.cget("columns"))
        base = {c: tree.heading(c, "text") for c in columns}
        tree._sort_base_headings = base
        tree._sort_numeric_columns = numeric_columns
        tree._sort_column = None
        tree._sort_descending = False

        def sort_column(col):
            descending = False
            if tree._sort_column == col:
                descending = not tree._sort_descending

            valid = []
            missing = []
            numeric = col in tree._sort_numeric_columns
            for iid in tree.get_children(""):
                raw = tree.set(iid, col)
                key = self._tree_sort_key(raw, numeric=numeric)
                if key is None:
                    missing.append(iid)
                else:
                    valid.append((key, iid))

            try:
                valid.sort(key=lambda pair: pair[0], reverse=descending)
            except TypeError:
                valid.sort(key=lambda pair: str(pair[0]).casefold(), reverse=descending)

            for index, iid in enumerate([iid for _key, iid in valid] + missing):
                tree.move(iid, "", index)

            tree._sort_column = col
            tree._sort_descending = descending
            for c in columns:
                label = base[c]
                if c == col:
                    label += " ▼" if descending else " ▲"
                tree.heading(c, text=label)

        for c in columns:
            tree.heading(c, command=lambda col=c: sort_column(col))

    def _reset_tree_sort(self, tree):
        try:
            tree._sort_column = None
            tree._sort_descending = False
            base = tree._sort_base_headings
            for c in tree.cget("columns"):
                tree.heading(c, text=base[c])
        except Exception:
            pass

    def make_tree(self, parent, columns, headings, widths, numeric_columns=(), stretch_columns=()):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)

        tree = ttk.Treeview(frame, columns=columns, show="headings")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        for c, h, w in zip(columns, headings, widths):
            tree.heading(c, text=h)
            anchor = "w" if c in ("target", "type", "source") else "e"
            tree.column(c, width=w, anchor=anchor, stretch=(c in stretch_columns))
        self._enable_tree_sorting(tree, numeric_columns=numeric_columns)
        return tree

    def _build_target_table(self):
        actions = ttk.Frame(self.target_tab)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Label(
            actions,
            text="Double-click a mob row for detailed analysis.",
            style="Small.TLabel",
        ).pack(side="left")
        self.target_details_btn = ttk.Button(
            actions,
            text="Details...",
            command=self.show_selected_target_details,
            state="disabled",
        )
        self.target_details_btn.pack(side="right")

        self.target_tree = self.make_tree(
            self.target_tab,
            ("target", "regular", "sneak", "damage", "healing", "net", "hits"),
            ("Target", "Regular", "Sneak", "Damage", "Healing", "Net HP", "Hits"),
            (250, 100, 100, 110, 90, 110, 60),
            numeric_columns=("regular", "sneak", "damage", "healing", "net", "hits"),
        )
        self.target_tree.bind("<<TreeviewSelect>>", self._on_target_selection)
        self.target_tree.bind("<Double-1>", lambda event: self._open_target_from_tree(self.target_tree, event))

    def _on_target_selection(self, _event=None):
        try:
            enabled = bool(self.target_tree.selection()) and self.current_session is not None
            self.target_details_btn.config(state="normal" if enabled else "disabled")
        except Exception:
            pass

    def _tree_target_value(self, tree, iid=None):
        try:
            if iid is None:
                selected = tree.selection()
                if not selected:
                    return ""
                iid = selected[0]
            values = tree.item(iid, "values")
            if not values:
                return ""
            return str(values[0]).strip()
        except Exception:
            return ""

    def _open_target_from_tree(self, tree, event=None):
        try:
            iid = tree.identify_row(event.y) if event is not None else ""
            if iid:
                tree.selection_set(iid)
                tree.focus(iid)
            target = self._tree_target_value(tree, iid or None)
            if target:
                self.show_target_details(target)
        except Exception as exc:
            messagebox.showerror("Mob details", f"Could not open mob details.\n\n{exc}")

    def show_selected_target_details(self):
        target = self._tree_target_value(self.target_tree)
        if not target:
            messagebox.showinfo("Mob details", "Select a target first.")
            return
        self.show_target_details(target)

    @staticmethod
    def _read_csv_rows(path):
        try:
            with Path(path).open("r", encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f))
        except Exception:
            return []

    @staticmethod
    def _as_int(value, default=0):
        try:
            return int(float(str(value or "0").replace(",", "")))
        except Exception:
            return default

    @staticmethod
    def _as_float(value, default=0.0):
        try:
            return float(str(value or "0").replace(",", ""))
        except Exception:
            return default

    @staticmethod
    def _normalized_entity_name(value):
        text = str(value or "").strip()
        # Some OCR variants can leave a leading Combat marker on incoming sources.
        text = re.sub(r"^.*?combat\)\s*:\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"^[^A-Za-z0-9]+", "", text).strip()
        return re.sub(r"\s+", " ", text).casefold()

    def _source_matches_target(self, source, target):
        src = self._normalized_entity_name(source)
        tgt = self._normalized_entity_name(target)
        if not src or not tgt:
            return False
        if src == tgt:
            return True
        if not src.startswith(tgt):
            return False
        rest = src[len(tgt):].lstrip()
        # Named spells/effects are usually logged as "Mob's effect". OCR can
        # turn the apostrophe into either straight/curly variants, sometimes twice.
        return bool(rest) and rest[0] in ("'", "’")

    @staticmethod
    def _display_source_name(value):
        text = str(value or "").strip()
        text = re.sub(r"^.*?combat\)\s*:\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"^[^A-Za-z0-9]+", "", text).strip()
        text = re.sub(r"[’']+s\b", "’s", text)
        return text or "Unknown"

    def _detail_tree(self, parent, columns, headings, widths, numeric_columns=(), stretch_columns=()):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for c, h, w in zip(columns, headings, widths):
            tree.heading(c, text=h)
            tree.column(
                c, width=w,
                anchor="w" if c in ("target", "type", "source", "reduction", "result", "text") else "e",
                stretch=(c in stretch_columns),
            )
        self._enable_tree_sorting(tree, numeric_columns=numeric_columns)
        return tree

    def _detail_metric(self, parent, title, value, row, col):
        box = ttk.LabelFrame(parent, text=title, padding=(10, 8))
        box.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        ttk.Label(box, text=value, style="Big.TLabel").pack(anchor="center", pady=5)

    def show_target_details(self, target):
        session = Path(self.current_session) if self.current_session else None
        if not session or not session.exists():
            messagebox.showinfo("Mob details", "No analyzed session is currently loaded.")
            return

        event_rows = self._read_csv_rows(session / "damage_events.csv")
        attack_rows = self._read_csv_rows(session / "attack_events.csv")
        crit_rows = self._read_csv_rows(session / "crit_by_target.csv")
        mitigation_rows = self._read_csv_rows(session / "mitigation_by_target_and_type.csv")
        defense_rows = self._read_csv_rows(session / "incoming_defense_events.csv")

        outgoing = [r for r in event_rows if r.get("event") == "damage_dealt" and r.get("target") == target]
        healing = [r for r in event_rows if r.get("event") == "healing_to_target" and r.get("target") == target]
        incoming = [
            r for r in event_rows
            if r.get("event") == "damage_taken" and self._source_matches_target(r.get("source", ""), target)
        ]
        target_key = self._normalized_entity_name(target)
        direct_incoming = [
            r for r in incoming
            if self._normalized_entity_name(r.get("source", "")) == target_key
        ]
        mob_defense = [
            r for r in defense_rows
            if self._normalized_entity_name(r.get("source", "")) == target_key
        ]
        mob_attacks = [r for r in attack_rows if r.get("target") == target and r.get("event") != "confirm"]
        mob_confirms = [r for r in attack_rows if r.get("target") == target and r.get("event") == "confirm"]
        crit_row = next((r for r in crit_rows if r.get("target") == target), {})
        mob_mitigation = [r for r in mitigation_rows if r.get("target") == target]

        regular = sum(self._as_int(r.get("amount")) for r in outgoing if r.get("mode") == "regular")
        sneak = sum(self._as_int(r.get("amount")) for r in outgoing if r.get("mode") == "sneak")
        total_damage = sum(self._as_int(r.get("amount")) for r in outgoing)
        damage_hits = len(outgoing)
        healed = sum(self._as_int(r.get("amount")) for r in healing)
        net_hp = total_damage - healed
        avg_hit = (total_damage / damage_hits) if damage_hits else 0.0
        blocked = sum(self._as_int(r.get("blocked")) for r in mob_mitigation)
        incoming_total = sum(self._as_int(r.get("amount")) for r in incoming)
        incoming_hits = len(incoming)

        attacks = self._as_int(crit_row.get("attacks"), len(mob_attacks))
        hits = self._as_int(crit_row.get("hits"))
        grazes = self._as_int(crit_row.get("grazing_hits"))
        misses = self._as_int(crit_row.get("misses"))
        threats = self._as_int(crit_row.get("critical_threats"))
        confirmed = self._as_int(crit_row.get("confirmed_crits"))
        failed_confirms = self._as_int(crit_row.get("failed_confirms"))

        # Evidence-based hit threshold bounds. Natural 1/20 and grazes are
        # excluded because they do not cleanly establish the ordinary full-hit
        # threshold.  Keep both natural-d20 and total-roll observations.
        eligible_misses = []
        eligible_full_hits = []
        for r in mob_attacks:
            roll = self._as_int(r.get("roll"), -1)
            if roll in (1, 20) or roll < 1:
                continue
            total = self._as_int(r.get("total"), -1)
            outcome = str(r.get("outcome", "")).strip()
            row = (roll, total if total >= 0 else None)
            if outcome == "miss":
                eligible_misses.append(row)
            elif outcome in ("hit", "critical_threat"):
                eligible_full_hits.append(row)

        highest_miss_roll = max((r for r, _t in eligible_misses), default=None)
        lowest_full_hit_roll = min((r for r, _t in eligible_full_hits), default=None)
        highest_miss_total = max((t for _r, t in eligible_misses if t is not None), default=None)
        lowest_full_hit_total = min((t for _r, t in eligible_full_hits if t is not None), default=None)

        def threshold_label(high_fail, low_success):
            if high_fail is not None and low_success is not None:
                lo = high_fail + 1
                hi = low_success
                if lo == hi:
                    return str(hi)
                if lo < hi:
                    return f"{lo}-{hi}"
                return f"varies ({high_fail} miss / {low_success} hit)"
            if low_success is not None:
                return f"<= {low_success}"
            if high_fail is not None:
                return f"> {high_fail}"
            return "—"

        observed_hit_total = threshold_label(highest_miss_total, lowest_full_hit_total)

        defense_counts = {
            "regular_miss": 0, "dodge": 0, "blur": 0,
            "displacement": 0, "deflect": 0, "other": 0,
        }
        for r in mob_defense:
            reason = str(r.get("reason", "other") or "other").strip().lower()
            defense_counts[reason if reason in defense_counts else "other"] += 1
        direct_hits = len(direct_incoming)
        total_defensive_misses = sum(defense_counts.values())
        observed_direct_attacks = direct_hits + total_defensive_misses

        win = tk.Toplevel(self)
        win.title(f"Mob details — {target}")
        win.geometry("1180x780")
        win.minsize(900, 620)
        win.transient(self)

        head = ttk.Frame(win, padding=(14, 12, 14, 4))
        head.pack(fill="x")
        ttk.Label(head, text=target, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"Session: {session.name}", style="Small.TLabel").pack(side="right")

        tabs = ttk.Notebook(win, style="Results.TNotebook")
        tabs.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        overview_tab = ttk.Frame(tabs, padding=12)
        attacks_tab = ttk.Frame(tabs, padding=12)
        crits_tab = ttk.Frame(tabs, padding=12)
        damage_tab = ttk.Frame(tabs, padding=12)
        dr_tab = ttk.Frame(tabs, padding=12)
        incoming_tab = ttk.Frame(tabs, padding=12)
        tabs.add(overview_tab, text="Overview")
        tabs.add(attacks_tab, text="Attacks")
        tabs.add(crits_tab, text="Crits")
        tabs.add(damage_tab, text="Damage")
        tabs.add(dr_tab, text="DR / Resistance")
        tabs.add(incoming_tab, text="Incoming damage")

        # Overview cards
        cards = ttk.Frame(overview_tab)
        cards.pack(fill="x")
        for c in range(4):
            cards.columnconfigure(c, weight=1)
        self._detail_metric(cards, "Damage dealt", dknum(total_damage), 0, 0)
        self._detail_metric(cards, "Damage hits", dknum(damage_hits), 0, 1)
        self._detail_metric(cards, "Avg. damage / hit", dknum(round(avg_hit)), 0, 2)
        self._detail_metric(cards, "Blocked / resisted", dknum(blocked), 0, 3)
        self._detail_metric(cards, "Attacks", dknum(attacks), 1, 0)
        self._detail_metric(cards, "Hits / grazes / misses", f"{hits} / {grazes} / {misses}", 1, 1)
        self._detail_metric(cards, "Confirmed crits", dknum(confirmed), 1, 2)
        self._detail_metric(cards, "Incoming damage", dknum(incoming_total), 1, 3)
        self._detail_metric(cards, "Regular damage", dknum(regular), 2, 0)
        self._detail_metric(cards, "Sneak damage", dknum(sneak), 2, 1)
        self._detail_metric(cards, "Healing to target", dknum(healed), 2, 2)
        self._detail_metric(cards, "Net HP loss", dknum(net_hp), 2, 3)

        reductions = sorted({r.get("reduction_type", "").strip() for r in mob_mitigation if r.get("reduction_type", "").strip()})
        summary = ttk.LabelFrame(overview_tab, text="Observed defenses", padding=12)
        summary.pack(fill="x", pady=(14, 0))
        ttk.Label(
            summary,
            text=(", ".join(reductions) if reductions else "No outgoing damage reduction/resistance observed."),
            style="Section.TLabel",
        ).pack(anchor="w")

        threshold_box = ttk.LabelFrame(overview_tab, text="Observed attack thresholds", padding=12)
        threshold_box.pack(fill="x", pady=(10, 0))
        confirm_observed_overview = crit_row.get("observed_confirm", "") or "—"
        ttk.Label(
            threshold_box,
            text=(f"Full-hit threshold (total roll): {observed_hit_total}    "
                  f"Critical-confirm threshold (total roll): {confirm_observed_overview}"),
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            threshold_box,
            text="Observed bounds only; natural 1/20 and grazing hits are excluded. Defenses/debuffs can change during combat.",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        # Attacks: every natural d20 from 1 through 20, including zero-observation rows.
        attack_summary = ttk.LabelFrame(attacks_tab, text="Attack roll summary", padding=10)
        attack_summary.pack(fill="x", pady=(0, 10))
        hit_rate = (100.0 * hits / attacks) if attacks else 0.0
        graze_rate = (100.0 * grazes / attacks) if attacks else 0.0
        ttk.Label(
            attack_summary,
            text=(f"Attacks: {attacks:,}    Hits: {hits:,}    Grazes: {grazes:,}    Misses: {misses:,}    "
                  f"Hit rate: {hit_rate:.1f}%    Graze rate: {graze_rate:.1f}%"),
            style="Section.TLabel",
        ).pack(anchor="w")
        miss_roll_text = "—" if highest_miss_roll is None else str(highest_miss_roll)
        hit_roll_text = "—" if lowest_full_hit_roll is None else str(lowest_full_hit_roll)
        miss_total_text = "—" if highest_miss_total is None else str(highest_miss_total)
        hit_total_text = "—" if lowest_full_hit_total is None else str(lowest_full_hit_total)
        ttk.Label(
            attack_summary,
            text=(f"Highest observed miss: d20 {miss_roll_text} / total {miss_total_text}    "
                  f"Lowest observed full hit: d20 {hit_roll_text} / total {hit_total_text}    "
                  f"Observed full-hit threshold (total): {observed_hit_total}"),
            style="Small.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            attack_summary,
            text="Natural 1/20 and grazing hits are excluded from threshold bounds.",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        d20_tree = self._detail_tree(
            attacks_tab,
            ("roll", "attacks", "miss", "graze", "hit", "threat", "other", "confirmed", "failed_confirm"),
            ("d20", "Attacks", "Miss", "Graze", "Normal hit", "Crit threat", "Other", "Confirmed crits", "Failed confirms"),
            (60, 85, 75, 75, 90, 95, 70, 115, 110),
            numeric_columns=("roll", "attacks", "miss", "graze", "hit", "threat", "other", "confirmed", "failed_confirm"),
        )
        by_roll = {n: {"attacks": 0, "miss": 0, "graze": 0, "hit": 0, "threat": 0, "other": 0, "confirmed": 0, "failed_confirm": 0} for n in range(1, 21)}
        attack_line_to_roll = {}
        for r in mob_attacks:
            roll = self._as_int(r.get("roll"), -1)
            if roll not in by_roll:
                continue
            d = by_roll[roll]
            d["attacks"] += 1
            outcome = str(r.get("outcome", "")).strip()
            if outcome == "miss":
                d["miss"] += 1
            elif outcome == "grazing_hit":
                d["graze"] += 1
            elif outcome == "critical_threat":
                d["threat"] += 1
            elif outcome == "hit":
                d["hit"] += 1
            else:
                d["other"] += 1
            line = str(r.get("line", "")).strip()
            if line:
                attack_line_to_roll[line] = roll
        for r in mob_confirms:
            linked = str(r.get("linked_attack_line", "")).strip()
            roll = attack_line_to_roll.get(linked)
            if roll not in by_roll:
                continue
            confirmed_value = str(r.get("confirmed", "")).strip().lower() in ("1", "true", "yes")
            by_roll[roll]["confirmed" if confirmed_value else "failed_confirm"] += 1
        for roll in range(1, 21):
            d = by_roll[roll]
            d20_tree.insert("", "end", values=(roll, d["attacks"], d["miss"], d["graze"], d["hit"], d["threat"], d["other"], d["confirmed"], d["failed_confirm"]))

        # Crits: target-level summary plus every confirmation roll.
        crit_summary = ttk.LabelFrame(crits_tab, text="Critical summary", padding=10)
        crit_summary.pack(fill="x", pady=(0, 10))
        confirm_attempts = self._as_int(crit_row.get("confirm_attempts"), len(mob_confirms))
        confirm_rate = self._as_float(crit_row.get("confirm_rate_percent"))
        crit_rate = self._as_float(crit_row.get("crit_rate_percent"))
        observed = crit_row.get("observed_confirm", "") or "—"
        high_fail = crit_row.get("highest_failed_confirm_total", "") or "—"
        low_success = crit_row.get("lowest_successful_confirm_total", "") or "—"
        ttk.Label(
            crit_summary,
            text=(f"Threats: {threats:,}    Confirm attempts: {confirm_attempts:,}    Confirmed: {confirmed:,}    "
                  f"Failed: {failed_confirms:,}    Crit rate: {crit_rate:.1f}%    Confirm rate: {confirm_rate:.1f}%"),
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            crit_summary,
            text=(f"Observed confirmation threshold (total roll): {observed}    "
                  f"Highest failed total: {high_fail}    Lowest successful total: {low_success}"),
            style="Small.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            crit_summary,
            text="Natural 1/20 confirmation rolls are excluded from threshold bounds.",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        confirm_tree = self._detail_tree(
            crits_tab,
            ("time", "roll", "bonus", "total", "result", "text"),
            ("Time", "d20", "Bonus", "Total", "Result", "Combat Log text"),
            (80, 65, 80, 80, 95, 620),
            numeric_columns=("time", "roll", "bonus", "total"),
            stretch_columns=("text",),
        )
        for r in mob_confirms:
            raw_time = r.get("elapsed_seconds", "")
            try:
                display_time = self._format_graph_time(float(raw_time))
            except Exception:
                display_time = ""
            result = "CRIT" if str(r.get("confirmed", "")).strip().lower() in ("1", "true", "yes") else "FAILED"
            confirm_tree.insert("", "end", values=(display_time, r.get("roll", ""), r.get("bonus", ""), r.get("total", ""), result, r.get("text", "")))

        # Damage by type.
        damage_types = defaultdict(lambda: {"regular": 0, "sneak": 0, "total": 0, "hits": 0})
        for r in outgoing:
            dtype = r.get("type", "unknown") or "unknown"
            amount = self._as_int(r.get("amount"))
            d = damage_types[dtype]
            if r.get("mode") == "regular":
                d["regular"] += amount
            elif r.get("mode") == "sneak":
                d["sneak"] += amount
            d["total"] += amount
            d["hits"] += 1
        damage_tree = self._detail_tree(
            damage_tab,
            ("type", "regular", "sneak", "total", "share", "hits"),
            ("Damage type", "Regular", "Sneak", "Total", "Share", "Hits"),
            (210, 120, 120, 120, 90, 70),
            numeric_columns=("regular", "sneak", "total", "share", "hits"),
        )
        for dtype, d in sorted(damage_types.items(), key=lambda kv: (-kv[1]["total"], kv[0].casefold())):
            share = 100.0 * d["total"] / total_damage if total_damage else 0.0
            damage_tree.insert("", "end", values=(dtype, dknum(d["regular"]), dknum(d["sneak"]), dknum(d["total"]), f"{share:.1f}%", d["hits"]))

        # DR / Resistance detail.
        if mob_mitigation:
            dr_tree = self._detail_tree(
                dr_tab,
                ("reduction", "type", "dealt", "blocked", "pre", "pct", "affected"),
                ("Reduction", "Damage type", "Dealt", "Blocked", "Before mitigation", "Blocked %", "Affected hits"),
                (190, 135, 110, 110, 140, 95, 105),
                numeric_columns=("dealt", "blocked", "pre", "pct", "affected"),
            )
            for r in sorted(mob_mitigation, key=lambda row: -self._as_int(row.get("blocked"))):
                dr_tree.insert("", "end", values=(
                    r.get("reduction_type", "") or "Unknown DR", r.get("damage_type", ""),
                    dknum(self._as_int(r.get("dealt"))), dknum(self._as_int(r.get("blocked"))),
                    dknum(self._as_int(r.get("pre_mitigation"))), f"{self._as_float(r.get('blocked_percent')):.1f}%",
                    self._as_int(r.get("affected_hits", r.get("hits"))),
                ))
        else:
            ttk.Label(dr_tab, text="No blocked/resisted outgoing damage observed for this target.", style="Section.TLabel").pack(anchor="w")

        # Incoming damage and explicit defensive attack outcomes for this mob.
        incoming_summary = ttk.LabelFrame(incoming_tab, text="Incoming summary", padding=10)
        incoming_summary.pack(fill="x", pady=(0, 10))
        incoming_avg = incoming_total / incoming_hits if incoming_hits else 0.0
        ttk.Label(
            incoming_summary,
            text=f"Damage taken: {incoming_total:,}    Damage events: {incoming_hits:,}    Avg. damage / event: {incoming_avg:,.0f}",
            style="Section.TLabel",
        ).pack(anchor="w")
        if observed_direct_attacks:
            miss_pct = 100.0 * total_defensive_misses / observed_direct_attacks
            ttk.Label(
                incoming_summary,
                text=(f"Observed direct attack outcomes: {observed_direct_attacks:,}    "
                      f"Direct hits: {direct_hits:,}    Explicit misses/avoids: {total_defensive_misses:,} ({miss_pct:.1f}%)"),
                style="Small.TLabel",
            ).pack(anchor="w", pady=(4, 0))

        defense_frame = ttk.LabelFrame(incoming_tab, text="Defensive outcomes from this mob", padding=8)
        defense_frame.pack(fill="x", pady=(0, 10))
        if observed_direct_attacks:
            cols = ("result", "count", "share")
            defense_tree = ttk.Treeview(defense_frame, columns=cols, show="headings", height=7)
            defense_tree.heading("result", text="Outcome")
            defense_tree.heading("count", text="Count")
            defense_tree.heading("share", text="Share")
            defense_tree.column("result", width=220, anchor="w", stretch=False)
            defense_tree.column("count", width=90, anchor="e", stretch=False)
            defense_tree.column("share", width=100, anchor="e", stretch=False)
            defense_tree.pack(anchor="w", fill="x")
            defense_rows_display = [
                ("Direct hit", direct_hits),
                ("Regular miss", defense_counts["regular_miss"]),
                ("Dodge", defense_counts["dodge"]),
                ("Blur", defense_counts["blur"]),
                ("Displacement", defense_counts["displacement"]),
                ("Deflect", defense_counts["deflect"]),
                ("Other miss reason", defense_counts["other"]),
            ]
            for label, count in defense_rows_display:
                share = 100.0 * count / observed_direct_attacks if observed_direct_attacks else 0.0
                defense_tree.insert("", "end", values=(label, count, f"{share:.1f}%"))
            ttk.Label(
                defense_frame,
                text="Share uses direct hits plus explicit 'misses you' lines. Named spell/effect damage is excluded from this denominator.",
                style="Small.TLabel",
            ).pack(anchor="w", pady=(5, 0))
        else:
            ttk.Label(
                defense_frame,
                text="No direct hit/miss avoidance outcomes were recognized for this mob.",
                style="Small.TLabel",
            ).pack(anchor="w")

        if incoming:
            split = ttk.Panedwindow(incoming_tab, orient="horizontal")
            split.pack(fill="both", expand=True)
            by_type_frame = ttk.LabelFrame(split, text="By damage type", padding=8)
            by_source_frame = ttk.LabelFrame(split, text="By source / effect", padding=8)
            split.add(by_type_frame, weight=1)
            split.add(by_source_frame, weight=2)

            in_types = defaultdict(lambda: {"total": 0, "hits": 0})
            in_sources = defaultdict(lambda: {"total": 0, "hits": 0})
            for r in incoming:
                amount = self._as_int(r.get("amount"))
                dt = r.get("type", "unknown") or "unknown"
                src = self._display_source_name(r.get("source", "unknown"))
                in_types[dt]["total"] += amount; in_types[dt]["hits"] += 1
                in_sources[src]["total"] += amount; in_sources[src]["hits"] += 1
            type_tree = self._detail_tree(by_type_frame, ("type", "total", "hits", "avg"), ("Damage type", "Total", "Hits", "Avg."), (170, 110, 65, 90), numeric_columns=("total", "hits", "avg"))
            for name, d in sorted(in_types.items(), key=lambda kv: -kv[1]["total"]):
                type_tree.insert("", "end", values=(name, dknum(d["total"]), d["hits"], dknum(round(d["total"] / d["hits"] if d["hits"] else 0))))
            source_tree = self._detail_tree(by_source_frame, ("source", "total", "hits"), ("Source / effect", "Total", "Hits"), (390, 110, 65), numeric_columns=("total", "hits"), stretch_columns=("source",))
            for name, d in sorted(in_sources.items(), key=lambda kv: -kv[1]["total"]):
                source_tree.insert("", "end", values=(name, dknum(d["total"]), d["hits"]))
        else:
            ttk.Label(incoming_tab, text="No incoming damage attributed to this mob was observed.", style="Section.TLabel").pack(anchor="w")

    def _build_type_table(self):
        self.type_tree = self.make_tree(
            self.type_tab,
            ("type", "regular", "sneak", "total", "hits"),
            ("Damage type", "Regular", "Sneak", "Total", "Hits"),
            (220, 130, 130, 130, 70),
            numeric_columns=("regular", "sneak", "total", "hits"),
        )

    def _build_taken_table(self):
        holder = ttk.Frame(self.taken_tab)
        holder.pack(fill="both", expand=True)
        holder.columnconfigure(0, weight=1)
        holder.columnconfigure(1, weight=1)
        holder.rowconfigure(0, weight=1)

        upper = ttk.LabelFrame(holder, text="By damage type", padding=8)
        upper.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.taken_type_tree = self.make_tree(
            upper,
            ("type", "total", "hits"),
            ("Damage type", "Total", "Hits"),
            (220, 120, 70),
            numeric_columns=("total", "hits"),
        )

        lower = ttk.LabelFrame(holder, text="By source", padding=8)
        lower.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.taken_source_tree = self.make_tree(
            lower,
            ("source", "total", "hits"),
            ("Source", "Total", "Hits"),
            (380, 120, 70),
            numeric_columns=("total", "hits"),
        )


    def _build_mitigation(self):
        self.mitigation_summary_var = tk.StringVar(value="Blocked / resisted outgoing damage: —")
        ttk.Label(
            self.mitigation_tab, textvariable=self.mitigation_summary_var,
            style="Section.TLabel"
        ).pack(anchor="w", pady=(0, 8))
        self.mitigation_tree = self.make_tree(
            self.mitigation_tab,
            ("target", "reduction", "type", "dealt", "blocked", "pre", "pct", "affected"),
            ("Target", "Reduction", "Damage type", "Dealt", "Blocked", "Before mitigation", "Blocked %", "Affected hits"),
            (240, 165, 115, 105, 105, 135, 95, 100),
            numeric_columns=("dealt", "blocked", "pre", "pct", "affected"),
            stretch_columns=(),
        )
        self.mitigation_tree.bind("<Double-1>", lambda event: self._open_target_from_tree(self.mitigation_tree, event))

    def _build_attack_crit(self):
        summary = ttk.LabelFrame(self.attack_tab, text="Attack and critical summary", padding=8)
        summary.pack(fill="x", pady=(0, 8))
        self.attack_total_var = tk.StringVar(value="Attacks: —")
        self.attack_hitmiss_var = tk.StringVar(value="Hits / grazes / misses: —")
        self.attack_crits_var = tk.StringVar(value="Confirmed crits: —")
        self.attack_apm_var = tk.StringVar(value=f"Peak {rolling_window_short(self.graph_window_seconds)} APM: —")
        for var in [self.attack_total_var, self.attack_hitmiss_var, self.attack_crits_var, self.attack_apm_var]:
            ttk.Label(summary, textvariable=var, style="Section.TLabel").pack(side="left", padx=(0, 28))

        ttk.Label(
            self.attack_tab,
            text=(
                "Observed confirm is evidence-based: <=186 means 186 succeeded, but the exact threshold "
                "has not been proven. Target defenses/debuffs may change during combat."
            ),
            style="Small.TLabel", wraplength=1100,
        ).pack(anchor="w", pady=(0, 8))

        detail_tabs = ttk.Notebook(self.attack_tab)
        detail_tabs.pack(fill="both", expand=True)
        by_target_tab = ttk.Frame(detail_tabs, padding=8)
        confirm_tab = ttk.Frame(detail_tabs, padding=8)
        rolls_tab = ttk.Frame(detail_tabs, padding=8)
        detail_tabs.add(by_target_tab, text="By target")
        detail_tabs.add(confirm_tab, text="Confirm rolls")
        detail_tabs.add(rolls_tab, text="d20 distribution")
        self.attack_detail_notebook = detail_tabs

        # By target
        frame = ttk.Frame(by_target_tab)
        frame.pack(fill="both", expand=True)
        cols = (
            "target", "attacks", "hits", "grazes", "misses", "threats", "crits", "failed",
            "grazepct", "critpct", "confpct", "highfail", "lowsuccess", "observed"
        )
        self.attack_summary_tree = ttk.Treeview(frame, columns=cols, show="headings")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.attack_summary_tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.attack_summary_tree.xview)
        self.attack_summary_tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.attack_summary_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        headings = {
            "target": "Target", "attacks": "Attacks", "hits": "Hits", "grazes": "Grazes", "misses": "Misses",
            "threats": "Threats", "crits": "Crits", "failed": "Failed",
            "grazepct": "Graze %", "critpct": "Crit %", "confpct": "Conf. %",
            "highfail": "High fail", "lowsuccess": "Low success", "observed": "Observed confirm",
        }
        widths = {
            "target": 220, "attacks": 70, "hits": 60, "grazes": 70, "misses": 70, "threats": 75,
            "crits": 60, "failed": 70, "grazepct": 75, "critpct": 70, "confpct": 75,
            "highfail": 80, "lowsuccess": 85, "observed": 145,
        }
        for c in cols:
            self.attack_summary_tree.heading(c, text=headings[c])
            self.attack_summary_tree.column(
                c, width=widths[c], anchor="w" if c in ("target", "observed") else "e", stretch=False
            )
        self._enable_tree_sorting(
            self.attack_summary_tree,
            numeric_columns=("attacks", "hits", "grazes", "misses", "threats", "crits", "failed",
                             "grazepct", "critpct", "confpct", "highfail", "lowsuccess"),
        )
        self.attack_summary_tree.bind("<Double-1>", lambda event: self._open_target_from_tree(self.attack_summary_tree, event))

        # Critical confirmation rolls
        f2 = ttk.Frame(confirm_tab)
        f2.pack(fill="both", expand=True)
        cols2 = ("time", "target", "roll", "bonus", "total", "result", "text")
        self.confirm_tree = ttk.Treeview(f2, columns=cols2, show="headings")
        ys2 = ttk.Scrollbar(f2, orient="vertical", command=self.confirm_tree.yview)
        xs2 = ttk.Scrollbar(f2, orient="horizontal", command=self.confirm_tree.xview)
        self.confirm_tree.configure(yscrollcommand=ys2.set, xscrollcommand=xs2.set)
        self.confirm_tree.grid(row=0, column=0, sticky="nsew")
        ys2.grid(row=0, column=1, sticky="ns")
        xs2.grid(row=1, column=0, sticky="ew")
        f2.rowconfigure(0, weight=1); f2.columnconfigure(0, weight=1)
        heads2 = {
            "time": "Time", "target": "Target", "roll": "d20", "bonus": "Bonus",
            "total": "Total", "result": "Result", "text": "Original OCR text"
        }
        widths2 = {"time": 70, "target": 220, "roll": 55, "bonus": 65, "total": 65, "result": 90, "text": 560}
        for c in cols2:
            self.confirm_tree.heading(c, text=heads2[c])
            self.confirm_tree.column(
                c, width=widths2[c], anchor="e" if c in ("time", "roll", "bonus", "total") else "w",
                stretch=(c == "text")
            )
        self._enable_tree_sorting(self.confirm_tree, numeric_columns=("time", "roll", "bonus", "total"))

        # d20 distribution
        f3 = ttk.Frame(rolls_tab)
        f3.pack(fill="both", expand=True)
        cols3 = ("target", "roll", "miss", "graze", "hit", "threat", "other", "total")
        self.roll_distribution_tree = ttk.Treeview(f3, columns=cols3, show="headings")
        ys3 = ttk.Scrollbar(f3, orient="vertical", command=self.roll_distribution_tree.yview)
        self.roll_distribution_tree.configure(yscrollcommand=ys3.set)
        self.roll_distribution_tree.grid(row=0, column=0, sticky="nsew")
        ys3.grid(row=0, column=1, sticky="ns")
        f3.rowconfigure(0, weight=1); f3.columnconfigure(0, weight=1)
        labels3 = {"target":"Target", "roll":"d20", "miss":"Miss", "graze":"Graze", "hit":"Hit", "threat":"Crit threat", "other":"Other", "total":"Total"}
        for c in cols3:
            self.roll_distribution_tree.heading(c, text=labels3[c])
            self.roll_distribution_tree.column(c, width=220 if c == "target" else 80, anchor="w" if c == "target" else "e", stretch=False)
        self._enable_tree_sorting(
            self.roll_distribution_tree,
            numeric_columns=("roll", "miss", "graze", "hit", "threat", "other", "total"),
        )

    def _build_damage_log(self):
        ttk.Label(
            self.log_tab,
            text="Every parsed combat event. Select a row to see the full original OCR text below.",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        frame = ttk.Frame(self.log_tab)
        frame.pack(fill="both", expand=True)
        cols = ("line", "time", "event", "source", "target", "mode", "type", "amount", "blocked")
        self.damage_log_tree = ttk.Treeview(frame, columns=cols, show="headings")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.damage_log_tree.yview)
        self.damage_log_tree.configure(yscrollcommand=yscroll.set)
        self.damage_log_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        headings = {
            "line": "Line", "time": "Time", "event": "Event", "source": "Source", "target": "Target",
            "mode": "Mode", "type": "Type", "amount": "Amount", "blocked": "Blocked",
        }
        widths = {
            "line": 55, "time": 70, "event": 110, "source": 180, "target": 220,
            "mode": 75, "type": 100, "amount": 95, "blocked": 95,
        }
        for c in cols:
            self.damage_log_tree.heading(c, text=headings[c])
            self.damage_log_tree.column(
                c, width=widths[c], anchor="e" if c in ("line", "time", "amount", "blocked") else "w", stretch=False
            )
        self._enable_tree_sorting(
            self.damage_log_tree,
            numeric_columns=("line", "time", "amount", "blocked"),
        )

        detail = ttk.LabelFrame(self.log_tab, text="Selected OCR text", padding=6)
        detail.pack(fill="x", pady=(8, 0))
        self.damage_log_detail = tk.Text(detail, height=3, wrap="word", font=("Consolas", 9))
        self.damage_log_detail.pack(fill="x", expand=True)
        self.damage_log_detail.configure(state="disabled")
        self.damage_log_text_by_item = {}

        def show_selected(_event=None):
            selected = self.damage_log_tree.selection()
            text = self.damage_log_text_by_item.get(selected[0], "") if selected else ""
            self.damage_log_detail.configure(state="normal")
            self.damage_log_detail.delete("1.0", "end")
            self.damage_log_detail.insert("1.0", text)
            self.damage_log_detail.configure(state="disabled")

        self.damage_log_tree.bind("<<TreeviewSelect>>", show_selected)

    def _build_diagnostics(self):
        ttk.Label(
            self.diag_tab,
            text="Technical summary for troubleshooting OCR and reconstruction.",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        self.diag_text = tk.Text(
            self.diag_tab,
            wrap="word",
            height=20,
            font=("Consolas", 10),
        )
        self.diag_text.pack(fill="both", expand=True)
        self.diag_text.insert("1.0", "Session summary will appear after analysis.")
        self.diag_text.config(state="disabled")

    def _build_dps_graph(self):
        self.dps_points = []
        self.live_dps_points = []
        self.dps_peak_var = tk.StringVar(value=f"Peak post {rolling_window_short(self.graph_window_seconds)} DPS: —")
        self.dps_final_var = tk.StringVar(value=f"Final post {rolling_window_short(self.graph_window_seconds)} DPS: —")
        self.dps_title_var = tk.StringVar(value=f"Rolling {rolling_window_label(self.graph_window_seconds)} DPS")
        self.dps_compare_var = tk.StringVar(value="Live comparison: —")

        top = ttk.Frame(self.dps_tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(
            top,
            textvariable=self.dps_title_var,
            style="Section.TLabel",
        ).pack(side="left")
        ttk.Label(
            top,
            textvariable=self.dps_peak_var,
            style="Section.TLabel",
        ).pack(side="right")
        ttk.Label(
            top,
            textvariable=self.dps_final_var,
            style="Section.TLabel",
        ).pack(side="right", padx=(0, 24))

        ttk.Label(
            self.dps_tab,
            textvariable=self.dps_compare_var,
            style="Small.TLabel",
        ).pack(anchor="w", pady=(0, 3))

        ttk.Label(
            self.dps_tab,
            text=(
                "Post-analysis DPS is calculated from the reconstructed Combat Log "
                "and should generally be more accurate. Live DPS is an approximate "
                "OCR estimate shown during combat."
            ),
            style="Small.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        self.dps_canvas = tk.Canvas(
            self.dps_tab,
            height=390,
            background="white",
            highlightthickness=1,
            highlightbackground="#b7b7b7",
        )
        self.dps_canvas.pack(fill="both", expand=True)
        self.dps_canvas.bind("<Configure>", lambda _e: self.draw_dps_graph())

    @staticmethod
    def _format_graph_time(seconds):
        seconds = max(0.0, float(seconds))
        minutes = int(seconds // 60)
        secs = int(round(seconds - minutes * 60))
        if secs == 60:
            minutes += 1
            secs = 0
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def _format_graph_number(value):
        value = float(value)
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if abs(value) >= 1_000:
            return f"{value / 1_000:.0f}k"
        return f"{value:.0f}"

    def draw_dps_graph(self):
        if not hasattr(self, "dps_canvas"):
            return

        c = self.dps_canvas
        c.delete("all")

        w = max(320, c.winfo_width())
        h = max(220, c.winfo_height())

        left = 72
        right = 24
        top = 34
        bottom = 48
        plot_w = max(1, w - left - right)
        plot_h = max(1, h - top - bottom)

        c.create_rectangle(
            left, top, left + plot_w, top + plot_h,
            fill="white", outline="#d0d0d0"
        )

        if not self.dps_points:
            c.create_text(
                w / 2, h / 2,
                text="DPS graph is available after a completed analysis.",
                fill="#666666",
                font=("Segoe UI", 11),
            )
            return

        all_points = list(self.dps_points) + list(self.live_dps_points)
        max_t = max(float(x) for x, _ in all_points)
        max_y = max(float(y) for _, y in all_points)
        if max_t <= 0:
            max_t = 1.0
        if max_y <= 0:
            max_y = 1.0
        y_top = max_y * 1.08

        for i in range(5):
            frac = i / 4
            y_value = y_top * (1 - frac)
            py = top + plot_h * frac
            c.create_line(left, py, left + plot_w, py, fill="#e6e6e6")
            c.create_text(
                left - 9, py,
                text=self._format_graph_number(y_value),
                anchor="e",
                fill="#555555",
                font=("Segoe UI", 9),
            )

        for i in range(5):
            frac = i / 4
            t = max_t * frac
            px = left + plot_w * frac
            c.create_line(px, top, px, top + plot_h, fill="#eeeeee")
            c.create_text(
                px, top + plot_h + 18,
                text=self._format_graph_time(t),
                anchor="n",
                fill="#555555",
                font=("Segoe UI", 9),
            )

        def draw_series(points, color, width=2):
            coords = []
            for t, dps in points:
                x = left + (float(t) / max_t) * plot_w
                y = top + plot_h - (float(dps) / y_top) * plot_h
                coords.extend((x, y))
            if len(coords) >= 4:
                c.create_line(*coords, fill=color, width=width, smooth=False)
            elif len(coords) == 2:
                x, y = coords
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")

        # Blue = post-analysis; orange = live estimate.
        draw_series(self.dps_points, "#2368a2", 2)
        if self.live_dps_points:
            draw_series(self.live_dps_points, "#d97706", 2)

        # Legend
        legend_x = left + 10
        legend_y = 14
        c.create_line(legend_x, legend_y, legend_x + 24, legend_y, fill="#2368a2", width=3)
        c.create_text(
            legend_x + 30, legend_y,
            text="Post-analysis",
            anchor="w",
            fill="#333333",
            font=("Segoe UI", 9),
        )
        if self.live_dps_points:
            lx = legend_x + 135
            c.create_line(lx, legend_y, lx + 24, legend_y, fill="#d97706", width=3)
            c.create_text(
                lx + 30, legend_y,
                text="Live estimate",
                anchor="w",
                fill="#333333",
                font=("Segoe UI", 9),
            )

        c.create_text(
            left + plot_w / 2,
            h - 7,
            text="Elapsed fight time",
            anchor="s",
            fill="#333333",
            font=("Segoe UI", 9, "bold"),
        )
        c.create_text(
            14,
            top + plot_h / 2,
            text=f"{rolling_window_short(self.graph_window_seconds)} DPS",
            angle=90,
            fill="#333333",
            font=("Segoe UI", 9, "bold"),
        )

    def _nearest_post_dps(self, t):
        if not self.dps_points:
            return None
        xs = [x for x, _ in self.dps_points]
        i = bisect.bisect_left(xs, t)
        candidates = []
        if i < len(self.dps_points):
            candidates.append(self.dps_points[i])
        if i > 0:
            candidates.append(self.dps_points[i - 1])
        if not candidates:
            return None
        return min(candidates, key=lambda p: abs(p[0] - t))[1]

    def load_dps_graph(self, session):
        self.dps_points = []
        self.live_dps_points = []
        post_path = session / "dps_rolling.csv"
        if not post_path.exists():
            post_path = session / "dps_15s.csv"
        live_path = session / "live_dps.csv"
        self.graph_window_seconds = self.get_session_rolling_window(session)
        window_short = rolling_window_short(self.graph_window_seconds)
        window_label = rolling_window_label(self.graph_window_seconds)
        self.dps_title_var.set(f"Rolling {window_label} DPS")

        first_capture_time = None
        post_max_t = None

        if post_path.exists():
            try:
                with post_path.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        try:
                            t = float(row.get("elapsed_seconds", ""))
                            capture_t = float(row.get("capture_elapsed_seconds", ""))
                            dps = float(row.get("rolling_dps", row.get("rolling_15s_dps", "")))
                        except Exception:
                            continue
                        if first_capture_time is None:
                            first_capture_time = capture_t - t
                        self.dps_points.append((t, dps))
            except Exception:
                self.dps_points = []

        if self.dps_points:
            post_max_t = self.dps_points[-1][0]

        if live_path.exists() and first_capture_time is not None and post_max_t is not None:
            try:
                with live_path.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        try:
                            capture_t = float(row.get("capture_elapsed_seconds", ""))
                            dps = float(row.get("live_rolling_dps", row.get("live_15s_dps", "")))
                        except Exception:
                            continue
                        t = capture_t - first_capture_time
                        if -0.15 <= t <= post_max_t + 0.15:
                            self.live_dps_points.append((max(0.0, t), dps))
            except Exception:
                self.live_dps_points = []

        if self.dps_points:
            self.notebook.add(self.dps_tab, text="DPS graph")
            peak = max(d for _, d in self.dps_points)
            final = self.dps_points[-1][1]
            self.dps_peak_var.set(f"Peak post {window_short} DPS: {dknum(round(peak))}")
            self.dps_final_var.set(f"Final post {window_short} DPS: {dknum(round(final))}")

            if self.live_dps_points:
                errors = []
                pct_errors = []
                for t, live in self.live_dps_points:
                    post = self._nearest_post_dps(t)
                    if post is None:
                        continue
                    errors.append(live - post)
                    if post > 1.0:
                        pct_errors.append((live - post) / post * 100.0)

                if pct_errors:
                    bias = sum(pct_errors) / len(pct_errors)
                    mae = sum(abs(x) for x in pct_errors) / len(pct_errors)
                    max_err = max(abs(x) for x in pct_errors)
                    within5 = 100.0 * sum(abs(x) <= 5.0 for x in pct_errors) / len(pct_errors)
                    self.dps_compare_var.set(
                        "Live vs post: "
                        f"mean bias {bias:+.1f}%  •  "
                        f"mean abs error {mae:.1f}%  •  "
                        f"max {max_err:.1f}%  •  "
                        f"within ±5% {within5:.0f}%"
                    )
                else:
                    self.dps_compare_var.set("Live comparison: not enough overlapping data.")
            else:
                self.dps_compare_var.set("Live comparison: no live estimate data available.")

            self.after_idle(self.draw_dps_graph)
        else:
            try:
                self.notebook.hide(self.dps_tab)
            except Exception:
                pass
            self.dps_peak_var.set(f"Peak post {window_short} DPS: —")
            self.dps_final_var.set(f"Final post {window_short} DPS: —")
            self.dps_compare_var.set("Live comparison: —")

    def get_session_rolling_window(self, session):
        try:
            summary = json.loads((session / "analysis_summary.json").read_text(encoding="utf-8"))
            value = summary.get("rolling_window_seconds")
            if value is not None:
                return max(1.0, float(value))
        except Exception:
            pass
        try:
            cfg = json.loads((session / "config_used.json").read_text(encoding="utf-8"))
            return max(1.0, float(cfg.get("live_dps", {}).get("rolling_window_seconds", 15.0)))
        except Exception:
            return 15.0

    def show_detailed_results(self):
        self.notebook.select(self.log_tab)
        self.damage_log_tree.focus_set()

    # -------------------------- state / messages ---------------------------

    def bring_to_front(self):
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.focus_force()
            self.after(250, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    def set_status(self, text, color="#777777"):
        self.status_label.config(text=text)
        self.status_dot.itemconfig(self.status_circle, fill=color)

    def set_busy(self, busy, text=None):
        if busy:
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
        if text is not None:
            self.progress_text.config(text=text)

    def set_analysis_progress(self, stage, current=0, total=1, extra=""):
        total = max(1, int(total or 1))
        current = max(0, min(int(current or 0), total))
        ratio = current / total

        if stage == "PREPARE":
            value = 2
            text = "Step 1/4 - Preparing analysis..."
        elif stage == "RECONSTRUCT":
            value = 4 + 26 * ratio
            suffix = f" - {extra} lines found" if extra else ""
            text = f"Step 2/4 - Reconstructing Combat Log: {current}/{total} frames{suffix}"
        elif stage == "OCR":
            value = 30 + 42 * ratio
            text = f"Step 3/4 - Indra OCR: {current}/{total} lines"
        elif stage == "PARSE":
            value = 97
            text = "Step 4/4 - Parsing combat results..."
        elif stage == "DONE":
            value = 100
            text = "Analysis complete"
        else:
            return

        self.progress.stop()
        self.progress.config(mode="determinate", maximum=100, value=value)
        self.progress_text.config(text=text)

    def refresh_capture_label(self):
        cap, configured = read_capture_area()
        if configured and cap:
            label = (
                f"Combat log area: {cap.get('width','?')}×{cap.get('height','?')} px "
                f"at ({cap.get('left','?')}, {cap.get('top','?')})  ✓"
            )
            self.capture_label.config(text=label)
        else:
            self.capture_label.config(text="Combat log area is not configured")

    def refresh_system_status(self):
        st = dependency_status()
        missing = []
        if st["missing_python"]:
            missing.append("Python packages")
        if not st["ffmpeg"]:
            missing.append("FFmpeg")

        if missing:
            self.system_label.config(text="Missing: " + ", ".join(missing) + " — run SETUP")
        else:
            self.system_label.config(text="System: ready ✓")

    def show_system_check(self):
        st = dependency_status()
        lines = []
        lines.append(
            "Python packages: "
            + ("OK" if not st["missing_python"] else "Missing " + ", ".join(st["missing_python"]))
        )
        lines.append("FFmpeg: " + (st["ffmpeg"] or "NOT FOUND"))

        if st["missing_python"]:
            ans = messagebox.askyesno(
                "System check",
                "\n".join(lines)
                + "\n\nWould you like to install the missing Python packages automatically?"
            )
            if ans:
                self.install_python_dependencies()
        else:
            messagebox.showinfo("System check", "\n".join(lines))

    def install_python_dependencies(self):
        if self.proc is not None:
            return
        self.set_status("Installing ...", "#c98b16")
        self.set_busy(True, "Installing Python packages ...")
        packages = ["mss", "opencv-python", "numpy", "indra-ocr"]
        if sys.platform.startswith("linux"):
            packages.append("python-xlib")
        cmd = [sys.executable, "-m", "pip", "install", *packages]
        self.start_subprocess(cmd, "install", stdin_pipe=False)

    # -------------------------- continuous live / hotkey ---------------------

    def configure_global_hotkey(self):
        try:
            self.hotkey_manager.start(self.hotkey_var.get())
        except Exception as e:
            self.progress_text.config(text=f"Hotkey unavailable: {e}")

    def poll_hotkey_event(self):
        if self.hotkey_event.is_set():
            self.hotkey_event.clear()
            self.toggle_recording_hotkey()
        self.after(100, self.poll_hotkey_event)

    def toggle_recording_hotkey(self):
        # The recording toggle is intended to be used while playing. Ignore a
        # global hotkey press when DDO is not the foreground window so Pause or
        # a user-selected fallback cannot accidentally start a session elsewhere.
        if not is_ddo_foreground():
            return
        if not self.continuous_live_var.get():
            self._beep(700, 100)
            return
        if self.proc_kind == "record":
            self.stop_recording(from_hotkey=True)
        elif self.proc is None:
            self.start_recording(from_hotkey=True)
        else:
            # First version deliberately does not start a new capture while a
            # prior session is still being analyzed.
            self._beep(600, 120)

    def _beep(self, frequency=1000, duration=120, repeat=1):
        def worker():
            if os.name == "nt":
                try:
                    import winsound
                    for i in range(max(1, int(repeat))):
                        winsound.Beep(int(frequency), int(duration))
                        if i + 1 < repeat:
                            time.sleep(0.08)
                except Exception:
                    pass
            elif sys.platform.startswith("linux"):
                try:
                    linux_beep(repeat=repeat)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def send_live_command(self, command):
        proc = self.live_proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            if proc.stdin:
                proc.stdin.write(command.rstrip("\n") + "\n")
                proc.stdin.flush()
                return True
        except Exception:
            pass
        return False

    def monitor_continuous_live(self):
        if self.continuous_live_var.get() and not self.suppress_continuous_live:
            if self.live_proc is None or self.live_proc.poll() is not None:
                self.live_proc = None
                self.ensure_continuous_live()
        self.after(2000, self.monitor_continuous_live)

    def ensure_continuous_live(self):
        if not self.continuous_live_var.get() or self.suppress_continuous_live:
            return
        if self.live_proc is not None and self.live_proc.poll() is None:
            return
        st = dependency_status()
        cap, configured = read_capture_area()
        if st["missing_python"] or not configured or not cap or not find_ddo_window():
            return
        try:
            cmd = [sys.executable, str(HERE / "continuous_live.py"), "--parent-pid", str(os.getpid())]
            self.live_proc = subprocess.Popen(
                cmd, cwd=str(HERE), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            self.live_proc = None

    def stop_continuous_live(self):
        if self.live_proc is not None:
            self.send_live_command("SHUTDOWN")
            try:
                self.live_proc.wait(timeout=3)
            except Exception:
                try:
                    self.live_proc.terminate()
                except Exception:
                    pass
            self.live_proc = None

    def on_continuous_live_changed(self):
        enabled = bool(self.continuous_live_var.get())
        save_live_user_settings(continuous_mode=enabled)
        if enabled:
            self.ensure_continuous_live()
        else:
            self.stop_continuous_live()

    def on_hotkey_changed(self, _event=None):
        save_live_user_settings(recording_hotkey=self.hotkey_var.get())
        self.configure_global_hotkey()

    def on_overlay_position_changed(self, _event=None):
        key = OVERLAY_POSITION_CHOICES.get(self.overlay_position_var.get(), "custom")
        save_live_user_settings(overlay_position=key)
        self.send_live_command("RELOAD_CONFIG")

    def on_rolling_window_changed(self, _event=None):
        if self.proc is not None:
            return
        try:
            seconds = float(self.rolling_window_var.get().lower().replace("s", "").strip())
        except Exception:
            seconds = 15.0
            self.rolling_window_var.set("15 s")
        save_live_user_settings(rolling_window_seconds=seconds)
        # Keep an already displayed session graph labelled with the window that
        # was actually used for that session. Only update the empty/default graph.
        if not self.dps_points:
            self.graph_window_seconds = seconds
            self.dps_title_var.set(f"Rolling {rolling_window_label(seconds)} DPS")
            self.dps_peak_var.set(f"Peak post {rolling_window_short(seconds)} DPS: —")
            self.dps_final_var.set(f"Final post {rolling_window_short(seconds)} DPS: —")
            self.attack_apm_var.set(f"Peak {rolling_window_short(seconds)} APM: —")
        self.send_live_command("RELOAD_CONFIG")
        self.progress_text.config(text=f"Rolling window: {rolling_window_short(seconds)}")

    def on_overlay_font_size_changed(self, _event=None):
        if self.proc is not None:
            return
        try:
            size = int(self.overlay_font_size_var.get())
        except Exception:
            size = 24
            self.overlay_font_size_var.set("24")
        if str(size) not in FONT_SIZE_CHOICES:
            size = 24
            self.overlay_font_size_var.set("24")
        save_live_user_settings(font_size=size)
        self.send_live_command("RELOAD_CONFIG")
        self.progress_text.config(text=f"Overlay font: {size}")

    def on_ocr_threads_changed(self, _event=None):
        if self.proc is not None:
            return
        selected = self.ocr_threads_var.get().strip()
        if selected not in OCR_THREAD_CHOICES:
            selected = "Auto"
            self.ocr_threads_var.set(selected)
        setting = "auto" if selected == "Auto" else int(selected)
        save_live_user_settings(indra_threads=setting)
        effective = resolve_indra_threads(setting)

        # Indra fixes its worker count when the model is loaded. Restart the
        # persistent live helper so the new selection takes effect immediately.
        if self.continuous_live_var.get():
            self.stop_continuous_live()
            self.after(250, self.ensure_continuous_live)

        if selected == "Auto":
            self.progress_text.config(
                text=f"OCR threads: Auto ({effective}; {self.detected_physical_cores} physical cores detected)"
            )
        else:
            self.progress_text.config(text=f"OCR threads: {effective}")

    def position_overlay(self):
        if self.proc is not None:
            return
        if not find_ddo_window():
            if self.setup_chain_active:
                self.setup_chain_active = False
                self.bring_to_front()
                self.enable_idle_controls()
                self.set_status("Ready", "#2e8b57")
                if self.continuous_live_var.get():
                    self.after(250, self.ensure_continuous_live)
            messagebox.showerror("DDO not found", "Start DDO before positioning the overlay.")
            return
        # Placement is done on top of DDO. The helper uses a no-activate window
        # so DDO remains foreground while the overlay is dragged.
        focus_ddo()
        cmd = [sys.executable, str(HERE / "overlay_positioner.py")]
        self.start_subprocess(cmd, "position", stdin_pipe=False)

    def session_dps(self, session):
        if session is None:
            return None
        summary_json = session / "analysis_summary.json"
        try:
            data = json.loads(summary_json.read_text(encoding="utf-8"))
            if "session_dps" in data:
                value = data.get("session_dps")
                return float(value) if value is not None else None
            value = data.get("elapsed_dps")  # older sessions
            if value is not None:
                return float(value)
        except Exception:
            pass
        try:
            total = 0
            with (session / "damage_events.csv").open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("event") == "damage_dealt":
                        total += int(row.get("amount", "0") or 0)
            duration = None
            for line in (session / "recording_summary.txt").read_text(encoding="utf-8").splitlines():
                if line.startswith("Elapsed seconds:"):
                    duration = float(line.split(":", 1)[1].strip())
                    break
            if duration and duration > 0:
                return total / duration
        except Exception:
            pass
        return None

    # -------------------------- subprocess engine --------------------------

    def start_subprocess(self, cmd, kind, stdin_pipe=False):
        if self.proc is not None:
            raise RuntimeError("A process is already running.")

        kwargs = dict(
            cwd=str(HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=(
                (CREATE_NO_WINDOW | (0x00004000 if kind in ("analyze", "replay") else 0))
                if os.name == "nt" else 0
            ),
        )
        if stdin_pipe:
            kwargs["stdin"] = subprocess.PIPE
        else:
            kwargs["stdin"] = subprocess.DEVNULL

        if kind == "analyze":
            self.analysis_output_lines = []
        elif kind == "replay":
            self.replay_output_lines = []

        self.proc = subprocess.Popen(cmd, **kwargs)
        self.proc_kind = kind

        def reader(proc, proc_kind):
            try:
                for line in proc.stdout:
                    self.output_queue.put(("line", proc_kind, line.rstrip()))
            finally:
                code = proc.wait()
                self.output_queue.put(("done", proc_kind, code))

        threading.Thread(target=reader, args=(self.proc, kind), daemon=True).start()

    def poll_output(self):
        try:
            while True:
                item = self.output_queue.get_nowait()
                kind = item[0]
                if kind == "line":
                    _, proc_kind, line = item
                    self.handle_process_line(proc_kind, line)
                elif kind == "done":
                    _, proc_kind, code = item
                    self.handle_process_done(proc_kind, code)
        except queue.Empty:
            pass
        self.after(120, self.poll_output)

    def handle_process_line(self, kind, line):
        if not line:
            return

        if kind == "analyze":
            try:
                self.analysis_output_lines.append(line)
            except Exception:
                self.analysis_output_lines = [line]
        elif kind == "replay":
            try:
                self.replay_output_lines.append(line)
            except Exception:
                self.replay_output_lines = [line]

        if kind == "record":
            if line.startswith("SESSION_DIR|"):
                self.recording_session = Path(line.split("|", 1)[1].strip())
            elif line == "RECORDER_READY":
                if self.recording_started_by_hotkey:
                    self.progress_text.config(text="Recorder ready - starting in background...")
                    self.after(10, self.begin_capture_after_focus)
                else:
                    self.progress_text.config(text="Recorder ready - switching to DDO...")
                    self.iconify()
                    focus_ddo()
                    self.after(35, self.begin_capture_after_focus)
            elif line == "CAPTURE_STARTED":
                if self.continuous_live_var.get():
                    self.ensure_continuous_live()
                    if self.recording_live_boundary_epoch is not None:
                        self.send_live_command(f"SESSION_BOUNDARY|{self.recording_live_boundary_epoch:.6f}")
                    if self.recording_session is not None:
                        self.send_live_command(f"START_SESSION|{self.recording_session}")
                else:
                    self.launch_live_estimator()
                self._beep(1250, 90)
                self.stop_btn.config(state="normal")
                self.set_status("Recording", "#b22222")
                self.set_busy(False, "Recording - capture is active")
            elif line.startswith("FOCUS_PAUSED|"):
                self.set_status("Recording paused - DDO not focused", "#c98b16")
                self.progress_text.config(text="Capture paused while DDO is not the foreground window.")
            elif line.startswith("FOCUS_RESUMED|"):
                self.set_status("Recording", "#b22222")
                self.progress_text.config(text="DDO focus restored - capture resumed.")
            elif "fps" in line and "kept" in line:
                self.progress_text.config(text=line)
            elif "Recording saved:" in line:
                self.progress_text.config(text="Recording saved")
        elif kind == "analyze":
            if line.startswith("PROGRESS|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    stage = parts[1]
                    current = parts[2]
                    total = parts[3]
                    extra = parts[4] if len(parts) >= 5 else ""
                    self.set_analysis_progress(stage, current, total, extra)
            elif "Visual reconstruction complete" in line:
                self.progress_text.config(text=line.strip())
        elif kind == "replay":
            if line.startswith("REPLAY_PROGRESS|"):
                parts = line.split("|")
                stage = parts[1] if len(parts) > 1 else ""
                if stage == "FRAMES" and len(parts) >= 5:
                    try:
                        current = int(parts[2]); total = max(1, int(parts[3])); lines = int(parts[4])
                        value = 5 + 55 * current / total
                        self.progress.stop()
                        self.progress.config(mode="determinate", maximum=100, value=value)
                        self.progress_text.config(
                            text=f"Replay live: reconstructing {current}/{total} saved frames - {lines} lines"
                        )
                    except Exception:
                        pass
                elif stage == "OCR" and len(parts) >= 5:
                    try:
                        done = int(parts[2]); detected = max(1, int(parts[3])); parsed = int(parts[4])
                        value = 60 + 35 * min(1.0, done / detected)
                        self.progress.stop()
                        self.progress.config(mode="determinate", maximum=100, value=value)
                        self.progress_text.config(
                            text=f"Replay live: Indra OCR {done}/{detected} lines - {parsed} damage events"
                        )
                    except Exception:
                        pass
                elif stage == "WAIT_OCR":
                    self.progress_text.config(text="Replay live: waiting for OCR queue to finish ...")
                elif stage == "DONE":
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=100, value=100)
                    self.progress_text.config(text="Replay live test complete")
            elif line.startswith("REPLAY_ERROR|"):
                self.progress_text.config(text=line.split("|", 1)[1])
        elif kind == "select":
            if line.startswith("ERROR:"):
                self.progress_text.config(text=line)
        elif kind == "install":
            if "Successfully installed" in line:
                self.progress_text.config(text="Python packages installed")

    def launch_live_estimator(self):
        if self.recording_session is None:
            return

        cfg = {}
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
        if not bool(cfg.get("live_dps", {}).get("enabled", True)):
            return

        try:
            stop_flag = self.recording_session / "live_stop.flag"
            if stop_flag.exists():
                stop_flag.unlink()
        except Exception:
            pass

        try:
            log_path = self.recording_session / "live_overlay_stderr.log"
            self.live_log_handle = log_path.open("w", encoding="utf-8")
            cmd = [
                sys.executable,
                str(HERE / "live_overlay.py"),
                "--session",
                str(self.recording_session),
                "--parent-pid",
                str(os.getpid()),
            ]
            self.live_proc = subprocess.Popen(
                cmd,
                cwd=str(HERE),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self.live_log_handle,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            self.live_proc = None
            try:
                if self.live_log_handle:
                    self.live_log_handle.close()
            except Exception:
                pass
            self.live_log_handle = None

    def stop_live_estimator(self):
        if self.recording_session is not None:
            try:
                (self.recording_session / "live_stop.flag").write_text(
                    "stop\n", encoding="utf-8"
                )
            except Exception:
                pass

        # Give the live process a short chance to close its CSV cleanly.
        if self.live_proc is not None:
            self.after(10000, self.force_stop_live_estimator)

    def force_stop_live_estimator(self):
        if self.live_proc is not None:
            try:
                if self.live_proc.poll() is None:
                    self.live_proc.terminate()
            except Exception:
                pass
            self.live_proc = None

        if self.live_log_handle is not None:
            try:
                self.live_log_handle.close()
            except Exception:
                pass
            self.live_log_handle = None

    def begin_capture_after_focus(self):
        if self.proc is None or self.proc_kind != "record":
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.write("START\n")
                self.proc.stdin.flush()
                self.start_time = time.monotonic()
                self.update_timer()
        except Exception as e:
            if not self.recording_started_by_hotkey:
                self.bring_to_front()
                messagebox.showerror("Recording startup error", str(e))
            else:
                self._beep(500, 180)
                self.set_status("Recording startup error", "#b22222")

    def handle_process_done(self, kind, code):
        proc = self.proc
        self.proc = None
        self.proc_kind = None

        if kind == "select":
            self.refresh_capture_label()

            if code == 0 and self.setup_chain_active:
                # First-run setup continues directly into overlay placement. Keep
                # the analyzer hidden so DDO remains the visual context.
                self.set_busy(False, "Combat Log area saved - positioning overlay ...")
                self.set_status("Position overlay", "#c98b16")
                self.after(250, self.position_overlay)
                return

            self.setup_chain_active = False
            self.bring_to_front()
            self.set_busy(False, "Ready")
            self.set_status("Ready", "#2e8b57")
            self.enable_idle_controls()
            if self.continuous_live_var.get():
                self.after(300, self.ensure_continuous_live)
            if code != 0:
                messagebox.showwarning(
                    "Combat log area",
                    "The combat log area was not changed. Make sure DDO is running and visible."
                )

        elif kind == "position":
            self.set_busy(False, "Ready")
            live_settings = read_live_user_settings()
            self.overlay_position_var.set(OVERLAY_POSITION_LABELS.get(live_settings.get("position", "custom"), "Custom"))
            self.send_live_command("RELOAD_CONFIG")
            if self.setup_chain_active:
                self.setup_chain_active = False
                self.set_status("Ready", "#2e8b57")
                self.enable_idle_controls()
                if self.continuous_live_var.get():
                    self.after(250, self.ensure_continuous_live)
                if code == 0:
                    self.progress_text.config(text="Setup complete")
                else:
                    self.progress_text.config(text="Overlay placement was not saved")
                self.after(120, self.bring_to_front)
                if code != 0:
                    self.after(250, lambda: messagebox.showwarning(
                        "Overlay placement",
                        "The overlay position was not saved. You can use Position overlay later."
                    ))

        elif kind == "record":
            self.stop_timer()
            if self.continuous_live_var.get():
                self.send_live_command("FLUSH_SESSION")
            else:
                self.stop_live_estimator()
            if code == 0:
                self.set_status("Analyzing ...", "#c98b16")
                self.set_busy(True, "Reconstructing combat log ...")
                self.start_analysis()
            else:
                if not self.analysis_background:
                    self.deiconify()
                    self.lift()
                self.set_status("Recording error", "#b22222")
                self.set_busy(False, "Recording failed")
                self.enable_idle_controls()
                self._beep(500, 180)
                if not self.analysis_background:
                    messagebox.showerror(
                        "Recording error",
                        "The recording did not finish correctly. Check ffmpeg.log in the session folder."
                    )

        elif kind == "analyze":
            background = bool(self.analysis_background)
            if not background:
                self.bring_to_front()
            self.set_busy(False, "Analysis complete" if code == 0 else "Analysis failed")
            self.enable_idle_controls()
            if code == 0:
                self.set_status("Complete", "#2e8b57")
                self.current_session = latest_session()
                self.load_results(self.current_session)
                if not background:
                    self.notebook.select(self.overview_tab)
                result_dps = self.session_dps(self.current_session)
                if result_dps is not None and self.continuous_live_var.get():
                    self.send_live_command(f"RESULT|{result_dps:.6f}|{self.result_display_seconds:.1f}")
                self._beep(1450, 110, repeat=2)

                should_warn, details = self.detect_no_combat_log_activity(self.current_session)
                if should_warn:
                    self.set_status("No Combat Log activity detected", "#c98b16")
                    if not background:
                        messagebox.showwarning(
                            "No Combat Log activity detected",
                            "The recording does not appear to contain an active DDO Combat Log.\n\n"
                            "Make sure the Combat Log tab is visible and that the selected capture area "
                            "covers the combat text.\n\n"
                            "This is more likely a capture/setup issue than a real 0-damage result.\n\n"
                            + details
                        )
            else:
                self.set_status("Analysis error", "#b22222")
                try:
                    session = latest_session()
                    if session is not None:
                        lines = getattr(self, "analysis_output_lines", [])
                        (session / "analysis_error.txt").write_text(
                            "\n".join(lines) + "\n", encoding="utf-8"
                        )
                except Exception:
                    pass
                self._beep(500, 180)
                if not self.analysis_background:
                    messagebox.showerror(
                        "Analysis error",
                        "Analysis failed. The recorded data are still saved and can be analyzed again.\n\n"
                        "Technical details were saved as analysis_error.txt in the session folder."
                    )

        elif kind == "replay":
            self.set_busy(False, "Replay live test complete" if code == 0 else "Replay live test failed")
            self.enable_idle_controls()
            self.suppress_continuous_live = False
            if self.continuous_live_var.get():
                self.after(300, self.ensure_continuous_live)
            session = self.current_session or latest_session()
            if code == 0 and session is not None:
                self.set_status("Replay live test complete", "#2e8b57")
                try:
                    data = json.loads((Path(session) / "replay_live_summary.json").read_text(encoding="utf-8"))
                    replay_damage = data.get("replay_live_damage")
                    replay_events = data.get("replay_live_damage_events")
                    post_damage = data.get("post_damage")
                    post_events = data.get("post_damage_events")
                    diff = data.get("damage_difference_replay_minus_post")
                    diff_pct = data.get("damage_difference_percent")
                    detected = data.get("detected_lines")
                    ocr_lines = data.get("ocr_lines")
                    fallback = data.get("robust_fallback_runs")
                    overrides = data.get("robust_fallback_overrides")
                    bottom_deferred = data.get("bottom_deferred")
                    bottom_recovered = data.get("bottom_recovered")
                    bottom_flush = data.get("bottom_stop_flushed")
                    lines = [
                        "Offline live replay complete.",
                        "",
                        f"Replay live damage: {dknum(replay_damage) if replay_damage is not None else '—'} ({replay_events if replay_events is not None else '—'} events)",
                        f"Post-analysis damage: {dknum(post_damage) if post_damage is not None else '—'} ({post_events if post_events is not None else '—'} events)",
                    ]
                    if diff is not None:
                        pct_text = f" ({float(diff_pct):+.3f}%)" if diff_pct is not None else ""
                        lines.append(f"Difference: {dknum(diff)}{pct_text}")
                    lines.extend([
                        "",
                        f"Detected / OCR lines: {detected} / {ocr_lines}",
                        f"Robust alignment fallbacks: {fallback} (overrides: {overrides})",
                        f"Bottom rows: deferred {bottom_deferred}, recovered {bottom_recovered}, Stop-flushed {bottom_flush}",
                        "",
                        "This replay checks live reconstruction/OCR/parser correctness on the saved capture. "
                        "It is not a real-time throughput test; the offline OCR queue waits instead of dropping lines.",
                    ])
                    messagebox.showinfo("Replay live test", "\n".join(lines))
                except Exception as e:
                    messagebox.showwarning(
                        "Replay live test",
                        f"Replay completed, but the summary could not be opened:\n\n{e}"
                    )
            else:
                self.set_status("Replay live test failed", "#b22222")
                details = "\n".join(getattr(self, "replay_output_lines", [])[-20:])
                messagebox.showerror(
                    "Replay live test",
                    "Replay failed. The original session was not modified.\n\n" + (details or "See replay_live_error.txt in the session folder.")
                )

        elif kind == "install":
            self.set_busy(False, "Installation complete")
            self.set_status("Ready", "#2e8b57" if code == 0 else "#b22222")
            self.refresh_system_status()
            if code == 0:
                messagebox.showinfo("Installation", "Python packages were installed successfully.")
            else:
                messagebox.showerror("Installation", "Python packages could not be installed.")

    def show_about(self):
        win = tk.Toplevel(self)
        win.title("About DDO Combat Analyzer")
        win.resizable(False, False)
        win.transient(self)

        body = ttk.Frame(win, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Made by ChatGPT 5.6 with dubious assistance from a silly Danish math teacher. Oct, 2006",
            wraplength=540,
            justify="left",
        ).pack(anchor="w")
        tk.Label(
            body,
            text="Hav det godt eller opfør jer ordentligt",
            font=("Segoe UI", 10, "italic"),
        ).pack(anchor="w", pady=(12, 0))
        ttk.Button(body, text="OK", command=win.destroy).pack(anchor="e", pady=(18, 0))

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(20, (self.winfo_width() - win.winfo_width()) // 2)
            y = self.winfo_rooty() + max(20, (self.winfo_height() - win.winfo_height()) // 3)
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass
        win.lift()
        win.focus_force()

    def _save_first_run_guide_setting(self, shown=True):
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        ui = cfg.setdefault("ui", {})
        ui["first_run_guide_shown"] = bool(shown)
        tmp = CONFIG.with_name(CONFIG.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(CONFIG)

    def show_first_run_guide_if_needed(self):
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        if bool(cfg.get("ui", {}).get("first_run_guide_shown", False)):
            return
        if getattr(self, "_first_run_guide_open", False):
            return
        self._first_run_guide_open = True

        win = tk.Toplevel(self)
        win.title("Welcome to DDO Combat Analyzer")
        win.resizable(False, False)
        win.transient(self)
        try:
            win.grab_set()
        except Exception:
            pass

        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body, text="Welcome to DDO Combat Analyzer", style="ResultsTitle.TLabel"
        ).pack(anchor="w", pady=(0, 12))

        guide_text = (
            "1. Select your Combat Log area\n"
            "   Make sure the full visible Combat Log is inside the selected area.\n\n"
            "2. Position the DPS overlay\n"
            "   Drag it where you want it. Double-click to save the position.\n\n"
            "3. Start/stop a recorded session with Pause/Break\n"
            "   Live DPS runs continuously. Press Pause/Break once to start recording\n"
            "   and again to stop and analyze.\n\n"
            "4. Keep DDO in focus\n"
            "   Live capture pauses automatically when DDO is not the active window."
        )
        ttk.Label(body, text=guide_text, justify="left").pack(anchor="w")
        ttk.Label(
            body,
            text="Tip: When analysis is complete, you'll hear a beep and Session DPS will briefly appear in the overlay.",
            wraplength=620,
            style="Small.TLabel",
        ).pack(anchor="w", pady=(14, 10))

        dont_show = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Don't show this again", variable=dont_show).pack(anchor="w")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))

        def finish(start_setup=False):
            if dont_show.get():
                self._save_first_run_guide_setting(True)
            else:
                self._save_first_run_guide_setting(False)
            self._first_run_guide_open = False
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            if start_setup:
                self.setup_chain_active = True
                self.after(120, lambda: self.configure_area(onboarding=True))

        ttk.Button(buttons, text="Set up now", command=lambda: finish(True)).pack(side="left")
        ttk.Button(buttons, text="Close", command=lambda: finish(False)).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: finish(False))

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(20, (self.winfo_width() - win.winfo_width()) // 2)
            y = self.winfo_rooty() + max(20, (self.winfo_height() - win.winfo_height()) // 3)
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass
        win.lift()
        win.focus_force()

    def update_reprocess_button_state(self):
        if not hasattr(self, "reprocess_btn"):
            return
        selected = self.current_session or latest_session()
        state = "normal" if self.proc is None and selected is not None else "disabled"
        try:
            self.reprocess_btn.config(state=state)
        except Exception:
            pass
        try:
            replay_ok = bool(
                self.proc is None
                and selected is not None
                and (Path(selected) / "combat_changed_gray_ffv1.mkv").exists()
                and (Path(selected) / "capture_index.csv").exists()
            )
            self.replay_live_btn.config(state="normal" if replay_ok else "disabled")
        except Exception:
            pass
        try:
            self.open_session_btn.config(state="normal" if self.proc is None else "disabled")
        except Exception:
            pass

    # -------------------------- controls -----------------------------------

    def disable_controls(self):
        self.start_btn.config(state="disabled")
        self.calibrate_btn.config(state="disabled")
        self.open_session_btn.config(state="disabled")
        self.reprocess_btn.config(state="disabled")
        self.replay_live_btn.config(state="disabled")
        self.system_button.config(state="disabled")
        self.line_height_combo.config(state="disabled")
        self.hotkey_combo.config(state="disabled")
        self.rolling_window_combo.config(state="disabled")
        self.overlay_font_size_combo.config(state="disabled")
        self.ocr_threads_combo.config(state="disabled")
        self.overlay_position_combo.config(state="disabled")
        self.position_overlay_btn.config(state="disabled")
        self.continuous_check.config(state="disabled")

    def enable_idle_controls(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.open_session_btn.config(state="normal")
        self.calibrate_btn.config(state="normal")
        self.update_reprocess_button_state()
        self.system_button.config(state="normal")
        self.line_height_combo.config(state="readonly")
        self.hotkey_combo.config(state="readonly")
        self.rolling_window_combo.config(state="readonly")
        self.overlay_font_size_combo.config(state="readonly")
        self.ocr_threads_combo.config(state="readonly")
        self.overlay_position_combo.config(state="readonly")
        self.position_overlay_btn.config(state="normal")
        self.continuous_check.config(state="normal")

    def on_line_height_changed(self, _event=None):
        if self.proc is not None:
            return
        try:
            save_line_height_setting(self.line_height_var.get())
            if self.line_height_var.get() == "Auto":
                self.progress_text.config(text="Line height: Auto")
            else:
                self.progress_text.config(text=f"Line height: {self.line_height_var.get()} (fixed)")
            if self.continuous_live_var.get():
                self.stop_continuous_live()
                self.after(250, self.ensure_continuous_live)
        except Exception as e:
            messagebox.showerror("Line height", f"Could not save line-height setting:\n\n{e}")

    def configure_area(self, onboarding=False):
        if self.proc is not None:
            return

        st = dependency_status()
        if st["missing_python"]:
            if onboarding:
                self.setup_chain_active = False
            messagebox.showwarning(
                "Missing Python packages",
                "Run System check and install the Python packages first."
            )
            return

        if not onboarding:
            if not messagebox.askokcancel(
                "Select combat log area",
                "DDO must be running with the combat log visible.\n\n"
                "When you click OK, this window will hide, DDO will be brought to the front, "
                "and a frozen screenshot will be taken. Then select ONLY the combat log."
            ):
                return

        if self.continuous_live_var.get():
            self.stop_continuous_live()
        self.disable_controls()
        self.set_busy(True, "Preparing log-area selection ...")
        self.set_status("Selecting log area ...", "#c98b16")
        self.withdraw()

        cmd = [sys.executable, str(HERE / "select_log_area_gui.py")]
        self.start_subprocess(cmd, "select", stdin_pipe=False)

    def check_ready_to_record(self):
        st = dependency_status()
        problems = []
        if st["missing_python"]:
            problems.append("Python packages: " + ", ".join(st["missing_python"]))
        if not st["ffmpeg"]:
            problems.append("FFmpeg")

        if problems:
            messagebox.showerror(
                "System not ready",
                "The following are missing:\n\n- " + "\n- ".join(problems)
                + "\n\nRun 'SETUP DDO Combat Analyzer.sh' from the program folder."
            )
            return False

        cap, configured = read_capture_area()
        if not cap or not configured:
            messagebox.showwarning(
                "Combat log area missing",
                "Select the combat log area before starting a recording."
            )
            return False

        if not find_ddo_window():
            messagebox.showerror(
                "DDO not found",
                "Start DDO and log in to your character before starting a recording."
            )
            return False

        return True

    def start_recording(self, from_hotkey=False):
        if self.proc is not None:
            return
        if not self.check_ready_to_record():
            return

        if not from_hotkey and not messagebox.askokcancel(
            "Ready to record?",
            "Before recording, make sure the DDO Combat Log tab is visible.\n\n"
            "The analyzer can only read combat text that is actually visible on screen.\n\n"
            "Click OK to start recording."
        ):
            return

        self.recording_started_by_hotkey = bool(from_hotkey)
        self.analysis_background = bool(from_hotkey)
        self.recording_live_boundary_epoch = time.time()
        self.ensure_continuous_live()
        # Drop only OCR work that was detected before this recording boundary.
        # Already-confirmed rolling events are deliberately preserved so live DPS
        # remains truly continuous across recording start/stop.
        self.send_live_command(f"SESSION_BOUNDARY|{self.recording_live_boundary_epoch:.6f}")
        self.clear_live_state()
        self.recording_session = None
        self.disable_controls()
        self.stop_btn.config(state="disabled")
        self.set_status("Preparing recorder...", "#c98b16")
        self.set_busy(True, "Preparing recorder before switching to DDO ...")

        # Start the recorder engine first. It sends RECORDER_READY only after
        # FFmpeg and all output files are initialized. DDO is focused only then.
        cmd = [sys.executable, str(HERE / "record_optimized.py")]
        self.start_subprocess(cmd, "record", stdin_pipe=True)

    def stop_recording(self, from_hotkey=False):
        if self.proc is None or self.proc_kind != "record":
            return

        if from_hotkey:
            self.analysis_background = True
        self.stop_btn.config(state="disabled")
        self.set_status("Stopping recording ...", "#c98b16")
        self.set_busy(True, "Saving recording ...")
        if self.continuous_live_var.get():
            self.send_live_command(f"STOP_SESSION|{time.time():.6f}")
        else:
            self.stop_live_estimator()
        self._beep(760, 100)

        try:
            if self.proc.stdin:
                self.proc.stdin.write("STOP\n")
                self.proc.stdin.flush()
        except Exception as e:
            messagebox.showerror("Stop error", str(e))

        # Stay in the analyzer while the recording is finalized.
        # There is no reason to switch back to DDO after the user has pressed Stop.

    def start_analysis(self):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=100, value=0)
        self.progress_text.config(text="Step 1/4 - Preparing analysis...")
        cmd = [sys.executable, str(HERE / "process_latest.py")]
        self.start_subprocess(cmd, "analyze", stdin_pipe=False)

    def reprocess_selected(self, session=None):
        if self.proc is not None:
            return
        session = Path(session) if session is not None else (self.current_session or latest_session())
        if session is None or not session.exists():
            messagebox.showinfo("Reanalysis", "There is no session selected to analyze.")
            return
        if not (session / "combat_changed_gray_ffv1.mkv").exists():
            messagebox.showerror(
                "Reanalysis",
                "The selected session does not contain the original capture video, so it cannot be reanalyzed."
            )
            return

        st = dependency_status()
        if st["missing_python"] or not st["ffmpeg"]:
            messagebox.showerror("System not ready", "Run System check first.")
            return

        try:
            set_latest_session_pointer(session)
        except Exception as e:
            messagebox.showerror("Reanalysis", f"Could not select the session for reanalysis:\n\n{e}")
            return
        self.current_session = session
        self.detail_label.config(text=session.name)
        self.analysis_background = False
        self.disable_controls()
        self.set_status("Analyzing ...", "#c98b16")
        self.set_busy(True, f"Reanalyzing {session.name} ...")
        self.start_analysis()

    def replay_live_selected(self, session=None):
        if self.proc is not None:
            return
        session = Path(session) if session is not None else (self.current_session or latest_session())
        if session is None or not session.exists():
            messagebox.showinfo("Replay live test", "There is no session selected to replay.")
            return
        missing = [
            name for name in ("combat_changed_gray_ffv1.mkv", "capture_index.csv")
            if not (session / name).exists()
        ]
        if missing:
            messagebox.showerror(
                "Replay live test",
                "The selected session cannot be replayed because it is missing:\n\n- " + "\n- ".join(missing)
            )
            return
        st = dependency_status()
        if st["missing_python"] or not st["ffmpeg"]:
            messagebox.showerror("System not ready", "Run System check first.")
            return

        self.current_session = session
        self.detail_label.config(text=session.name)
        self.analysis_background = False
        # Do not let the persistent live helper compete with an offline replay for
        # Indra/CPU resources. The GUI automatically starts it again afterwards.
        self.suppress_continuous_live = True
        self.stop_continuous_live()
        self.disable_controls()
        self.set_status("Replaying live pipeline ...", "#c98b16")
        self.set_busy(True, f"Replay live test: {session.name} ...")
        cmd = [
            sys.executable, str(HERE / "replay_live_test.py"),
            "--session", str(session),
        ]
        try:
            self.start_subprocess(cmd, "replay", stdin_pipe=False)
        except Exception as e:
            self.set_busy(False, "Replay live test failed")
            self.enable_idle_controls()
            self.suppress_continuous_live = False
            if self.continuous_live_var.get():
                self.after(300, self.ensure_continuous_live)
            self.set_status("Replay live test failed", "#b22222")
            messagebox.showerror("Replay live test", str(e))

    # Backwards-compatible alias used by older code paths, if any.
    def reprocess_latest(self):
        self.reprocess_selected()

    # -------------------------- timer --------------------------------------

    def update_timer(self):
        if self.start_time is None:
            return
        secs = int(time.monotonic() - self.start_time)
        mm, ss = divmod(secs, 60)
        self.elapsed_label.config(text=f"{mm:02d}:{ss:02d}")
        self.timer_job = self.after(500, self.update_timer)

    def stop_timer(self):
        self.start_time = None
        if self.timer_job:
            try:
                self.after_cancel(self.timer_job)
            except Exception:
                pass
        self.timer_job = None

    # -------------------------- results ------------------------------------

    def clear_tree(self, tree):
        for item in tree.get_children():
            tree.delete(item)
        self._reset_tree_sort(tree)

    def clear_live_state(self):
        self.total_dealt_var.set("—")
        self.regular_var.set("—")
        self.sneak_var.set("—")
        self.total_taken_var.set("—")
        self.healing_var.set("Healing to targets: —")
        self.net_loss_var.set("Net target HP loss: —")
        self.detail_label.config(text="")
        self.attack_total_var.set("Attacks: —")
        self.attack_hitmiss_var.set("Hits / misses: —")
        self.attack_crits_var.set("Confirmed crits: —")
        self.attack_apm_var.set(f"Peak {rolling_window_short(float(self.rolling_window_var.get().replace(' s', '')))} APM: —")
        for tree in [
            self.target_tree, self.type_tree,
            self.taken_type_tree, self.taken_source_tree,
            self.attack_summary_tree, self.confirm_tree,
            self.damage_log_tree
        ]:
            self.clear_tree(tree)
        self.details_btn.config(state="disabled")
        try:
            self.target_details_btn.config(state="disabled")
        except Exception:
            pass
        self.diag_text.config(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("1.0", "Session summary will appear after analysis.")
        self.diag_text.config(state="disabled")
        self.dps_points = []
        self.live_dps_points = []
        self.dps_peak_var.set(f"Peak post {rolling_window_short(self.graph_window_seconds)} DPS: —")
        self.dps_final_var.set(f"Final post {rolling_window_short(self.graph_window_seconds)} DPS: —")
        self.dps_compare_var.set("Live comparison: —")
        try:
            self.notebook.hide(self.dps_tab)
        except Exception:
            pass

    def detect_no_combat_log_activity(self, session):
        """
        Detect recordings where essentially nothing changed in the selected area.
        This commonly means the visible DDO tab was General/Standard instead of Combat.
        Returns (should_warn, details_text).
        """
        if not session:
            return (False, "")

        capture_csv = session / "capture_index.csv"
        ocr_log = session / "combat_log.txt"
        damage_csv = session / "damage_events.csv"

        total_saved = 0
        non_forced = 0
        ocr_count = 0
        event_count = 0

        try:
            if capture_csv.exists():
                with capture_csv.open("r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
                rows = [r for r in rows if str(r.get("saved", "")).strip() == "1"]
                total_saved = len(rows)

                # Count genuine text-change frames separately from periodic anchors.
                for row in rows:
                    forced_value = str(row.get("forced_anchor", "")).strip().lower()
                    is_forced = forced_value in ("1", "true", "yes")
                    if not is_forced:
                        non_forced += 1
        except Exception:
            pass

        try:
            if ocr_log.exists():
                with ocr_log.open("r", encoding="utf-8", errors="replace") as f:
                    ocr_count = sum(1 for line in f if line.strip())
        except Exception:
            pass

        try:
            if damage_csv.exists():
                with damage_csv.open("r", encoding="utf-8", newline="") as f:
                    event_count = sum(1 for _ in csv.DictReader(f))
        except Exception:
            pass

        # Strong signal: no reconstructed OCR at all and no parsed events.
        # If there are many saved frames but almost no genuine changes, the selected
        # area was likely static/non-Combat chat.
        should_warn = (
            event_count == 0
            and ocr_count == 0
            and total_saved >= 8
            and non_forced <= max(6, int(total_saved * 0.08))
        )

        details = (
            f"Saved frames: {total_saved}\\n"
            f"Non-anchor changes: {non_forced}\\n"
            f"Reconstructed OCR lines: {ocr_count}\\n"
            f"Parsed damage events: {event_count}"
        )
        return (should_warn, details)

    def load_results(self, session):
        if not session:
            return
        events_file = session / "damage_events.csv"
        if not events_file.exists():
            self.progress_text.config(text="damage_events.csv is missing")
            return

        outgoing = []
        incoming = []
        healing = []

        with events_file.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    amount = int(row.get("amount", "0") or 0)
                except Exception:
                    amount = 0
                event = row.get("event", "")
                item = dict(row)
                item["amount"] = amount
                if event == "damage_dealt":
                    outgoing.append(item)
                elif event == "damage_taken":
                    incoming.append(item)
                elif event == "healing_to_target":
                    healing.append(item)

        regular = sum(x["amount"] for x in outgoing if x.get("mode") == "regular")
        sneak = sum(x["amount"] for x in outgoing if x.get("mode") == "sneak")
        dealt = regular + sneak
        taken = sum(x["amount"] for x in incoming)
        healed = sum(x["amount"] for x in healing)
        net = dealt - healed

        self.total_dealt_var.set(dknum(dealt))
        self.regular_var.set(dknum(regular))
        self.sneak_var.set(dknum(sneak))
        self.total_taken_var.set(dknum(taken))
        self.healing_var.set("Healing to targets: " + dknum(healed))
        self.net_loss_var.set("Net target HP loss: " + dknum(net))

        analysis_summary = {}
        try:
            analysis_summary = json.loads((session / "analysis_summary.json").read_text(encoding="utf-8"))
        except Exception:
            analysis_summary = {}
        try:
            if 'session_dps' in analysis_summary:
                session_value = analysis_summary.get('session_dps')
            else:
                session_value = analysis_summary.get('elapsed_dps')  # older sessions
            if session_value is None:
                self.elapsed_dps_var.set("Session DPS: —")
            else:
                self.elapsed_dps_var.set(f"Session DPS: {dknum(round(float(session_value)))}")
            self.active_dps_var.set(f"Active combat DPS: {dknum(round(float(analysis_summary.get('active_combat_dps', 0))))}")
            self.blocked_var.set(f"Blocked/resisted: {dknum(int(analysis_summary.get('blocked_outgoing', 0) or 0))}")
            self.sneak_stats_var.set(
                f"Sneak: {float(analysis_summary.get('sneak_share_percent', 0)):.1f}% | "
                f"{int(analysis_summary.get('sneak_hits', 0) or 0)} hits | "
                f"{float(analysis_summary.get('sneak_attacks_per_minute', 0)):.1f}/min"
            )
        except Exception:
            pass

        # Targets
        targets = defaultdict(lambda: {
            "regular": 0, "sneak": 0, "damage": 0,
            "healing": 0, "hits": 0
        })
        for x in outgoing:
            t = targets[x.get("target", "Unknown")]
            mode = x.get("mode", "regular")
            if mode in ("regular", "sneak"):
                t[mode] += x["amount"]
            t["damage"] += x["amount"]
            t["hits"] += 1
        for x in healing:
            targets[x.get("target", "Unknown")]["healing"] += x["amount"]

        self.clear_tree(self.target_tree)
        for name, d in sorted(targets.items(), key=lambda kv: (-kv[1]["damage"], kv[0].lower())):
            self.target_tree.insert("", "end", values=(
                name,
                dknum(d["regular"]),
                dknum(d["sneak"]),
                dknum(d["damage"]),
                dknum(d["healing"]),
                dknum(d["damage"] - d["healing"]),
                d["hits"],
            ))

        # Outgoing by type
        types = defaultdict(lambda: {"regular": 0, "sneak": 0, "total": 0, "hits": 0})
        for x in outgoing:
            d = types[x.get("type", "unknown")]
            mode = x.get("mode", "regular")
            if mode in ("regular", "sneak"):
                d[mode] += x["amount"]
            d["total"] += x["amount"]
            d["hits"] += 1

        self.clear_tree(self.type_tree)
        for name, d in sorted(types.items(), key=lambda kv: (-kv[1]["total"], kv[0])):
            self.type_tree.insert("", "end", values=(
                name, dknum(d["regular"]), dknum(d["sneak"]),
                dknum(d["total"]), d["hits"]
            ))

        # Incoming by type/source
        taken_types = defaultdict(lambda: {"total": 0, "hits": 0})
        taken_sources = defaultdict(lambda: {"total": 0, "hits": 0})
        for x in incoming:
            dt = taken_types[x.get("type", "unknown")]
            dt["total"] += x["amount"]
            dt["hits"] += 1

            ds = taken_sources[x.get("source", "unknown")]
            ds["total"] += x["amount"]
            ds["hits"] += 1

        self.clear_tree(self.taken_type_tree)
        for name, d in sorted(taken_types.items(), key=lambda kv: (-kv[1]["total"], kv[0])):
            self.taken_type_tree.insert("", "end", values=(name, dknum(d["total"]), d["hits"]))

        self.clear_tree(self.taken_source_tree)
        for name, d in sorted(taken_sources.items(), key=lambda kv: (-kv[1]["total"], kv[0])):
            self.taken_source_tree.insert("", "end", values=(name, dknum(d["total"]), d["hits"]))

        # Blocking / mitigation statistics
        self.clear_tree(self.mitigation_tree)
        blocked_total = 0
        try:
            with (session / "mitigation_by_target_and_type.csv").open("r", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    blocked_total += int(r.get("blocked", "0") or 0)
                    reduction = r.get("reduction_type", "") or ("Unknown DR" if int(r.get("blocked", "0") or 0) else "")
                    self.mitigation_tree.insert("", "end", values=(
                        r.get("target", ""), reduction, r.get("damage_type", ""),
                        dknum(int(r.get("dealt", "0") or 0)),
                        dknum(int(r.get("blocked", "0") or 0)),
                        dknum(int(r.get("pre_mitigation", "0") or 0)),
                        f"{float(r.get('blocked_percent', '0') or 0):.1f}%",
                        r.get("affected_hits", r.get("hits", "")),
                    ))
        except Exception:
            pass
        self.mitigation_summary_var.set(f"Blocked / resisted outgoing damage: {dknum(blocked_total)}")

        # Attack / critical statistics
        crit_file = session / "crit_by_target.csv"
        attack_events_file = session / "attack_events.csv"
        apm_file = session / "apm_rolling.csv"
        if not apm_file.exists():
            apm_file = session / "apm_15s.csv"
        rolling_window = self.get_session_rolling_window(session)
        window_short = rolling_window_short(rolling_window)
        crit_rows = []
        attack_rows = []
        try:
            if crit_file.exists():
                with crit_file.open("r", encoding="utf-8", newline="") as f:
                    crit_rows = list(csv.DictReader(f))
        except Exception:
            crit_rows = []
        try:
            if attack_events_file.exists():
                with attack_events_file.open("r", encoding="utf-8", newline="") as f:
                    attack_rows = list(csv.DictReader(f))
        except Exception:
            attack_rows = []

        total_attacks = sum(int(r.get("attacks", "0") or 0) for r in crit_rows)
        total_hits = sum(int(r.get("hits", "0") or 0) for r in crit_rows)
        total_grazes = sum(int(r.get("grazing_hits", "0") or 0) for r in crit_rows)
        total_misses = sum(int(r.get("misses", "0") or 0) for r in crit_rows)
        total_crits = sum(int(r.get("confirmed_crits", "0") or 0) for r in crit_rows)
        peak_apm = None
        try:
            if apm_file.exists():
                vals = []
                with apm_file.open("r", encoding="utf-8", newline="") as f:
                    for r in csv.DictReader(f):
                        try:
                            vals.append(float(r.get("rolling_apm", r.get("rolling_15s_apm", ""))))
                        except Exception:
                            pass
                if vals:
                    peak_apm = max(vals)
        except Exception:
            peak_apm = None

        self.attack_total_var.set(f"Attacks: {dknum(total_attacks)}")
        self.attack_hitmiss_var.set(
            f"Hits / grazes / misses: {dknum(total_hits)} / {dknum(total_grazes)} / {dknum(total_misses)}"
        )
        self.attack_crits_var.set(f"Confirmed crits: {dknum(total_crits)}")
        self.attack_apm_var.set(
            f"Peak {window_short} APM: —" if peak_apm is None else f"Peak {window_short} APM: {int(round(peak_apm))}"
        )

        self.clear_tree(self.attack_summary_tree)
        for r in crit_rows:
            def pct(name):
                try:
                    return f"{float(r.get(name, '0') or 0):.1f}%"
                except Exception:
                    return "—"
            self.attack_summary_tree.insert("", "end", values=(
                r.get("target", ""), r.get("attacks", ""), r.get("hits", ""), r.get("grazing_hits", ""), r.get("misses", ""),
                r.get("critical_threats", ""), r.get("confirmed_crits", ""), r.get("failed_confirms", ""),
                pct("grazing_rate_percent"), pct("crit_rate_percent"), pct("confirm_rate_percent"),
                r.get("highest_failed_confirm_total", "") or "—",
                r.get("lowest_successful_confirm_total", "") or "—",
                r.get("observed_confirm", "") or "—",
            ))

        self.clear_tree(self.confirm_tree)
        for r in attack_rows:
            if r.get("event") != "confirm":
                continue
            raw_time = r.get("elapsed_seconds", "")
            try:
                display_time = self._format_graph_time(float(raw_time))
            except Exception:
                display_time = ""
            result = "CRIT" if str(r.get("confirmed", "")).strip() in ("1", "true", "True") else "FAILED"
            self.confirm_tree.insert("", "end", values=(
                display_time, r.get("target", ""), r.get("roll", ""), r.get("bonus", ""),
                r.get("total", ""), result, r.get("text", "")
            ))

        self.clear_tree(self.roll_distribution_tree)
        try:
            with (session / "attack_roll_distribution.csv").open("r", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    self.roll_distribution_tree.insert("", "end", values=(
                        r.get("target", ""), r.get("roll", ""), r.get("miss", ""),
                        r.get("grazing_hit", ""), r.get("hit", ""),
                        r.get("critical_threat", ""), r.get("other", ""), r.get("total", ""),
                    ))
        except Exception:
            pass

        # Detailed per-event log
        self.clear_tree(self.damage_log_tree)
        self.damage_log_text_by_item = {}
        try:
            with events_file.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    raw_time = row.get("elapsed_seconds", "")
                    try:
                        display_time = self._format_graph_time(float(raw_time))
                    except Exception:
                        display_time = ""
                    iid = self.damage_log_tree.insert("", "end", values=(
                        row.get("line", ""), display_time, row.get("event", ""), row.get("source", ""),
                        row.get("target", ""), row.get("mode", ""), row.get("type", ""),
                        row.get("amount", ""), row.get("blocked_amount", ""),
                    ))
                    self.damage_log_text_by_item[iid] = row.get("text", "")
        except Exception:
            pass

        # Diagnostics summary
        def count_csv_rows(path):
            try:
                with path.open("r", encoding="utf-8", newline="") as f:
                    return sum(1 for _ in csv.DictReader(f))
            except Exception:
                return 0

        capture_rows = 0
        saved_rows = 0
        forced_rows = 0
        try:
            with (session / "capture_index.csv").open("r", encoding="utf-8", newline="") as f:
                crows = list(csv.DictReader(f))
            capture_rows = len(crows)
            saved = [r for r in crows if str(r.get("saved", "")).strip() == "1"]
            saved_rows = len(saved)
            forced_rows = sum(
                1 for r in saved if str(r.get("forced_anchor", "")).strip() == "1"
            )
        except Exception:
            pass

        diagnostics = [
            f"Session: {session.name}",
            "",
            f"Captured frames:       {capture_rows}",
            f"Saved FFV1 frames:     {saved_rows}",
            f"Parsed combat events:  {count_csv_rows(session / 'damage_events.csv')}",
            f"{rolling_window_short(self.get_session_rolling_window(session))} DPS samples:       {count_csv_rows(session / 'dps_rolling.csv') or count_csv_rows(session / 'dps_15s.csv')}",
            f"Live DPS samples:      {count_csv_rows(session / 'live_dps.csv')}",
            f"Attack/confirm events: {count_csv_rows(session / 'attack_events.csv')}",
            f"{rolling_window_short(self.get_session_rolling_window(session))} APM samples:       {count_csv_rows(session / 'apm_rolling.csv') or count_csv_rows(session / 'apm_15s.csv')}",
            f"Normalized events:     {count_csv_rows(session / 'normalized_events.csv')}",
            "",
            "DATA QUALITY",
            f"Visual lines:          {analysis_summary.get('data_quality', {}).get('visual_lines_reconstructed', '—')}",
            f"OCR text lines:        {analysis_summary.get('data_quality', {}).get('ocr_text_lines', '—')}",
            f"Post damage events:    {analysis_summary.get('data_quality', {}).get('post_damage_events', '—')}",
            f"Live damage events:    {analysis_summary.get('data_quality', {}).get('live_damage_events', '—')}",
            f"Post corrections:      {analysis_summary.get('data_quality', {}).get('post_numeric_corrections', '—')}",
            f"Live corrections:      {analysis_summary.get('data_quality', {}).get('live_numeric_corrections', '—')}",
            f"Unparsed damage-like:  {analysis_summary.get('data_quality', {}).get('suspicious_unparsed_damage_lines', '—')}",
            f"Event count agreement: {analysis_summary.get('data_quality', {}).get('event_count_agreement', '—')}",
            f"Focus interruptions:   {analysis_summary.get('data_quality', {}).get('focus_interruptions', 0)}",
            f"Focus pause seconds:   {analysis_summary.get('data_quality', {}).get('focus_interruption_seconds', 0):.2f}" if isinstance(analysis_summary.get('data_quality', {}).get('focus_interruption_seconds', 0), (int, float)) else f"Focus pause seconds:   {analysis_summary.get('data_quality', {}).get('focus_interruption_seconds', '—')}",
            "",
            "OCR engine: Indra",
        ]
        self.diag_text.config(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("1.0", "\n".join(diagnostics))
        self.diag_text.config(state="disabled")

        self.load_dps_graph(session)

        self.current_session = session
        self.open_folder_btn.config(state="normal")
        self.details_btn.config(state="normal")
        self.detail_label.config(text=session.name)
        self.overview_note.config(
            text="Analysis is complete. Use Detailed Results or the tabs above to inspect individual events."
        )
        self.update_reprocess_button_state()

    def try_load_latest_result(self):
        session = latest_session()
        if session and (session / "damage_events.csv").exists():
            try:
                self.load_results(session)
                self.set_status("Ready", "#2e8b57")
            except Exception:
                pass
        else:
            self.set_status("Ready", "#2e8b57")
        self.update_reprocess_button_state()

    def open_session_browser(self):
        if self.proc is not None:
            return
        sessions = list_sessions()
        if not sessions:
            messagebox.showinfo("Sessions", "No recorded sessions were found in this installation.")
            return

        win = tk.Toplevel(self)
        win.title("Open session")
        win.geometry("980x470")
        win.minsize(820, 360)
        win.transient(self)

        ttk.Label(
            win, text="Previous sessions", style="ResultsTitle.TLabel"
        ).pack(anchor="w", padx=14, pady=(14, 2))
        ttk.Label(
            win,
            text="Double-click an analyzed session to reopen its saved results. Reanalysis reruns OCR on the original capture.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=14, pady=(0, 10))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        columns = ("date", "duration", "damage", "dps", "targets", "status")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "date": "Date / time", "duration": "Recording", "damage": "Total damage",
            "dps": "Session DPS", "targets": "Targets", "status": "Status",
        }
        widths = {"date": 150, "duration": 90, "damage": 120, "dps": 105, "targets": 330, "status": 85}
        for col in columns:
            tree.heading(col, text=headings[col])
            tree.column(col, width=widths[col], minwidth=60, anchor="w")
        tree.column("duration", anchor="e")
        tree.column("damage", anchor="e")
        tree.column("dps", anchor="e")
        tree.column("status", anchor="center")
        self._enable_tree_sorting(tree, numeric_columns=("duration", "damage", "dps"))
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        item_to_session = {}
        select_iid = None
        current_resolved = None
        try:
            if self.current_session is not None:
                current_resolved = Path(self.current_session).resolve()
        except Exception:
            pass

        for idx, session in enumerate(sessions):
            info = read_session_catalog_info(session)
            duration = "—"
            if info["duration"] is not None:
                try:
                    secs = float(info["duration"])
                    m, sec = divmod(secs, 60.0)
                    duration = f"{int(m):02d}:{sec:04.1f}" if secs >= 60 else f"{sec:.1f} s"
                except Exception:
                    pass
            damage = "—" if info["damage"] is None else dknum(round(float(info["damage"])))
            dps = "—" if info["dps"] is None else dknum(round(float(info["dps"])))
            iid = tree.insert("", "end", values=(
                info["date"], duration, damage, dps, info["targets"] or "—", info["status"]
            ))
            item_to_session[iid] = session
            try:
                if current_resolved is not None and session.resolve() == current_resolved:
                    select_iid = iid
            except Exception:
                pass
            if select_iid is None and idx == 0:
                select_iid = iid

        if select_iid is not None:
            tree.selection_set(select_iid)
            tree.focus(select_iid)
            tree.see(select_iid)

        def selected_session():
            sel = tree.selection()
            return item_to_session.get(sel[0]) if sel else None

        def open_selected(_event=None):
            session = selected_session()
            if session is None:
                return
            if not (session / "damage_events.csv").exists():
                messagebox.showinfo(
                    "Session not analyzed",
                    "This session has no saved analysis yet. Use Reanalyze to analyze its original capture.",
                    parent=win,
                )
                return
            try:
                self.load_results(session)
                set_latest_session_pointer(session)
                self.set_status("Ready", "#2e8b57")
                win.destroy()
            except Exception as e:
                messagebox.showerror("Open session", f"Could not open the saved analysis:\n\n{e}", parent=win)

        def reanalyze_selected():
            session = selected_session()
            if session is None:
                return
            win.destroy()
            self.reprocess_selected(session)

        def folder_selected():
            session = selected_session()
            if session is None:
                return
            try:
                if os.name == "nt":
                    os.startfile(str(session))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(session)])
                else:
                    subprocess.Popen(["xdg-open", str(session)])
            except Exception as e:
                messagebox.showerror("Open folder", str(e), parent=win)

        tree.bind("<Double-1>", open_selected)
        tree.bind("<Return>", open_selected)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(buttons, text="Open analysis", command=open_selected).pack(side="left")
        ttk.Button(buttons, text="Reanalyze", command=reanalyze_selected).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Open folder", command=folder_selected).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")

        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(10, (self.winfo_width() - win.winfo_width()) // 2)
            y = self.winfo_rooty() + max(10, (self.winfo_height() - win.winfo_height()) // 3)
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass
        win.lift()
        tree.focus_set()

    def open_session_folder(self):
        session = self.current_session or latest_session()
        if not session:
            return
        try:
            if os.name == "nt":
                os.startfile(str(session))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(session)])
            else:
                subprocess.Popen(["xdg-open", str(session)])
        except Exception as e:
            messagebox.showerror("Open folder", str(e))

    # -------------------------- shutdown -----------------------------------

    def on_close(self):
        if self.proc is not None:
            if not messagebox.askyesno(
                "Exit",
                "A recording or analysis is currently running. Do you really want to exit?"
            ):
                return
        try:
            self.hotkey_manager.stop()
        except Exception:
            pass
        self.stop_continuous_live()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
