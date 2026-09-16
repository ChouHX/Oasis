#!/usr/bin/env python3
"""Host capacity, so the console can suggest a sane thread count.

Browser mode is the expensive one: every worker gets its own browser context
inside the shared chromium process. Measured on this project: ~200MB per hybrid
worker and ~250MB per browser-mode worker, on top of the shared browser itself
(~250MB) and the Qt UI (~150MB). Recommending more than the machine can hold is
worse than recommending too little: the OS starts swapping and every worker
slows down together.
"""
import os
import sys

# Reserved so the desktop, the console itself and any other browser keep
# working; a run that eats all free memory is slower than a smaller one.
HEADROOM_MB = 1024
MB_PER_WORKER = {"browser": 250, "hybrid": 200}
MAX_WORKERS = 16


def memory():
    """(total_mb, available_mb) or (None, None) when it cannot be read."""
    try:
        if sys.platform == "win32":
            return _memory_windows()
        return _memory_posix()
    except Exception:
        return None, None


def _memory_windows():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None, None
    return st.ullTotalPhys // (1024 * 1024), st.ullAvailPhys // (1024 * 1024)


def _memory_posix():
    info = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key.strip()] = int(rest.split()[0])       # kB
    total = info.get("MemTotal", 0) // 1024
    avail = info.get("MemAvailable",
                     info.get("MemFree", 0) + info.get("Buffers", 0)
                     + info.get("Cached", 0)) // 1024
    return total, avail


def cpu_count():
    try:
        return len(os.sched_getaffinity(0))            # respects cgroup limits
    except AttributeError:
        return os.cpu_count() or 4
    except Exception:
        return os.cpu_count() or 4


def recommend_threads(mode="browser", running_now=0):
    """A worker count this machine can actually sustain.

    Bounded by both memory and cores, and never below 1. `running_now` is
    subtracted so the advice reflects what is left, not the idle machine.
    """
    total, avail = memory()
    cores = cpu_count()
    per = MB_PER_WORKER.get(mode, 200)
    if avail is None:
        return max(1, min(cores, 4)), "内存信息不可读，按 CPU 核数保守估计"
    usable = max(0, avail - HEADROOM_MB - running_now * per)
    by_mem = usable // per
    n = max(1, min(cores, MAX_WORKERS, by_mem))
    reason = (f"可用 {avail} MB / {total} MB · {cores} 核 · "
              f"按 {per} MB/线程预留 {HEADROOM_MB} MB")
    return n, reason


def summary(mode="browser"):
    total, avail = memory()
    n, why = recommend_threads(mode)
    return {"total_mb": total, "available_mb": avail,
            "cores": cpu_count(), "recommended": n, "reason": why}
