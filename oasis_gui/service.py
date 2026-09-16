#!/usr/bin/env python3
"""Headless runner for the Oasis console: the same engine, no GUI.

Why this exists
---------------
The desktop console owns the queue through a PyQt window. A server deployment
does not want a window: it wants a process that keeps draining the queue,
survives a restart, and can be monitored. This entry point drives
`core.engine.Engine` in a loop and serves a small status endpoint.

Configuration is environment-driven so one image serves any account pool; the
SQLite file stays the single source of truth, so a restart simply resumes.

    OASIS_DB            /data/oasis.db      account pool (mount a volume!)
    OASIS_PROXIES       newline- or comma-separated proxy lines
    OASIS_FRONT_PROXY   optional local proxy every upstream is dialled through
    OASIS_MAIL_PROXY    optional proxy for mailbox fetches
    OASIS_GOOGLE_PROXY  second egress for Google. REQUIRED by browser/hybrid
                        mode - reCAPTCHA cannot load without it.
    OASIS_HME_BASE      iCloud Hide-My-Email service (default 127.0.0.1:8081)
    OASIS_HME_PASSWORD  that service's admin password
    OASIS_MODE          hybrid | browser           (default browser)
    OASIS_THREADS       workers; unset = ask core.sysinfo for a safe number
    OASIS_SHOWS         preference order, e.g. glasgow,manchester,paris
    OASIS_LINK_TIMEOUT  seconds to wait for the mail   (default 240)
    OASIS_IMPORT        optional file of credential lines to import at boot
    OASIS_IDLE          seconds to sleep when the queue is empty (default 30)
    OASIS_PORT          status HTTP port               (default 8080)
    OASIS_ONESHOT       "1" = drain the queue then exit instead of looping
"""
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import registrar, sysinfo                # noqa: E402
from core.engine import Engine                     # noqa: E402
from core.proxy_pool import ProxyPool              # noqa: E402
from core.store import Store                       # noqa: E402

STOP = threading.Event()


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def split_list(raw):
    out = []
    for chunk in raw.replace(",", "\n").splitlines():
        line = chunk.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def log(level, msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}", flush=True)


def build_config():
    mode = env("OASIS_MODE", "browser").lower()
    threads = env_int("OASIS_THREADS", 0)
    note = ""
    if threads <= 0:
        threads, note = sysinfo.recommend_threads(mode)
        log("info", f"OASIS_THREADS unset -> {threads} worker(s) ({note})")
    return {
        "db_path": env("OASIS_DB", "/data/oasis.db"),
        "proxies": split_list(env("OASIS_PROXIES")),
        "front_proxy": env("OASIS_FRONT_PROXY"),
        "mail_proxy": env("OASIS_MAIL_PROXY"),
        "google_proxy": env("OASIS_GOOGLE_PROXY"),
        "hme_base": env("OASIS_HME_BASE", "http://127.0.0.1:8081"),
        "hme_password": env("OASIS_HME_PASSWORD"),
        "mode": mode,
        "threads": threads,
        "shows": split_list(env("OASIS_SHOWS")) or list(registrar.DEFAULT_ORDER),
        "link_timeout": env_int("OASIS_LINK_TIMEOUT", 240),
        "think_time": env_int("OASIS_THINK_TIME", 45),
        "http_timeout": env_int("OASIS_HTTP_TIMEOUT", 60),
        "verify_success": env("OASIS_VERIFY_SUCCESS", "1") == "1",
        "success_timeout": env_int("OASIS_SUCCESS_TIMEOUT", 150),
    }


class Status:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"state": "starting", "started": time.time(), "rounds": 0,
                     "registered": 0, "submitted": 0, "failed": 0,
                     "threads": 0, "last_event": None, "stats": {},
                     "host": sysinfo.summary()}

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)

    def bump(self, key):
        with self.lock:
            self.data[key] = self.data.get(key, 0) + 1

    def snapshot(self):
        with self.lock:
            d = dict(self.data)
        d["uptime"] = round(time.time() - d["started"], 1)
        return d


STATUS = Status()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            snap = STATUS.snapshot()
            body = {"ok": not STOP.is_set(), "state": snap["state"]}
        elif self.path.startswith("/stats"):
            body = STATUS.snapshot()
        else:
            body = {"endpoints": ["/health", "/stats"]}
        raw = json.dumps(body, ensure_ascii=False, indent=1).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def on_event(kind, payload=None):
    payload = payload or {}
    if kind == "registered":
        evidence = payload.get("evidence") or "mail"
        label = "SUBMITTED" if evidence == "page" else "REGISTERED"
        log("ok", f"{label} {payload.get('email', '?')} "
                  f"({payload.get('elapsed')}s, {payload.get('mode')}) - "
                  f"{payload.get('show')} [{evidence}]")
        STATUS.bump("submitted" if evidence == "page" else "registered")
    elif kind == "failed":
        log("error", f"FAILED {payload.get('email', '?')} - "
                     f"{str(payload.get('error', ''))[:160]}")
        STATUS.bump("failed")
    elif kind == "stopped":
        STATUS.set(state="idle", stats=payload.get("stats", {}))
    STATUS.set(last_event={"kind": kind, "at": time.time(), "payload": payload})


def main():
    cfg = build_config()
    if not cfg["proxies"]:
        log("error", "OASIS_PROXIES is empty - nothing to route through")
        return 2
    if cfg["mode"] in ("hybrid", "browser") and not cfg["google_proxy"]:
        log("warn", "no OASIS_GOOGLE_PROXY: reCAPTCHA cannot load unless the "
                    "upstream itself reaches Google - browser mode will stall")

    store = Store(cfg["db_path"])
    log("info", f"db {cfg['db_path']}  stats={store.stats()}")

    imported = env("OASIS_IMPORT")
    if imported and os.path.exists(imported):
        lines = [l for l in open(imported, encoding="utf-8").read().splitlines()
                 if l.strip()]
        added, dup = store.add_mailboxes(lines, "auto")
        log("info", f"imported {added} new account(s) from {imported} ({dup} dup)")

    pool = ProxyPool(cfg["proxies"])
    log("info", f"{len(pool)} proxy line(s); front={cfg['front_proxy'] or '-'} "
                f"mode={cfg['mode']} threads={cfg['threads']} "
                f"google={cfg['google_proxy'] or '-'} hme={cfg['hme_base']}")

    engine = Engine(store, pool, log, on_event, cfg)

    oneshot = env("OASIS_ONESHOT") == "1"
    idle = env_int("OASIS_IDLE", 30)
    port = env_int("OASIS_PORT", 8080)
    srv = None
    if port:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="status-http").start()
        log("info", f"status endpoint on :{port}  (/health, /stats)")

    def shutdown(signum, _frame):
        log("info", f"signal {signum}: draining workers, then exiting")
        STOP.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    STATUS.set(state="idle", threads=cfg["threads"], stats=store.stats())
    rounds = 0
    while not STOP.is_set():
        stats = store.stats()
        pending = stats.get("pending", 0)
        if not pending:
            STATUS.set(state="idle", stats=stats)
            if oneshot:
                log("info", "queue empty and OASIS_ONESHOT=1 - done")
                break
            STOP.wait(idle)
            continue

        rounds += 1
        STATUS.set(state="running", rounds=rounds, stats=stats)
        log("info", f"round {rounds}: {pending} pending, "
                    f"{cfg['threads']} worker(s)")
        try:
            engine.start(threads=cfg["threads"], order=cfg["shows"],
                         link_timeout=cfg["link_timeout"], mode=cfg["mode"])
            while engine.busy and not STOP.is_set():
                time.sleep(1)
            if STOP.is_set():
                engine.stop()
            engine.join()
            engine.finish()
        except Exception as e:
            log("error", f"round {rounds} crashed: {type(e).__name__}: {e}")
            time.sleep(5)
        STATUS.set(stats=store.stats())

    STATUS.set(state="stopping")
    try:
        engine.browser.close_warm()
    except Exception:
        pass
    if srv:
        srv.shutdown()
    log("info", f"stopped after {rounds} round(s); stats={store.stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
