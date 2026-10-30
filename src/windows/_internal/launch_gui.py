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


def show_error(message: str):
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "DDO Combat Analyzer - Startup Error",
            0x10 | 0x0,
        )
    except Exception:
        pass


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
    # Use ShellExecuteW directly from Python. This avoids the quoting/path
    # problems that occur when elevation is attempted from a CMD file.
    script = str(Path(__file__).resolve())
    params = subprocess.list2cmdline([script, "--elevated"])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, str(HERE), 1
    )
    return int(rc) > 32




_MUTEX_HANDLE = None


def acquire_single_instance():
    """Allow only one elevated analyzer GUI instance per Windows session.

    This uses a normal named mutex. It does not enumerate, inspect, terminate,
    inject into, or otherwise manipulate any other process.
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, "Local\\DDOCombatAnalyzer_GUI_v1")
        if not handle:
            return True
        already_exists = int(ctypes.get_last_error()) == ERROR_ALREADY_EXISTS
        if already_exists:
            kernel32.CloseHandle(handle)
            show_error("DDO Combat Analyzer is already running.")
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        # Failure to create the convenience mutex must never prevent startup.
        return True


def main():
    try:
        if os.name == "nt" and not is_admin():
            if not elevate_self():
                show_error(
                    "DDO Combat Analyzer needs Administrator privileges so the "
                    "Pause/Break recording hotkey can work while DDO has focus."
                )
            return

        # Remove an old startup error so its presence always refers to this run.
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
            f"App: {APP}\n\n"
            f"{details}"
        )
        try:
            LOG.write_text(text, encoding="utf-8")
        except Exception:
            pass

        show_error(
            "DDO Combat Analyzer could not start.\n\n"
            "A diagnostic file named 'startup_error.log' has been created "
            "in the program folder.\n\n"
            "Please send that file when asking for help."
        )
        raise


if __name__ == "__main__":
    main()
