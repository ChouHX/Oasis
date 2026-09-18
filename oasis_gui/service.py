#!/usr/bin/env python3
"""Headless runner for the Oasis hit monitor: the same engine, no GUI.

Why this exists
---------------
The desktop console owns the sweep through a PyQt window. A server deployment
does not want a window: it wants a process that keeps watching mailboxes,
survives a restart, and can be monitored. This entry point drives
`core.monitor.HitMonitor` in a loop and serves a small status endpoint.

Configuration is environment-driven so one image serves any account pool; the
SQLite file stays the single source of truth, so a restart simply resumes - and
a mailbox that was already read is not re-read from scratch.

    OASIS_DB            /data/oasis.db   账号池与配置的存放位置（必填）
    OASIS_WEB_PASSWORD  管理界面密码（必填 —— 不设服务拒绝启动）

    Everything else is configured in the admin UI and persisted to
    <db dir>/oasis_config.json, so it survives a restart: poll interval,
    concurrency, look-back window, mail proxy, iCloud service, debug logging.

    The remaining OASIS_* variables below still work as first-boot seeds -
    they are only written into the config file when it has no value yet, so
    they never fight with what was set in the browser.

    OASIS_INTERVAL      seconds between sweeps (default 300)
    OASIS_THREADS       seed the concurrency (unset = sized to the machine)
    OASIS_LOOKBACK_DAYS how far back the first sweep looks (default 30, 0 = all)
    OASIS_PER_PAGE      messages read per mailbox (default 20)
    OASIS_SKIP_HITS     "0" keeps checking mailboxes that already hit
    OASIS_DEBUG         "1" logs tracebacks on failure
    OASIS_MAIL_PROXY / OASIS_HME_BASE / OASIS_HME_PASSWORD
                        seeds for the matching settings
    OASIS_CONFIG        where the runtime settings live (default: next to the db)
    OASIS_WEB_DIST      built frontend directory
    OASIS_IMPORT        credential file imported at boot
    OASIS_IDLE          seconds to sleep when the pool is empty
    OASIS_PORT          status/admin HTTP port
    OASIS_ONESHOT       "1" = run one sweep then exit instead of looping
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

from core import sysinfo                          # noqa: E402
from core.webui import LogRing, WebAdmin          # noqa: E402
from core.monitor import HitMonitor               # noqa: E402
from core.config import Config                    # noqa: E402
from core.store import Store                      # noqa: E402

STOP = threading.Event()      # shutdown
WAKE = threading.Event()      # "start a sweep now", set by the web UI
# Set while the operator has asked the run to stop. The monitor's own stop() only
# ends the sweep in flight - without this the outer loop would immediately start
# another one, because there are still mailboxes to read. Cleared by Start.
PAUSED = threading.Event()
LOGS = LogRing()
STATUS = None                 # assigned in main()


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
    "OASIS_INTERVAL": "interval",
    "OASIS_THREADS": "threads",
    "OASIS_LOOKBACK_DAYS": "lookback_days",
    "OASIS_PER_PAGE": "per_page",
    "OASIS_LIMIT": "limit",
    "OASIS_DEBUG": "debug",
    "OASIS_MAIL_PROXY": "mail_proxy",
    "OASIS_HME_BASE": "hme_base",
    "OASIS_HME_PASSWORD": "hme_password",
    "OASIS_ALIAS_INBOX": "alias_inbox",
    "OASIS_ALIAS_PASSWORD": "alias_inbox_password",
}
_INT_KEYS = ("interval", "threads", "lookback_days", "per_page", "limit")
_BOOL_KEYS = ("debug", "skip_hits")


def build_config():
    """Returns (conf, boot).

    `conf` is the persisted Config that the monitor and the web UI both read, so
    an edit made in the browser survives a restart. `boot` holds the values that
    stay deployment-only - the account pool path and the resolved concurrency.

    Precedence: an env var that is actually set wins on boot and is written
    into the file; anything else keeps whatever the file already had. That way
    .env describes the deployment, and the web UI describes the run.
    """
    boot = {"db_path": env("OASIS_DB", "/data/oasis.db")}
    cfg_path = env("OASIS_CONFIG") or os.path.join(
        os.path.dirname(boot["db_path"]) or ".", "oasis_config.json")
    fresh = not os.path.exists(cfg_path)
    conf = Config(cfg_path)

    if fresh:
        # First boot starts from the service's own defaults, not the desktop
        # console's. An unattended server wants its concurrency derived from the
        # machine rather than pinned at the desktop's 2.
        conf.data["threads"] = 0

    for var, key in ENV_KEYS.items():
        raw = os.environ.get(var)
        if raw is None or not raw.strip():
            continue
        if key in _INT_KEYS:
            conf.data[key] = env_int(var, conf.get(key))
        elif key in _BOOL_KEYS:
            conf.data[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            conf.data[key] = raw.strip()
    if env("OASIS_SKIP_HITS"):
        conf.data["skip_hits"] = env("OASIS_SKIP_HITS").strip().lower() in (
            "1", "true", "yes", "on")

    if not conf.get("threads"):
        n, note = sysinfo.recommend_threads("mail")
        conf.data["threads"] = n
        log("info", f"OASIS_THREADS unset -> {n} concurrent mailbox(es) ({note})")
    # Reflect the real pool path, so the settings page shows what is in use
    # rather than whatever the shared DEFAULTS happen to say.
    conf.data["db_path"] = boot["db_path"]
    conf.save()
    return conf, boot


class Status:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"state": "starting", "started": time.time(), "sweeps": 0,
                     "hits": 0, "checked": 0, "check_failed": 0,
                     "threads": 0, "interval": 0, "last_event": None,
                     "stats": {}, "monitor": {}, "host": sysinfo.summary("mail")}

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)

    def bump(self, key, n=1):
        with self.lock:
            self.data[key] = self.data.get(key, 0) + n

    def snapshot(self):
        with self.lock:
            d = dict(self.data)
        d["uptime"] = round(time.time() - d["started"], 1)
        return d


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
    """What the web UI may do to the monitor - sweep now, pause, ask."""

    def __init__(self):
        self.monitor = None

    def running(self):
        # A paused run is not running, even though there is still work to do.
        if PAUSED.is_set():
            return False
        return bool(self.monitor and self.monitor.running)

    def start(self, threads=None, interval=None):
        PAUSED.clear()
        WAKE.set()

    def stop(self):
        PAUSED.set()
        if self.monitor:
            self.monitor.stop()
            self.monitor.join()


ADMIN = None


def on_event(kind, payload=None):
    payload = payload or {}
    if kind == "hit":
        # 详细那行已经由 monitor 打过了（桌面版也靠它写日志页），这里只计数，
        # 否则同一次中签会在 stdout 里出现两遍。
        STATUS.bump("hits")
    elif kind == "checked":
        STATUS.bump("checked")
    elif kind == "check_failed":
        # 那一行由 monitor 打过了（桌面版也靠它写日志页），这里只计数。
        STATUS.bump("check_failed")
    elif kind == "sweep":
        log("info", f"第 {payload.get('round')} 轮结束："
                    f"{payload.get('targets')} 个账号，新中签 "
                    f"{payload.get('hits')}")
        # stats 一定要一起更新：统计卡读的是它，只更新 monitor 会让仪表盘停在
        # 轮次开始时的快照上，看着像「一轮跑了半天一个都没查」。
        STATUS.set(sweeps=payload.get("round", 0),
                   stats=payload.get("stats") or {},
                   monitor=payload.get("monitor") or {})
    elif kind == "stopped":
        STATUS.set(state="idle", stats=payload.get("stats", {}),
                   monitor=payload.get("monitor") or {})
    STATUS.set(last_event={"kind": kind, "at": time.time(), "payload": payload})


def main():
    global STATUS, ADMIN
    conf, boot = build_config()

    store = Store(boot["db_path"])
    stats = store.stats()
    log("info", f"db {boot['db_path']}  stats={stats}")
    # Printed once at boot so a report carries the build and the host facts.
    log("info", f"build {sysinfo.build_stamp()}")
    if stats.get("hits"):
        log("info", f"名单里已有 {stats['hits']} 个中签账号（含上一版程序并入的）")
    if not stats.get("total"):
        log("warn", "账号池是空的 - 到管理界面的「邮箱池」导入账号")

    imported = env("OASIS_IMPORT")
    if imported and os.path.exists(imported):
        lines = [l for l in open(imported, encoding="utf-8").read().splitlines()
                 if l.strip()]
        added, dup = store.add_mailboxes(lines, "auto")
        log("info", f"imported {added} new account(s) from {imported} ({dup} dup)")

    monitor = HitMonitor(store, log, on_event, conf)
    controller = Controller()
    controller.monitor = monitor
    STATUS = Status()
    STATUS.set(threads=conf.get("threads"), interval=conf.get("interval"),
               stats=store.stats())

    # Built React bundle. CI bakes it into the image at /app/webui_dist; a
    # source checkout can point OASIS_WEB_DIST at webui/dist.
    static_dir = env("OASIS_WEB_DIST") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "webui_dist")
    if not os.path.isfile(os.path.join(static_dir, "index.html")):
        log("warn", f"no built frontend at {static_dir} - the admin UI will "
                    f"show build instructions. Run `npm run build` in webui/, "
                    f"or use the published image.")
    ADMIN = WebAdmin(store, conf, STATUS, LOGS, controller,
                     password=env("OASIS_WEB_PASSWORD"), static_dir=static_dir,
                     log=log)

    oneshot = env("OASIS_ONESHOT") == "1"
    idle = env_int("OASIS_IDLE", 5)
    port = env_int("OASIS_PORT", 8080)
    srv = None
    if port and not env("OASIS_WEB_PASSWORD"):
        log("error", "OASIS_WEB_PASSWORD is not set - the admin UI exposes the "
                     "account pool and can start/stop the monitor, so it will "
                     "not be served without a password. Set it in .env.")
        return 2
    if port:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="status-http").start()
        log("info", f"admin UI on :{port}  (/, /health)")

    def shutdown(signum, _frame):
        log("info", f"signal {signum}: finishing the sweep, then exiting")
        STOP.set()
        WAKE.set()
        monitor.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    sweeps = 0
    while not STOP.is_set():
        if not store.stats().get("total"):
            STATUS.set(state="idle")
            if oneshot:
                log("info", "账号池为空且 OASIS_ONESHOT=1 - 结束")
                break
            WAKE.wait(idle * 4)
            WAKE.clear()
            continue
        # A stop request means "stop", not "finish this sweep and begin the
        # next one": there is still mail to read, so without this the loop
        # would relaunch immediately after the sweep drained.
        if PAUSED.is_set():
            STATUS.set(state="paused", stats=store.stats())
            WAKE.wait(idle * 4)
            WAKE.clear()
            continue

        sweeps += 1
        # read per sweep, so a change made in the web UI applies from the
        # next sweep without a restart
        interval = max(5, int(conf.get("interval") or 300))
        STATUS.set(state="running", sweeps=sweeps, stats=store.stats(),
                   threads=conf.get("threads"), interval=interval,
                   monitor=monitor.stats())
        log("info", f"巡检 {sweeps}：并发 {conf.get('threads')} · "
                    f"间隔 {interval}s · 回看 {conf.get('lookback_days')} 天"
                    + (f" · 本轮上限 {conf.get('limit')} 个"
                       if conf.get("limit") else ""))
        try:
            monitor.start(threads=int(conf.get("threads") or 2),
                          interval=interval,
                          lookback_days=float(conf.get("lookback_days") or 0),
                          per_page=int(conf.get("per_page") or 20),
                          skip_hits=bool(conf.get("skip_hits", True)),
                          limit=int(conf.get("limit") or 0),
                          rounds=1)
            monitor.join()
        except Exception as e:
            log("error", f"巡检 {sweeps} 崩了：{type(e).__name__}: {e}")
            time.sleep(5)
        STATUS.set(stats=store.stats(), monitor=monitor.stats())
        if STOP.is_set() or oneshot:
            break
        # Wait out the interval, but let Start/Stop and a new sweep cut it short:
        # "立刻检查" that waits five minutes is not a button.
        WAKE.wait(interval)
        WAKE.clear()

    STATUS.set(state="stopping")
    if srv:
        srv.shutdown()
    log("info", f"stopped after {sweeps} sweep(s); stats={store.stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
