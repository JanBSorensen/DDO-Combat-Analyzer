from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'config.json'


def ddo_hwnd():
    if os.name != 'nt':
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW('Turbine Device Class', None)
    if not hwnd:
        hwnd = user32.FindWindowW('Turbine Device Class Name', None)
    return hwnd or None


def ddo_client_rect():
    hwnd = ddo_hwnd()
    if not hwnd:
        return None
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    pt = wintypes.POINT(0, 0)
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y), int(rect.right - rect.left), int(rect.bottom - rect.top)


def focus_ddo():
    hwnd = ddo_hwnd()
    if not hwnd or os.name != 'nt':
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def is_ddo_foreground():
    hwnd = ddo_hwnd()
    if not hwnd or os.name != 'nt':
        return False
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def make_noactivate(root):
    """Keep the placement overlay clickable/draggable without activating it."""
    if os.name != 'nt':
        return
    try:
        root.update_idletasks()
        hwnd = root.winfo_id()
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        HWND_TOPMOST = -1
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception:
        pass


def save_config(root, actual_width, actual_height):
    rect = ddo_client_rect()
    if rect is None:
        messagebox.showerror('DDO not found', 'DDO must be running to save a relative overlay position.')
        return
    l, t, w, h = rect
    # The placement helper has extra instruction space underneath the real
    # overlay preview. Save the center of the real overlay, not the helper.
    cx = root.winfo_x() + actual_width / 2
    cy = root.winfo_y() + actual_height / 2
    rx = max(0.0, min(1.0, (cx - l) / max(1, w)))
    ry = max(0.0, min(1.0, (cy - t) / max(1, h)))
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    live = cfg.setdefault('live_dps', {})
    live['overlay_position'] = 'custom'
    live['overlay_x_ratio'] = round(rx, 6)
    live['overlay_y_ratio'] = round(ry, 6)
    tmp = CONFIG.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    tmp.replace(CONFIG)
    root.destroy()


def main():
    rect = ddo_client_rect()
    if rect is None:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror('DDO not found', 'Start DDO before positioning the overlay.')
        root.destroy(); return

    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    live = cfg.get('live_dps', {})
    actual_width = int(live.get('overlay_width', 560))
    actual_height = int(live.get('overlay_height', 190))
    helper_height = actual_height + 62
    font_size = int(live.get('font_size', 24))

    l, t, w, h = rect
    rx = float(live.get('overlay_x_ratio', 0.5))
    ry = float(live.get('overlay_y_ratio', 0.12))
    x = int(l + rx * w - actual_width / 2)
    y = int(t + ry * h - actual_height / 2)

    # Bring DDO forward before the helper is shown. The helper itself is marked
    # WS_EX_NOACTIVATE so dragging/clicking it does not take foreground focus.
    focus_ddo()
    time.sleep(0.08)

    root = tk.Tk()
    root.title('Position DPS overlay')
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.configure(bg='#151515')
    root.geometry(f'{actual_width}x{helper_height}{x:+d}{y:+d}')

    border = tk.Frame(root, bg='#e5b700', bd=3, relief='solid')
    border.pack(fill='both', expand=True)

    preview = tk.Frame(border, bg='#151515', width=actual_width, height=actual_height)
    preview.pack(fill='x')
    preview.pack_propagate(False)

    drag_widgets = [root, border, preview]
    lbl1 = tk.Label(
        preview, text='32,456 DPS', fg='#ff3030', bg='#151515',
        font=('Segoe UI', font_size, 'bold')
    )
    lbl1.pack(pady=(12, 0)); drag_widgets.append(lbl1)
    lbl2 = tk.Label(
        preview, text='82 APM', fg='#ff5050', bg='#151515',
        font=('Segoe UI', max(18, int(font_size * 0.60)), 'bold')
    )
    lbl2.pack(); drag_widgets.append(lbl2)
    lbl3 = tk.Label(
        preview, text='SESSION DPS: 34,126', fg='#ffd060', bg='#151515',
        font=('Segoe UI', max(16, int(font_size * 0.55)), 'bold')
    )
    lbl3.pack(); drag_widgets.append(lbl3)

    instruction = tk.Frame(border, bg='#e5b700')
    instruction.pack(fill='both', expand=True)
    lbl4 = tk.Label(
        instruction,
        text='Drag to position',
        fg='#111111', bg='#e5b700', font=('Segoe UI', 13, 'bold')
    )
    lbl4.pack(pady=(5, 0))
    lbl5 = tk.Label(
        instruction,
        text='Double-click = save   •   Right-click = cancel',
        fg='#111111', bg='#e5b700', font=('Segoe UI', 11, 'bold')
    )
    lbl5.pack(pady=(0, 5))
    drag_widgets.extend([instruction, lbl4, lbl5])

    state = {'x': 0, 'y': 0}

    def down(e):
        state.update(x=e.x_root - root.winfo_x(), y=e.y_root - root.winfo_y())

    def drag(e):
        root.geometry(f'+{e.x_root - state["x"]}+{e.y_root - state["y"]}')

    def save(_e=None):
        save_config(root, actual_width, actual_height)

    def cancel(_e=None):
        root.destroy()

    for widget in drag_widgets:
        widget.bind('<ButtonPress-1>', down)
        widget.bind('<B1-Motion>', drag)
        widget.bind('<Double-Button-1>', save)
        widget.bind('<Button-3>', cancel)

    root.update_idletasks()
    make_noactivate(root)

    def monitor_ddo_focus():
        try:
            if is_ddo_foreground():
                if str(root.state()) == 'withdrawn':
                    root.deiconify()
                    root.attributes('-topmost', True)
                    make_noactivate(root)
            else:
                if str(root.state()) != 'withdrawn':
                    root.withdraw()
        except Exception:
            pass
        if root.winfo_exists():
            root.after(100, monitor_ddo_focus)

    # Tk creation may briefly activate the helper; restore DDO after the styles
    # have been applied. Mouse interaction then leaves DDO as foreground.
    root.after(80, focus_ddo)
    root.after(180, monitor_ddo_focus)
    root.mainloop()


if __name__ == '__main__':
    main()
