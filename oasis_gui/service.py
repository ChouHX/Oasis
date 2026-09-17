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

    OASIS_DB            /data/oasis.db   账号池与配置的存放位置（必填）
    OASIS_WEB_PASSWORD  管理界面密码（必填 —— 不设服务拒绝启动）

    Everything else is configured in the admin UI and persisted to
    <db dir>/oasis_config.json, so it survives a restart:
    proxy pool, thread count, show preference, timeouts, Google split,
    iCloud service address and password, debug logging.

    The remaining OASIS_* variables below still work as first-boot seeds -
    they are only written into the config file when it has no value yet, so
    they never fight with what was set in the browser.

    OASIS_PROXIES       seed the proxy pool
    OASIS_THREADS       seed the worker count (unset = sized to the machine)
    OASIS_SHOWS         seed the venue preference order
    OASIS_MODE          accepted for compatibility; browser is the only flow
    OASIS_LINK_TIMEOUT  seconds to wait for the verification mail
    OASIS_SUCCESS_TIMEOUT  seconds to wait for the success mail when the page
                        did not confirm (a page confirmation only waits 30s)
    OASIS_DEBUG         "1" logs tracebacks on failure
    OASIS_FRONT_PROXY / OASIS_MAIL_PROXY / OASIS_GOOGLE_PROXY / OASIS_HME_BASE
    OASIS_HME_PASSWORD  seeds for the matching settings
    OASIS_CONFIG        where the runtime settings live (default: next to the db)
    OASIS_WEB_DIST      built frontend directory
    OASIS_IMPORT        credential file imported at boot
    OASIS_IDLE          seconds to sleep when the queue is empty
    OASIS_PORT          status/admin HTTP port
    OASIS_ONESHOT       "1" = drain the queue then exit instead of looping
"""
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import registrar, sysinfo                # noqa: E402
from core.webui import LogRing, WebAdmin            # noqa: E402
from core.engine import Engine                     # noqa: E402
from core.config import Config                      # noqa: E402
from core.proxy_pool import ProxyPool              # noqa: E402
from core.store import Store                       # noqa: E402

STOP = threading.Event()      # shutdown
WAKE = threading.Event()      # "start a round now", set by the web UI
# Set while the operator has asked the run to stop. Engine.stop() only ends the
# current round - without this the outer loop would immediately start another
# one, because the queue is still non-empty. Cleared by the Start button.
PAUSED = threading.Event()
LOGS = LogRing()
# Counts transport-level failures, which never show up in the account stats.
REQUESTS = 0
# Pause after a round that registered nothing but did fail accounts.
BACKOFF = 60


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
    LOGS.add(level, msg)


# env var -> config key, for the keys an operator may also edit in the web UI.
# Only variables actually set are written into the file, so a deployment that
# sets none of them still gets the DEFAULTS.
ENV_KEYS = {
    "OASIS_MODE": "mode",
    "OASIS_SHOWS": "shows",
    "OASIS_THREADS": "threads",
    "OASIS_LINK_TIMEOUT": "link_timeout",
    "OASIS_DELAY_BETWEEN": "delay_between",
    "OASIS_DEBUG": "debug",
    "OASIS_VERIFY_SUCCESS": "verify_success",
    "OASIS_SUCCESS_TIMEOUT": "success_timeout",
    "OASIS_FRONT_PROXY": "front_proxy",
    "OASIS_MAIL_PROXY": "mail_proxy",
    "OASIS_GOOGLE_PROXY": "google_proxy",
    "OASIS_HME_BASE": "hme_base",
    "OASIS_HME_PASSWORD": "hme_password",
}
_INT_KEYS = ("threads", "link_timeout", "success_timeout", "delay_between")
_BOOL_KEYS = ("verify_success", "debug")


def build_config():
    """Returns (conf, boot).

    `conf` is the persisted Config that the engine and the web UI both read, so
    an edit made in the browser survives a restart. `boot` holds the two values
    that stay deployment-only - the account pool path and the proxy list - plus
    the resolved thread count.

    Precedence: an env var that is actually set wins on boot and is written
    into the file; anything else keeps whatever the file already had. That way
    .env describes the deployment, and the web UI describes the run.
    """
    boot = {
        "db_path": env("OASIS_DB", "/data/oasis.db"),
        "proxies": split_list(env("OASIS_PROXIES")),
    }
    cfg_path = env("OASIS_CONFIG") or os.path.join(
        os.path.dirname(boot["db_path"]) or ".", "oasis_config.json")
    fresh = not os.path.exists(cfg_path)
    conf = Config(cfg_path)

    if fresh:
        # First boot starts from the service's own defaults, not the desktop
        # console's. The desktop ships a fixed 4 threads for its window; an
        # unattended server wants its thread count derived from the machine.
        conf.data["mode"] = "browser"
        conf.data["threads"] = 0
        conf.data["shows"] = list(registrar.DEFAULT_ORDER)

    for var, key in ENV_KEYS.items():
        raw = os.environ.get(var)
        if raw is None or not raw.strip():
            continue
        if key == "shows":
            conf.data[key] = split_list(raw)
        elif key in _INT_KEYS:
            conf.data[key] = env_int(var, conf.get(key))
        elif key in _BOOL_KEYS:
            conf.data[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            conf.data[key] = raw.strip()

    if not conf.get("threads"):
        n, note = sysinfo.recommend_threads(conf.get("mode", "browser"))
        conf.data["threads"] = n
        log("info", f"OASIS_THREADS unset -> {n} worker(s) ({note})")
    # Reflect the real pool path, so the settings page shows what is in use
    # rather than whatever the shared DEFAULTS happen to say.
    conf.data["db_path"] = boot["db_path"]

    # The proxy list lives in the config file, the same way the desktop console
    # keeps it, so it can be edited from the web UI. OASIS_PROXIES only seeds
    # that file when it is still empty - otherwise every restart would undo
    # whatever was set in the browser.
    if boot["proxies"] and not conf.get("proxies"):
        conf.data["proxies"] = list(boot["proxies"])
        log("info", f"seeded {len(boot['proxies'])} proxy line(s) from "
                    f"OASIS_PROXIES into {cfg_path}")
    conf.save()
    boot["proxies"] = list(conf.get("proxies") or [])
    return conf, boot


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
    """Everything goes through WebAdmin, including auth."""

    def _serve(self, method):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = None
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = None
        status, headers, payload = ADMIN.handle(
            self, method, parsed.path,
            urllib.parse.parse_qs(parsed.query), body)
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def log_message(self, *a):
        pass


class Controller:
    """What the web UI may do to the engine - start a round, stop it, ask."""

    def __init__(self):
        self.engine = None

    def running(self):
        # A paused run is not running, even though the queue is not empty.
        if PAUSED.is_set():
            return False
        return bool(self.engine and (self.engine.running or self.engine.busy))

    def start(self, threads, mode):
        PAUSED.clear()
        WAKE.set()

    def stop(self):
        PAUSED.set()
        if self.engine:
            self.engine.stop()


ADMIN = None


def on_event(kind, payload=None):
    global REQUESTS
    payload = payload or {}
    if kind == "requeued":
        REQUESTS += 1
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
    elif kind == "requeued":
        # Transport failure: the account is back in the queue untouched. Counted
        # separately from failures so the back-off below can see it - a dead
        # proxy produces zero failures and would otherwise spin forever.
        log("warn", f"REQUEUED {payload.get('email', '?')} - "
                    f"{str(payload.get('error', ''))[:140]}")
        STATUS.bump("requeued")
    elif kind == "stopped":
        STATUS.set(state="idle", stats=payload.get("stats", {}))
    STATUS.set(last_event={"kind": kind, "at": time.time(), "payload": payload})


def main():
    conf, boot = build_config()
    if not boot["proxies"]:
        # Not fatal: the proxy pool is editable from the admin UI, so a fresh
        # deployment can come up empty, get its accounts imported and its
        # proxies pasted in without ever touching .env.
        log("warn", "no proxies configured yet - set them in the admin UI "
                    "(代理池页) or via OASIS_PROXIES; rounds will fail until then")
    if not conf.get("google_proxy"):
        log("warn", "no OASIS_GOOGLE_PROXY: reCAPTCHA cannot load unless the "
                    "upstream itself reaches Google - browser mode will stall")

    store = Store(boot["db_path"])
    log("info", f"db {boot['db_path']}  stats={store.stats()}")

    imported = env("OASIS_IMPORT")
    if imported and os.path.exists(imported):
        lines = [l for l in open(imported, encoding="utf-8").read().splitlines()
                 if l.strip()]
        added, dup = store.add_mailboxes(lines, "auto")
        log("info", f"imported {added} new account(s) from {imported} ({dup} dup)")

    pool = ProxyPool(boot["proxies"])
    log("info", f"{len(pool)} proxy line(s); front={conf.get('front_proxy') or '-'} "
                f"mode={conf.get('mode')} threads={conf.get('threads')} "
                f"google={conf.get('google_proxy') or '-'} "
                f"hme={conf.get('hme_base')}")

    engine = Engine(store, pool, log, on_event, conf)
    controller = Controller()
    controller.engine = engine

    global ADMIN
    # Built React bundle. CI bakes it into the image at /app/webui_dist; a
    # source checkout can point OASIS_WEB_DIST at webui/dist.
    static_dir = env("OASIS_WEB_DIST") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "webui_dist")
    if not os.path.isfile(os.path.join(static_dir, "index.html")):
        log("warn", f"no built frontend at {static_dir} - the admin UI will "
                    f"show build instructions. Run `npm run build` in webui/, "
                    f"or use the published image.")
    ADMIN = WebAdmin(store, conf, STATUS, pool, LOGS, controller,
                     password=env("OASIS_WEB_PASSWORD"), static_dir=static_dir,
                     log=log)

    oneshot = env("OASIS_ONESHOT") == "1"
    idle = env_int("OASIS_IDLE", 30)
    port = env_int("OASIS_PORT", 8080)
    srv = None
    if port and not env("OASIS_WEB_PASSWORD"):
        log("error", "OASIS_WEB_PASSWORD is not set - the admin UI exposes the "
                     "account pool and can start/stop the engine, so it will "
                     "not be served without a password. Set it in .env.")
        return 2
    if port:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="status-http").start()
        log("info", f"admin UI on :{port}  (/, /health, /stats)")

    def shutdown(signum, _frame):
        log("info", f"signal {signum}: draining workers, then exiting")
        STOP.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    STATUS.set(state="idle", threads=conf.get("threads"), stats=store.stats())
    rounds = 0
    while not STOP.is_set():
        stats = store.stats()
        pending = stats.get("pending", 0)
        if not pending:
            STATUS.set(state="idle", stats=stats)
            if oneshot:
                log("info", "queue empty and OASIS_ONESHOT=1 - done")
                break
            WAKE.wait(idle)
            WAKE.clear()
            continue

        # A stop request means "stop", not "finish this round and begin the
        # next one": the queue is still full, so without this the loop would
        # relaunch immediately after the workers drained.
        if PAUSED.is_set():
            STATUS.set(state="paused", stats=stats)
            WAKE.wait(idle)
            WAKE.clear()
            continue
        # Starting a round with an empty pool would mark every claimed account
        # failed for a reason that has nothing to do with the account. Wait for
        # the pool instead.
        if not len(pool):
            STATUS.set(state="idle", stats=stats)
            log("warn", "queue has work but the proxy pool is empty - waiting; "
                        "add proxies in the admin UI (代理池页)")
            WAKE.wait(idle)
            WAKE.clear()
            continue

        # Counted only once we are actually about to run, so the pause loop
        # above does not inflate it.
        rounds += 1
        before = stats                      # for the back-off decision below
        requests_before = REQUESTS
        STATUS.set(state="running", rounds=rounds, stats=stats)
        # read per round, so a change made in the web UI applies from the
        # next round without a restart
        threads = int(conf.get("threads") or 1)
        log("info", f"round {rounds}: {pending} pending, {threads} worker(s)")
        try:
            # Everything here is read fresh each round, so a change made in the
            # web UI applies from the next round without a restart.
            engine.start(threads=threads, order=conf.get("shows"),
                         link_timeout=int(conf.get("link_timeout") or 240),
                         delay_between=float(conf.get("delay_between") or 0),
                         mode=conf.get("mode"))
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

        # A round that registered nothing and failed something is almost always
        # a transport problem (dead proxies, no egress, site blocking). Without
        # a pause the loop spins: with fast failures it can burn through the
        # whole queue in seconds and mark every account failed for a reason
        # that is not about the accounts.
        #
        # Skipped when the operator asked to stop - the loop's own pause check
        # handles that, and blocking here would leave the UI showing "idle"
        # instead of "paused" for a whole minute.
        done = store.stats()
        gained = (done.get("registered", 0) + done.get("submitted", 0)
                  - before.get("registered", 0) - before.get("submitted", 0))
        lost = done.get("failed", 0) - before.get("failed", 0)
        stuck = REQUESTS - requests_before      # transport failures this round
        if gained <= 0 and (lost > 0 or stuck > 0) and not PAUSED.is_set():
            why = []
            if lost:
                why.append(f"{lost} 个被拒")
            if stuck:
                why.append(f"{stuck} 次传输失败（账号已退回队列）")
            log("warn", f"round {rounds}: 零成功，" + "、".join(why) +
                        f" —— 退避 {BACKOFF}s 再试（检查代理池）")
            WAKE.wait(BACKOFF)
            WAKE.clear()

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
