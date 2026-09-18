#!/usr/bin/env python3
"""Host capacity, so the console can suggest a sane concurrency.

检测程序的开销是「同时打开几条 IMAP 连接」，不是「同时开几个浏览器」：一条
连接读信时驻留约 5-10MB，瓶颈在 CPU 与上游的并发限制上，不在内存。所以这里
按内存留够余量、再按核数收敛，推荐值偏低 —— 并发开太多只会让几个邮箱同时
报超时。
"""
import hashlib
import os
import platform
import sys
import time

# Reserved so the console itself and the system keep working.
HEADROOM_MB = 512
MB_PER_WORKER = {"mail": 16}
MAX_WORKERS = 8


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


def recommend_threads(mode="mail", running_now=0):
    """A concurrency this machine can actually sustain.

    Bounded by both memory and cores, and never below 1. `running_now` is
    subtracted so the advice reflects what is left, not the idle machine.
    """
    total, avail = memory()
    cores = cpu_count()
    per = MB_PER_WORKER.get(mode, MB_PER_WORKER["mail"])
    if avail is None:
        return max(1, min(cores, 2)), "内存信息不可读，按 CPU 核数保守估计"
    usable = max(0, avail - HEADROOM_MB - running_now * per)
    by_mem = usable // per
    n = max(1, min(cores, MAX_WORKERS, by_mem))
    reason = (f"可用 {avail} MB / {total} MB · {cores} 核 · "
              f"按 {per} MB/连接预留 {HEADROOM_MB} MB")
    return n, reason


def summary(mode="mail"):
    total, avail = memory()
    n, why = recommend_threads(mode)
    return {"total_mb": total, "available_mb": avail,
            "cores": cpu_count(), "recommended": n, "reason": why}


def build_stamp():
    """Which build of this program is running, and on what.

    A report is only actionable if it says which code produced it - a stale
    image and a genuinely broken host need opposite fixes, and the log lines
    around a failure look identical either way. The digest covers every module
    in core/, so "did the container actually pick up my change" is one line of
    log instead of a guess.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        digest = hashlib.sha256()
        newest = 0.0
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(here, name)
            with open(path, "rb") as fh:
                digest.update(fh.read())
            newest = max(newest, os.path.getmtime(path))
        tag = digest.hexdigest()[:12]
        when = time.strftime("%m-%d %H:%M", time.localtime(newest))
    except Exception:
        tag, when = "?", "?"
    return (f"core@{tag} ({when}) · py{platform.python_version()} · "
            f"{cpu_count()} cores")
