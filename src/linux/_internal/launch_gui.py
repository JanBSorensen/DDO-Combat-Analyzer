from __future__ import annotations

import ctypes
import os
import runpy
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "DDO Combat Analyzer.pyw"
LOG = HERE / "startup_error.log"
_MUTEX_HANDLE = None
_LOCK_FILE = None


def show_error(message: str):
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "DDO Combat Analyzer - Startup Error", 0x10)
            return
        except Exception:
            pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("DDO Combat Analyzer - Startup Error", message, parent=root)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def is_admin():
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_self():
    if os.name != "nt" or is_admin():
        return True
    script = str(Path(__file__).resolve())
    params = subprocess.list2cmdline([script, "--elevated"])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(HERE), 1)
    return int(rc) > 32


def acquire_single_instance():
    global _MUTEX_HANDLE, _LOCK_FILE
    if os.name == "nt":
        try:
            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, False, "Local\\DDOCombatAnalyzer_GUI_v1")
            if handle and int(ctypes.get_last_error()) == ERROR_ALREADY_EXISTS:
                show_error("DDO Combat Analyzer is already running.")
                return False
            _MUTEX_HANDLE = handle
            return True
        except Exception:
            return True
    if sys.platform.startswith("linux"):
        try:
            import fcntl
            lock_path = HERE / ".gui_instance.lock"
            _LOCK_FILE = lock_path.open("w")
            fcntl.flock(_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _LOCK_FILE.write(str(os.getpid()))
            _LOCK_FILE.flush()
            return True
        except BlockingIOError:
            show_error("DDO Combat Analyzer is already running.")
            return False
        except Exception:
            return True
    return True


def main():
    try:
        if sys.platform.startswith("linux"):
            session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
            if session and session != "x11":
                show_error(
                    "This Linux build currently targets X11.\n\n"
                    "Log out and choose a Cinnamon (X11) session, then start the analyzer again."
                )
                return
        if os.name == "nt" and not is_admin():
            if not elevate_self():
                show_error(
                    "DDO Combat Analyzer needs Administrator privileges so the "
                    "Pause/Break recording hotkey can work while DDO has focus."
                )
            return

        if LOG.exists():
            try:
                LOG.unlink()
            except Exception:
                pass

        os.chdir(HERE)
        if not acquire_single_instance():
            return
        runpy.run_path(str(APP), run_name="__main__")

    except BaseException:
        details = traceback.format_exc()
        text = (
            f"DDO Combat Analyzer startup failure\n"
            f"{datetime.now().isoformat(timespec='seconds')}\n\n"
            f"Python: {sys.executable}\n"
            f"App: {APP}\n\n{details}"
        )
        try:
            LOG.write_text(text, encoding="utf-8")
        except Exception:
            pass
        show_error(
            "DDO Combat Analyzer could not start.\n\n"
            "A diagnostic file named 'startup_error.log' has been created in the program folder.\n\n"
            "Please send that file when asking for help."
        )
        raise


if __name__ == "__main__":
    main()
