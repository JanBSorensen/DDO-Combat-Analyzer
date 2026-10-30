from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


def _windows_physical_cores():
    """Return physical core count using GetLogicalProcessorInformationEx."""
    try:
        RelationProcessorCore = 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        func = kernel32.GetLogicalProcessorInformationEx
        func.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        func.restype = ctypes.c_bool

        needed = ctypes.c_ulong(0)
        func(RelationProcessorCore, None, ctypes.byref(needed))
        if needed.value <= 0:
            return None

        buf = ctypes.create_string_buffer(needed.value)
        if not func(RelationProcessorCore, ctypes.byref(buf), ctypes.byref(needed)):
            return None

        count = 0
        offset = 0
        total = int(needed.value)
        while offset + 8 <= total:
            relationship = ctypes.c_uint32.from_buffer(buf, offset).value
            size = ctypes.c_uint32.from_buffer(buf, offset + 4).value
            if size < 8 or offset + size > total:
                break
            if relationship == RelationProcessorCore:
                count += 1
            offset += size
        return count or None
    except Exception:
        return None


def _linux_physical_cores():
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        pairs = set()
        for block in text.split("\n\n"):
            fields = {}
            for line in block.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip().lower()] = value.strip()
            if "physical id" in fields and "core id" in fields:
                pairs.add((fields["physical id"], fields["core id"]))
        if pairs:
            return len(pairs)
    except Exception:
        pass
    return None


def _mac_physical_cores():
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "hw.physicalcpu"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        value = int(out)
        return value if value > 0 else None
    except Exception:
        return None


def physical_core_count():
    """Best-effort physical CPU core count, with a safe logical-CPU fallback."""
    count = None
    if os.name == "nt":
        count = _windows_physical_cores()
    elif sys.platform.startswith("linux"):
        count = _linux_physical_cores()
    elif sys.platform == "darwin":
        count = _mac_physical_cores()

    if not count:
        count = os.cpu_count() or 1
    return max(1, int(count))


def resolve_indra_threads(setting):
    """Resolve the user setting to an actual Indra worker count.

    Auto policy requested for DDO Combat Analyzer:
      - 1-2 physical CPU cores -> 2 Indra threads
      - 3+ physical CPU cores  -> 4 Indra threads
    """
    raw = str(setting if setting is not None else "auto").strip().lower()
    if raw in ("", "auto"):
        return 2 if physical_core_count() <= 2 else 4

    try:
        value = int(raw)
    except Exception:
        return 2 if physical_core_count() <= 2 else 4
    return max(1, min(8, value))
