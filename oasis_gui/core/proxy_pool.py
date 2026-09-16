#!/usr/bin/env python3
"""Proxy pool with health tracking.

Accepts the formats an operator actually pastes:

    http://user:pass@host:port
    socks5://user:pass@host:port
    host:port
    host:port:user:pass

Entries are handed out round-robin. A proxy that fails repeatedly is put on a
short cooldown instead of being dropped, because the residential upstream here
fails intermittently rather than permanently.
"""
import threading
import time

COOLDOWN_AFTER = 3          # consecutive failures before a rest
COOLDOWN_SECONDS = 45


def parse_proxy(line):
    """One line -> normalised proxy url, or None when unparseable."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
            scheme = "http"
        return f"{scheme}://{rest}"

    parts = line.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    return None


class ProxyPool:
    def __init__(self, lines=None):
        self._lock = threading.Lock()
        self._entries = []          # list of dicts
        self.load(lines or [])

    def load(self, lines):
        with self._lock:
            self._entries = []
            seen = set()
            for raw in lines:
                url = parse_proxy(raw)
                if not url or url in seen:
                    continue
                seen.add(url)
                self._entries.append({"url": url, "ok": 0, "fail": 0,
                                      "strikes": 0, "rest_until": 0.0,
                                      "last_error": ""})
        return len(self._entries)

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def urls(self):
        with self._lock:
            return [e["url"] for e in self._entries]

    def acquire(self):
        """Pick a proxy, preferring the least-used healthy entry."""
        with self._lock:
            if not self._entries:
                return None
            now = time.time()
            healthy = [e for e in self._entries if e["rest_until"] <= now]
            if not healthy:
                # everything is resting: take the one that wakes up soonest
                healthy = [min(self._entries, key=lambda e: e["rest_until"])]
            best = min(healthy, key=lambda e: e["ok"] + e["fail"] * 2)
            return best["url"]

    def report(self, url, success, error=""):
        with self._lock:
            for e in self._entries:
                if e["url"] != url:
                    continue
                if success:
                    e["ok"] += 1
                    e["strikes"] = 0
                    e["last_error"] = ""
                else:
                    e["fail"] += 1
                    e["strikes"] += 1
                    e["last_error"] = str(error)[:160]
                    if e["strikes"] >= COOLDOWN_AFTER:
                        e["rest_until"] = time.time() + COOLDOWN_SECONDS
                        e["strikes"] = 0
                break

    def snapshot(self):
        with self._lock:
            now = time.time()
            return [{"url": e["url"], "ok": e["ok"], "fail": e["fail"],
                     "cooling": e["rest_until"] > now,
                     "last_error": e["last_error"]} for e in self._entries]


if __name__ == "__main__":
    p = ProxyPool([
        "http://user:pass@PROXY_HOST:2260",
        "1.2.3.4:8080",
        "5.6.7.8:1080:user:pw",
        "socks5://u:p@9.9.9.9:1080",
        "garbage",
    ])
    print("loaded:", len(p))
    for u in p.urls():
        print("  ", u)
    print("pick:", p.acquire())
    p.report(p.acquire(), False, "boom")
    print("snapshot:", p.snapshot()[0])
