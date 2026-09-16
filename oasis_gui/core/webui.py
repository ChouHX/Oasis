#!/usr/bin/env python3
"""Web admin for the headless console.

Mirrors the six pages of the desktop console, over plain `http.server` - no
extra dependency, so the image does not grow and nothing has to be installed on
the box.

Access control is not optional: this can start/stop the engine and read the
account pool, so without a password the server refuses to start rather than
serving that to whoever reaches the port. A session cookie carries the login;
the password is compared in constant time and logins are rate limited.
"""
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from core import mailbox, registrar

from http.server import BaseHTTPRequestHandler

SESSION_COOKIE = "oasis_session"
# 12h: long enough to be convenient from a phone, short enough that a leaked
# cookie stops working the same day.
SESSION_TTL = 12 * 3600
LOGIN_WINDOW = 15 * 60
LOGIN_MAX = 6


def _shows_label(raw):
    """Turn the stored poll-answer uuids into the venue names they stand for.

    `registrations.poll_answer_ids` holds the show *poll* ids, which mean
    nothing to a human. The page previously showed "undefined" here because it
    read fields the query never selected; translating server-side keeps the
    page from having to hard-code uuids that would drift from registrar.SHOWS.
    """
    if raw in (None, "", "[]"):
        return ""
    try:
        ids = json.loads(raw) if isinstance(raw, str) else list(raw)
    except Exception:
        return str(raw)[:60]
    by_poll = {poll: venue for venue, (poll, _answers)
               in registrar.SHOWS.items()}
    out = []
    for pid in ids:
        venue = by_poll.get(str(pid))
        out.append(registrar.SHOW_LABEL.get(venue, str(pid)[:8]) if venue
                   else str(pid)[:8])
    return " > ".join(out)


def split_lines(raw):
    """Proxy/credential lines: newline or comma separated, # comments dropped."""
    out = []
    for chunk in str(raw).replace(",", "\n").splitlines():
        line = chunk.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def mask_url(url):
    """Hide user:pass inside a proxy URL before it reaches the page."""
    return re.sub(r"//[^@/]+@", "//***:***@", str(url or ""))


class LogRing:
    """Recent log lines, so the web UI can show them without reading a file."""

    def __init__(self, capacity=2000):
        self.capacity = capacity
        self._lines = []
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, level, message):
        with self._lock:
            self._seq += 1
            self._lines.append((self._seq, time.time(), level, message))
            if len(self._lines) > self.capacity:
                del self._lines[:len(self._lines) - self.capacity]

    def since(self, seq=0, limit=400):
        with self._lock:
            out = [x for x in self._lines if x[0] > seq]
        return out[-limit:]

    @property
    def seq(self):
        with self._lock:
            return self._seq


class WebAdmin:
    """Routes for the admin UI. `controller` owns the engine lifecycle."""

    def __init__(self, store, config, status, proxies, logs, controller,
                 password="", log=None):
        self.store = store
        self.config = config
        self.status = status
        self.proxies = proxies
        self.logs = logs
        self.controller = controller
        self.password = password or ""
        self.log = log or (lambda lvl, m: None)
        self._sessions = {}
        self._lock = threading.Lock()
        self._attempts = []

    # ------------------------------------------------------------------ auth
    def ok_password(self, given):
        if not self.password:
            return False
        return hmac.compare_digest(given.encode("utf-8"),
                                   self.password.encode("utf-8"))

    def rate_limited(self):
        now = time.time()
        with self._lock:
            self._attempts = [t for t in self._attempts if now - t < LOGIN_WINDOW]
            return len(self._attempts) >= LOGIN_MAX

    def note_attempt(self):
        with self._lock:
            self._attempts.append(time.time())

    def new_session(self):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + SESSION_TTL
        return token

    def valid_session(self, token):
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if not exp:
                return False
            if exp < time.time():
                self._sessions.pop(token, None)
                return False
        return True

    def drop_session(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    # ----------------------------------------------------------------- routes
    def handle(self, handler, method, path, query, body):
        """Dispatch. Returns (status, headers, payload_bytes)."""
        if path == "/login" and method == "POST":
            return self._login(body)
        if path == "/logout":
            token = self._cookie(handler)
            self.drop_session(token)
            return self._json(200, {"ok": True},
                              extra=[("Set-Cookie",
                                      f"{SESSION_COOKIE}=; Path=/; Max-Age=0")])

        if not self.valid_session(self._cookie(handler)):
            if path.startswith("/api/"):
                return self._json(401, {"error": "AUTH_REQUIRED"})
            return self._page()

        try:
            return self._authed(method, path, query, body)
        except Exception as e:
            self.log("error", f"web {path}: {type(e).__name__}: {e}")
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _authed(self, method, path, query, body):
        # The page itself. Without this the post-login reload of "/" fell
        # through to the 404 below, so a correct password still showed
        # {"error": "not found"}.
        if path in ("/", "/index.html"):
            return self._page()

        # Browsers ask for this unprompted; answer quietly instead of logging
        # a 404 on every page load.
        if path == "/favicon.ico":
            return 204, [], b""

        if path == "/api/state":
            info = self.status.snapshot()
            # The config carries the proxy list, credentials and all. The state
            # endpoint only exists to render the dashboard, so it gets the
            # count; the full (masked) list comes from /api/proxies.
            cfg = dict(self.config.data)
            if cfg.get("proxies"):
                cfg["proxies"] = f"{len(cfg['proxies'])} 个（见代理池页）"
            info["config"] = cfg
            info["running"] = self.controller.running()
            info["version"] = 1
            return self._json(200, info)

        if path == "/api/icloud/aliases":
            return self._icloud_catalog()

        if path == "/api/icloud/import" and method == "POST":
            want = {e.strip().lower()
                    for e in ((body or {}).get("emails") or []) if e}
            if not want:
                return self._json(400, {"error": "没有勾选任何别名"})
            try:
                cat = mailbox.hme_catalog(self.config.get("hme_base", ""),
                                          self.config.get("hme_password", ""))
            except mailbox.MailAuthError as e:
                return self._json(502, {"error": str(e)})
            # Rebuild the credential lines from the service's own answer rather
            # than trusting the ids posted by the page.
            lines = [f"{a['email']}----{a['account_id']}----hme"
                     for a in cat if a["email"].lower() in want]
            if not lines:
                return self._json(400, {"error": "勾选的别名在服务上找不到了"})
            added, dup = self.store.add_mailboxes(lines, "hme")
            self.log("info", f"web: imported {added} iCloud alias(es), "
                             f"{dup} already present")
            return self._json(200, {"added": added, "duplicate": dup,
                                    "stats": self.store.stats()})

        if path == "/api/accounts":
            status = query.get("status", [""])[0]
            page = max(1, int(query.get("page", ["1"])[0] or 1))
            per = 50
            rows = self.store.accounts()
            if status:
                rows = [r for r in rows if r["status"] == status]
            total = len(rows)
            return self._json(200, {
                "total": total, "page": page, "per": per,
                "rows": rows[(page - 1) * per: page * per]})

        if path == "/api/registrations":
            rows = self.store.registrations()
            for r in rows:
                r["shows"] = _shows_label(r.get("poll_answer_ids"))
            return self._json(200, {"rows": rows})

        if path == "/api/proxies":
            if method == "POST":
                lines = split_lines((body or {}).get("lines") or "")
                # The page only ever saw masked urls, so a line that still
                # reads //***:***@ means "leave this one alone" - restore the
                # real credential from the current pool instead of overwriting
                # the password with asterisks.
                known = {mask_url(e["url"]): e["url"]
                         for e in self.proxies.snapshot()}
                resolved = [known.get(mask_url(ln), ln) for ln in lines]
                # Persist first: the file is the source of truth, so a restart
                # does not silently revert to whatever OASIS_PROXIES seeded.
                self.config.data["proxies"] = resolved
                self.config.save()
                self.proxies.load(resolved)
                self.log("info", f"web: proxy pool replaced with "
                                 f"{len(resolved)} line(s)")
                return self._json(200, {"ok": True, "total": len(resolved)})
            snap = self.proxies.snapshot()
            return self._json(200, {
                "total": len(snap),
                # Masked here, not just in the page: the password would
                # otherwise travel over the wire on every page load, and show
                # up in any access log or proxy in between.
                "rows": [dict(e, url=mask_url(e["url"])) for e in snap[:500]],
                "lines": [mask_url(e["url"]) for e in snap],
            })

        if path == "/api/accounts/import" and method == "POST":
            lines = split_lines((body or {}).get("lines") or "")
            if not lines:
                return self._json(400, {"error": "没有可导入的行"})
            protocol = (body or {}).get("protocol") or "auto"
            added, dup = self.store.add_mailboxes(lines, protocol)
            self.log("info", f"web: imported {added} account(s), {dup} duplicate")
            return self._json(200, {"added": added, "duplicate": dup,
                                    "stats": self.store.stats()})

        if path == "/api/log":
            seq = int(query.get("since", ["0"])[0] or 0)
            rows = self.logs.since(seq)
            return self._json(200, {"seq": self.logs.seq,
                                    "rows": [{"n": n, "at": at, "level": lv,
                                              "msg": m} for n, at, lv, m in rows]})

        if path == "/api/start" and method == "POST":
            threads = int((body or {}).get("threads") or 0)
            mode = (body or {}).get("mode") or self.config.get("mode", "browser")
            if threads <= 0:
                return self._json(400, {"error": "threads must be > 0"})
            if self.controller.running():
                return self._json(409, {"error": "engine already running"})
            self.config.data["threads"] = threads
            self.config.data["mode"] = mode
            self.config.save()
            self.controller.start(threads, mode)
            return self._json(200, {"ok": True})

        if path == "/api/stop" and method == "POST":
            self.controller.stop()
            return self._json(200, {"ok": True})

        if path == "/api/config" and method == "POST":
            for key, value in (body or {}).items():
                if key in self.config.data:
                    self.config.data[key] = value
            self.config.save()
            return self._json(200, {"ok": True, "config": dict(self.config.data)})

        if path == "/api/accounts/reset" and method == "POST":
            return self._json(200, {"changed": self.store.reset_failed()})

        if path == "/api/accounts/delete" and method == "POST":
            status = (body or {}).get("status") or None
            return self._json(200, {"deleted": self.store.delete_accounts(status)})

        if path == "/api/vacuum" and method == "POST":
            self.store.vacuum()
            return self._json(200, {"ok": True})

        if path == "/api/export":
            what = query.get("what", ["creds"])[0]
            if what == "creds":
                data = "\n".join(self.store.cred_lines()) + "\n"
            else:
                data = json.dumps(self.store.accounts(), ensure_ascii=False,
                                  indent=1)
            return (200, [("Content-Type", "text/plain; charset=utf-8"),
                          ("Content-Disposition",
                           f'attachment; filename="oasis_{what}.txt"')],
                    data.encode("utf-8"))

        # Unknown non-API path: send the browser to the app rather than showing
        # it a JSON 404 (a typo in the address bar should not look like a bug).
        if not path.startswith("/api/"):
            return 302, [("Location", "/")], b""
        return self._json(404, {"error": "not found"})

    def _icloud_catalog(self):
        """Aliases on the iCloud service, flagged with what is already imported.

        Importing the whole list blind wastes aliases that are already spent -
        iCloud caps creation at roughly 10/hour - so the page gets enough to
        let the operator choose.
        """
        try:
            cat = mailbox.hme_catalog(self.config.get("hme_base", ""),
                                      self.config.get("hme_password", ""))
        except mailbox.MailAuthError as e:
            return self._json(502, {"error": str(e)})
        have = {a["email"].lower() for a in self.store.accounts()}
        for a in cat:
            a["imported"] = a["email"].lower() in have
        self.log("info", f"web: listed {len(cat)} iCloud alias(es)")
        return self._json(200, {"total": len(cat), "aliases": cat})

    def _cookie(self, handler):
        raw = handler.headers.get("Cookie") or ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
            morsel = jar.get(SESSION_COOKIE)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _login(self, body):
        if self.rate_limited():
            return self._json(429, {"error": "RATE_LIMITED"})
        given = (body or {}).get("password") or ""
        if not self.ok_password(given):
            self.note_attempt()
            self.log("warn", "web: failed login attempt")
            return self._json(401, {"error": "INVALID_CREDENTIALS"})
        token = self.new_session()
        self.log("info", "web: login ok")
        return self._json(200, {"ok": True},
                          extra=[("Set-Cookie",
                                  f"{SESSION_COOKIE}={token}; Path=/; "
                                  f"HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")])

    def _json(self, status, payload, extra=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8")]
        headers += (extra or [])
        return status, headers, raw

    def _page(self):
        return 200, [("Content-Type", "text/html; charset=utf-8")], PAGE.encode("utf-8")


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oasis 控制台</title>
<style>
/* Fluent 2 tokens, mirroring what qfluentwidgets renders on the desktop side:
   same surface colours, same corner radii, same 28px control height. Light and
   dark both ship so the page follows the OS like the window does. */
:root{
  --bg:#f3f3f3; --layer:#ffffff; --layer-2:#fbfbfb; --stroke:#e5e5e5;
  --stroke-2:#d6d6d6; --text:#1a1a1a; --text-2:#616161; --text-3:#8a8a8a;
  --accent:#005fb8; --accent-hover:#196cbb; --accent-press:#004c93;
  --accent-fg:#ffffff; --subtle:#fdfdfd; --subtle-hover:#f5f5f5;
  --danger:#c42b1c; --danger-fg:#ffffff;
  --ok:#0f7b0f; --warn:#9d5d00;
  --shadow:0 1px 2px rgba(0,0,0,.06),0 2px 6px rgba(0,0,0,.04);
  --radius:4px; --radius-card:8px; --ctl:28px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#202020; --layer:#2b2b2b; --layer-2:#323232; --stroke:#3d3d3d;
    --stroke-2:#4a4a4a; --text:#ffffff; --text-2:#a0a0a0; --text-3:#7a7a7a;
    --accent:#60cdff; --accent-hover:#7ad6ff; --accent-press:#4db8e8;
    --accent-fg:#003a5c; --subtle:#2d2d2d; --subtle-hover:#383838;
    --danger:#ff99a4; --danger-fg:#3b0a0a; --ok:#6ccb5f; --warn:#fce100;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 2px 8px rgba(0,0,0,.24);
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.45 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,
       "PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}

/* ---------- shell: left rail + content, like FluentWindow ---------- */
.shell{display:flex;min-height:100vh}
.rail{width:190px;flex:none;background:var(--layer-2);border-right:1px solid var(--stroke);
  padding:8px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh}
.rail .brand{padding:12px 10px 14px;font-weight:600;font-size:15px;letter-spacing:.2px}
.rail .spacer{flex:1}
.rail-item{display:flex;align-items:center;gap:10px;height:36px;padding:0 10px;
  border:0;background:none;color:var(--text);border-radius:var(--radius);
  cursor:pointer;font:inherit;text-align:left;width:100%;position:relative}
.rail-item:hover{background:var(--subtle-hover)}
.rail-item.on{background:var(--subtle-hover);font-weight:600}
.rail-item.on::before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;
  border-radius:2px;background:var(--accent)}
.rail-item svg{width:18px;height:18px;flex:none;stroke:currentColor;fill:none;
  stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.rail .sep{height:1px;background:var(--stroke);margin:6px 10px}
.content{flex:1;min-width:0;padding:16px 20px 40px;max-width:1500px}

/* ---------- banner: same gradient as the desktop header ---------- */
.banner{height:72px;border-radius:var(--radius-card);display:flex;flex-direction:column;
  justify-content:center;padding:0 20px;margin-bottom:14px;
  background:linear-gradient(90deg,#b4531f 0%,#d9822b 45%,#f2c14e 100%)}
.banner b{color:#fff8f0;font-size:16px;font-weight:600}
.banner span{color:rgba(255,248,240,.9);font-size:12px}

/* ---------- stat cards: value on top (accented), caption below ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;
  margin-bottom:14px}
.stat{background:var(--layer);border:1px solid var(--stroke);border-radius:var(--radius-card);
  height:64px;padding:8px 14px;display:flex;flex-direction:column;justify-content:center;
  box-shadow:var(--shadow)}
.stat .v{font-size:22px;font-weight:600;line-height:1.15}
.stat .k{font-size:12px;color:var(--text-2);line-height:1.2}

/* ---------- header card, like HeaderCardWidget ---------- */
.card{background:var(--layer);border:1px solid var(--stroke);border-radius:var(--radius-card);
  margin-bottom:14px;box-shadow:var(--shadow);overflow:hidden}
.card > .hd{height:36px;display:flex;align-items:center;gap:10px;padding:0 14px;
  font-size:13px;font-weight:600;border-bottom:1px solid var(--stroke)}
.card > .bd{padding:12px 14px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row + .row{margin-top:8px}
.grow{flex:1}
.dim{color:var(--text-2);font-size:12px}
.hint{color:var(--text-2);font-size:12px;margin:8px 0 0}

/* ---------- controls: 28px, same as the desktop buttons ---------- */
.btn{height:var(--ctl);padding:0 14px;border-radius:var(--radius);font:inherit;font-size:13px;
  border:1px solid var(--stroke-2);background:var(--subtle);color:var(--text);cursor:pointer;
  display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn:hover{background:var(--subtle-hover)}
.btn:active{opacity:.85}
.btn.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.btn.pri:hover{background:var(--accent-hover)}
.btn.dan{background:var(--danger);border-color:var(--danger);color:var(--danger-fg)}
.btn:disabled{opacity:.5;cursor:default}
input,select{height:var(--ctl);padding:0 10px;border-radius:var(--radius);
  border:1px solid var(--stroke-2);background:var(--layer);color:var(--text);font:inherit;
  font-size:13px}
input[type=number]{width:78px}
input[type=checkbox]{height:auto;width:auto}
input:focus,select:focus,.btn:focus-visible{outline:2px solid var(--accent);
  outline-offset:1px;border-color:transparent}
textarea{width:100%;padding:9px 10px;border-radius:var(--radius);border:1px solid var(--stroke-2);
  background:var(--layer);color:var(--text);resize:vertical;
  font:12px/1.55 "Cascadia Mono",Consolas,"SF Mono",Menlo,monospace}
code{background:var(--layer-2);border:1px solid var(--stroke);border-radius:3px;padding:1px 4px;
  font-size:12px}

/* ---------- tables ---------- */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;font-weight:600;font-size:12px;color:var(--text-2);
  padding:6px 8px;border-bottom:1px solid var(--stroke);white-space:nowrap}
.tbl td{padding:6px 8px;border-bottom:1px solid var(--stroke);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:260px}
.tbl tbody tr:hover td{background:var(--layer-2)}
.scroll{overflow:auto}

/* ---------- status pills ---------- */
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;
  border:1px solid var(--stroke-2);color:var(--text-2)}
.tag.running{color:var(--warn);border-color:var(--warn)}
.tag.registered{color:var(--ok);border-color:var(--ok)}
.tag.submitted{color:#0f7b7b;border-color:#0f7b7b}
.tag.failed{color:var(--danger);border-color:var(--danger)}

/* ---------- log ---------- */
.log{font:12px/1.55 "Cascadia Mono",Consolas,"SF Mono",Menlo,monospace;
  background:var(--layer-2);border:1px solid var(--stroke);border-radius:var(--radius);
  padding:10px 12px;height:46vh;min-height:220px;overflow:auto;white-space:pre-wrap}
.log div{word-break:break-word}
.log .ok{color:var(--ok)} .log .error{color:var(--danger)}
.log .warn{color:var(--warn)} .log .info{color:var(--text)}

.icloud-row{display:flex;align-items:center;gap:10px;padding:5px 0;
  border-bottom:1px solid var(--stroke)}
.icloud-row:last-child{border-bottom:0}

/* ---------- login ---------- */
.login{max-width:320px;margin:16vh auto;background:var(--layer);border:1px solid var(--stroke);
  border-radius:var(--radius-card);padding:24px;text-align:center;box-shadow:var(--shadow)}
.login h2{margin:0 0 4px;font-size:16px}
.login input{width:100%;margin:14px 0 10px}
.err{color:var(--danger);font-size:12px;min-height:16px}

main{display:block}
section{display:none}
section.on{display:block}

@media(max-width:820px){
  .shell{flex-direction:column}
  .rail{width:auto;height:auto;position:static;flex-direction:row;overflow-x:auto;
    border-right:0;border-bottom:1px solid var(--stroke);padding:6px}
  .rail .brand,.rail .sep,.rail .spacer{display:none}
  .rail-item{width:auto;flex:none;height:34px}
  .rail-item.on::before{left:8px;right:8px;top:auto;bottom:0;width:auto;height:2px}
  .content{padding:12px}
}
</style></head><body>

<div id="login" class="login" style="display:none">
  <h2>Oasis 控制台</h2>
  <p class="dim" style="margin:0">需要密码才能访问</p>
  <input id="pw" type="password" placeholder="管理密码" autocomplete="current-password">
  <button class="btn pri" style="width:100%" onclick="doLogin()">进入</button>
  <div id="loginerr" class="err" style="margin-top:8px"></div>
</div>

<div id="app" style="display:none">
<div class="shell">
  <nav class="rail">
    <div class="brand">Oasis 控制台</div>
    <button class="rail-item on" data-t="dash">
      <svg viewBox="0 0 20 20"><path d="M3 9.5 10 3.5l7 6V16a1 1 0 0 1-1 1h-3.5v-4.5h-5V17H4a1 1 0 0 1-1-1z"/></svg>
      仪表盘</button>
    <button class="rail-item" data-t="mail">
      <svg viewBox="0 0 20 20"><rect x="2.5" y="4.5" width="15" height="11" rx="1.5"/><path d="m3 6 7 5 7-5"/></svg>
      邮箱池</button>
    <button class="rail-item" data-t="proxy">
      <svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="7"/><path d="M3 10h14"/><path d="M10 3c2.4 2.6 2.4 11.4 0 14M10 3c-2.4 2.6-2.4 11.4 0 14"/></svg>
      代理池</button>
    <button class="rail-item" data-t="reg">
      <svg viewBox="0 0 20 20"><path d="M5 3h6l4 4v10H5z"/><path d="M11 3v4h4"/><path d="M7.5 11h5M7.5 13.5h5"/></svg>
      预约记录</button>
    <button class="rail-item" data-t="data">
      <svg viewBox="0 0 20 20"><ellipse cx="10" cy="5" rx="6" ry="2.4"/><path d="M4 5v9.5c0 1.3 2.7 2.4 6 2.4s6-1.1 6-2.4V5"/><path d="M4 10c0 1.3 2.7 2.4 6 2.4s6-1.1 6-2.4"/></svg>
      数据库</button>
    <div class="spacer"></div>
    <div class="sep"></div>
    <button class="rail-item" data-t="set">
      <svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="2.6"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4"/></svg>
      设置</button>
    <div class="dim" style="padding:8px 10px 4px;line-height:1.6" id="runstate"></div>
    <button class="btn" style="margin:4px 6px 6px" onclick="doLogout()">退出登录</button>
  </nav>

  <main class="content">

    <section id="t-dash" class="on">
      <div class="banner">
        <b>Oasis Live '27&nbsp; 注册控制台</b>
        <span>浏览器真实 captcha · 官方表单驱动 · SQLite 去重落库</span>
      </div>
      <div class="stats" id="cards"></div>

      <div class="card"><div class="hd">运行控制</div><div class="bd">
        <div class="row">
          <label class="row" style="gap:6px">模式
            <select id="mode">
              <option value="browser">浏览器（Playwright 全流程）</option>
              <option value="hybrid">混合（共享浏览器取 captcha）</option>
            </select></label>
          <label class="row" style="gap:6px">线程
            <input id="threads" type="number" min="1" max="64"></label>
          <button class="btn pri" id="btnStart" onclick="startEngine()">开始注册</button>
          <button class="btn" onclick="stopEngine()">停止</button>
          <span class="grow"></span>
          <span class="dim" id="hosthint"></span>
        </div>
      </div></div>

      <div class="card"><div class="hd">实时日志
        <span class="grow"></span>
        <span class="dim" id="logcount"></span>
        <label class="dim row" style="gap:5px;font-weight:400">
          <input type="checkbox" id="autoscroll" checked> 自动滚动</label>
        <button class="btn" style="height:22px;padding:0 10px;font-size:12px"
                onclick="clearLogView()">清屏</button>
      </div><div class="bd">
        <div id="log" class="log"></div>
      </div></div>
    </section>

    <section id="t-mail">
      <div class="card"><div class="hd">iCloud 别名（勾选后导入）</div><div class="bd">
        <p class="hint" style="margin-top:0">从本地 iCloud 隐藏邮箱服务拉取别名。
          iCloud 建别名有配额（约每小时 10 个），所以没有全选 ——
          <b>只导入你真正要用的</b>。已导入的会置灰。</p>
        <div class="row">
          <button class="btn pri" onclick="loadIcloud()">拉取别名列表</button>
          <button class="btn" onclick="icloudAll(false)">全不选</button>
          <button class="btn" onclick="toggleUnused()">只选未导入的</button>
          <button class="btn" onclick="doIcloudImport()">导入勾选项</button>
          <span class="grow"></span>
          <span class="dim" id="icloudmsg"></span>
        </div>
        <div id="icloudlist" class="dim" style="margin-top:10px">还没有拉取。</div>
      </div></div>

      <div class="card"><div class="hd">手动导入账号</div><div class="bd">
        <p class="hint" style="margin-top:0">每行一个，空行与 # 开头的行会忽略。<br>
          <code>outlook@x.com----密码----client_id----refresh_token</code><br>
          <code>gmail@x.com----密码----client_id----client_secret----refresh_token</code><br>
          <code>别名@icloud.com----acc_xxxxxxxx----hme</code></p>
        <textarea id="importbox" rows="6" placeholder="粘贴凭据行…"></textarea>
        <div class="row">
          <label class="row" style="gap:6px">协议
            <select id="importproto">
              <option value="auto">自动</option>
              <option value="graph">Microsoft Graph</option>
              <option value="imap">IMAP</option>
              <option value="hme">iCloud HME</option>
            </select></label>
          <button class="btn pri" onclick="doImport()">导入到数据库</button>
          <span class="grow"></span>
          <span class="dim" id="importmsg"></span>
        </div>
      </div></div>

      <div class="card"><div class="hd">账号池
        <span class="grow"></span>
        <span class="dim" id="mailcount"></span>
      </div><div class="bd">
        <div class="row" style="margin-bottom:8px">
          <select id="fstatus" onchange="loadAccounts(1)">
            <option value="">全部状态</option>
            <option value="pending">待注册</option>
            <option value="running">进行中</option>
            <option value="registered">邮件确认</option>
            <option value="submitted">页面确认</option>
            <option value="failed">失败</option>
          </select>
          <button class="btn" onclick="resetFailed()">重置失败与卡住项</button>
          <button class="btn" onclick="exportFile('creds')">导出全部凭据</button>
        </div>
        <div class="scroll"><table class="tbl" id="mtable"><thead><tr>
          <th>ID</th><th>邮箱</th><th>协议</th><th>状态</th><th>姓名</th><th>电话</th><th>备注</th>
        </tr></thead><tbody></tbody></table></div>
        <div class="row" style="margin-top:10px">
          <button class="btn" onclick="loadAccounts(curPage-1)" id="prev">上一页</button>
          <span class="dim" id="pageno"></span>
          <button class="btn" onclick="loadAccounts(curPage+1)" id="next">下一页</button>
        </div>
      </div></div>
    </section>

    <section id="t-proxy">
      <div class="card"><div class="hd">代理池配置</div><div class="bd">
        <p class="hint" style="margin-top:0">每行一个：
          <code>http://user:pass@host:port</code> ·
          <code>socks5://user:pass@host:port</code> ·
          <code>host:port</code>。链路写法 <code>前置代理|上游</code>。
          保存后立即生效并写入配置文件，重启不会丢。</p>
        <textarea id="proxybox" rows="10" placeholder="每行一个代理…"></textarea>
        <div class="row">
          <button class="btn pri" onclick="saveProxies()">保存并生效</button>
          <span class="grow"></span>
          <span class="dim" id="proxymsg"></span>
        </div>
        <p class="hint">保存时会用输入框里的内容<b>整体替换</b>。凭据加载时是遮罩显示，
          不动就原样保存 —— 要改动请整行重写。</p>
      </div></div>

      <div class="card"><div class="hd">代理健康度
        <span class="grow"></span><span class="dim" id="proxysummary"></span>
      </div><div class="bd">
        <div class="scroll"><table class="tbl" id="ptable"><thead><tr>
          <th>代理</th><th>成功</th><th>失败</th><th>冷却中</th><th>最后错误</th>
        </tr></thead><tbody></tbody></table></div>
      </div></div>
    </section>

    <section id="t-reg">
      <div class="card"><div class="hd">预约记录</div><div class="bd">
        <div class="scroll"><table class="tbl" id="rtable"><thead><tr>
          <th>账号</th><th>场次（按偏好顺序）</th><th>模式</th><th>状态</th><th>时间</th>
        </tr></thead><tbody></tbody></table></div>
      </div></div>
    </section>

    <section id="t-data">
      <div class="card"><div class="hd">数据库维护</div><div class="bd">
        <p class="hint" style="margin-top:0">清理不可撤销；账号被删除时，其预约记录会
          一并删除（外键级联）。</p>
        <div class="row">
          <button class="btn" onclick="vacuum()">VACUUM 压缩</button>
          <button class="btn" onclick="exportFile('accounts')">导出账号 JSON</button>
          <span class="grow"></span>
          <select id="delstatus">
            <option value="failed">失败</option>
            <option value="submitted">页面确认</option>
            <option value="registered">邮件确认</option>
            <option value="pending">待注册</option>
          </select>
          <button class="btn dan" onclick="delBucket()">清理该状态</button>
        </div>
      </div></div>
    </section>

    <section id="t-set">
      <div class="card"><div class="hd">设置</div><div class="bd">
        <div id="setform"></div>
        <p class="hint">改动会写回服务端配置文件；代理池与账号在各自页面管理。</p>
      </div></div>
    </section>

  </main>
</div>
</div>

<script>
const $ = s => document.querySelector(s);
let curPage = 1, logSeq = 0, logLines = 0;

const LABEL = {total:'账号总数', pending:'待注册', running:'进行中',
               registered:'邮件确认注册', submitted:'页面确认提交', failed:'失败',
               registrations:'预约记录'};
// same accents the desktop StatCards use
const ACCENT = {total:'#3b7dd8', pending:'#8a8f98', running:'#c8871a',
                registered:'#1f9d55', submitted:'#3fa88a', failed:'#d64545',
                registrations:'#7a6ad8'};

async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  if (r.status === 401){ showLogin(); throw new Error('AUTH_REQUIRED'); }
  return r.json();
}
function showLogin(){ $('#login').style.display='block'; $('#app').style.display='none'; }
function showApp(){ $('#login').style.display='none'; $('#app').style.display='block'; }

async function doLogin(){
  const pw = $('#pw').value;
  const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'},
                                   body: JSON.stringify({password: pw})});
  if (r.ok){ location.reload(); return; }
  const j = await r.json().catch(()=>({}));
  $('#loginerr').textContent = j.error === 'RATE_LIMITED'
    ? '尝试次数过多，请稍后再试' : '密码错误';
  $('#pw').select();
}
async function doLogout(){ await fetch('/logout', {method:'POST'}); location.reload(); }

document.querySelectorAll('.rail-item').forEach(b => b.onclick = () => {
  document.querySelectorAll('.rail-item').forEach(x => x.classList.remove('on'));
  document.querySelectorAll('section').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  $('#t-' + b.dataset.t).classList.add('on');
  if (b.dataset.t === 'mail') loadAccounts(1);
  if (b.dataset.t === 'proxy') loadProxies();
  if (b.dataset.t === 'reg') loadRegs();
  if (b.dataset.t === 'set') loadSettings();
});

function esc(s){ return String(s??'').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function mask(u){ return String(u||'').replace(/\/\/[^@/]+@/, '//***:***@'); }

async function tick(){
  let s;
  try { s = await api('/api/state'); } catch(e){ return; }
  const st = s.stats || {};
  $('#cards').innerHTML = Object.keys(LABEL).map(k =>
    `<div class="stat"><div class="v" style="color:${ACCENT[k]}">${st[k] ?? 0}</div>
      <div class="k">${LABEL[k]}</div></div>`).join('');
  $('#runstate').innerHTML = (s.running ? '● 运行中' : '○ 空闲')
    + '<br>' + s.state + ' · 线程 ' + (s.threads || '-');
  $('#btnStart').disabled = !!s.running;
  if (s.config && !$('#mode').dataset.init){
    $('#mode').value = s.config.mode || 'browser';
    $('#threads').value = s.config.threads || 1;
    $('#mode').dataset.init = 1;
  }
  if (!$('#threads').value && s.host){ $('#threads').value = s.host.recommended || 1; }
  if (s.host){
    const h = s.host;
    $('#hosthint').textContent = h.total_mb
      ? `内存 ${h.total_mb - h.available_mb}/${h.total_mb} MB · ${h.cores} 核 · 建议 ${h.recommended} 线程`
      : `建议 ${h.recommended} 线程`;
  }
}

async function startEngine(){
  await api('/api/start', {method:'POST', body: JSON.stringify({
    threads: parseInt($('#threads').value||'1'), mode: $('#mode').value })});
  tick();
}
async function stopEngine(){ await api('/api/stop', {method:'POST'}); tick(); }

async function loadAccounts(page){
  if (page < 1) return;
  curPage = page;
  const st = $('#fstatus').value;
  const d = await api('/api/accounts?page=' + page + '&status=' + encodeURIComponent(st));
  const tb = $('#mtable tbody');
  tb.innerHTML = d.rows.length ? d.rows.map(r => `<tr>
     <td>${r.id}</td><td>${esc(r.email)}</td><td>${esc(r.protocol||'')}</td>
     <td><span class="tag ${esc(r.status||'')}">${LABEL[r.status]||esc(r.status||'')}</span></td>
     <td>${esc((r.first_name||'')+' '+(r.last_name||''))}</td>
     <td>${esc(r.phone||'')}</td>
     <td class="dim">${esc((r.error||'').slice(0,60))}</td></tr>`).join('')
     : '<tr><td colspan="7" class="dim">没有数据</td></tr>';
  $('#mailcount').textContent = `共 ${d.total} 条`;
  $('#pageno').textContent = `第 ${d.page} / ${Math.max(1, Math.ceil(d.total/d.per))} 页`;
  $('#prev').disabled = d.page <= 1;
  $('#next').disabled = d.page * d.per >= d.total;
}

async function loadProxies(){
  const d = await api('/api/proxies');
  $('#proxysummary').textContent = `共 ${d.total} 个`;
  $('#ptable tbody').innerHTML = d.rows.length ? d.rows.map(r => `<tr>
    <td>${esc(mask(r.url))}</td><td>${r.ok ?? 0}</td><td>${r.fail ?? 0}</td>
    <td>${r.cooling ? '<span class="tag running">是</span>' : ''}</td>
    <td class="dim">${esc((r.last_error||'').slice(0,60))}</td></tr>`).join('')
    : '<tr><td colspan="5" class="dim">没有数据</td></tr>';
  if (!$('#proxybox').dataset.dirty) $('#proxybox').value = (d.lines || []).join('\n');
}
async function saveProxies(){
  const lines = $('#proxybox').value;
  const n = lines.split('\n').filter(x => x.trim() && !x.trim().startsWith('#')).length;
  if (!n){ $('#proxymsg').textContent = '至少要有一行'; return; }
  if (!confirm(`将用 ${n} 行整体替换当前代理池，继续？`)) return;
  const d = await api('/api/proxies', {method:'POST', body: JSON.stringify({lines})});
  $('#proxymsg').textContent = `已保存 ${d.total} 行，立即生效`;
  $('#proxybox').dataset.dirty = '';
  loadProxies(); tick();
}

async function loadRegs(){
  const d = await api('/api/registrations');
  $('#rtable tbody').innerHTML = d.rows.length ? d.rows.map(r => `<tr>
    <td>${esc(r.email || '')}</td><td>${esc(r.shows || '—')}</td>
    <td>${esc(r.mode || '')}</td>
    <td><span class="tag ${esc(r.status||'')}">${LABEL[r.status] || esc(r.status||'')}</span></td>
    <td class="dim">${esc(r.created_at || '')}</td></tr>`).join('')
    : '<tr><td colspan="5" class="dim">还没有记录</td></tr>';
}

function icloudBoxes(){ return [...document.querySelectorAll('#icloudlist input[type=checkbox]')]; }
function icloudAll(v){ icloudBoxes().forEach(b => { if (!b.disabled) b.checked = v; }); }
function toggleUnused(){
  icloudBoxes().forEach(b => { b.checked = !b.disabled && b.dataset.imported !== '1'; });
}
async function loadIcloud(){
  $('#icloudmsg').textContent = '拉取中…';
  $('#icloudlist').innerHTML = '';
  let d;
  try { d = await api('/api/icloud/aliases'); }
  catch(e){ $('#icloudmsg').textContent = '请求失败'; return; }
  if (d.error){ $('#icloudmsg').textContent = d.error; return; }
  if (!d.total){ $('#icloudmsg').textContent = '服务上没有别名'; return; }
  $('#icloudmsg').textContent =
    `共 ${d.total} 个，已导入 ${d.aliases.filter(a => a.imported).length} 个`;
  $('#icloudlist').innerHTML = d.aliases.map(a => `
    <label class="icloud-row">
      <input type="checkbox" data-email="${esc(a.email)}" data-imported="${a.imported?1:0}"
             ${a.imported ? 'disabled' : ''}>
      <span style="min-width:230px">${esc(a.email)}</span>
      <span class="tag ${a.active ? 'registered' : 'failed'}">${a.active ? '启用' : '停用'}</span>
      <span class="dim">${esc(a.account_name||'')}</span>
      <span class="dim">${esc(a.label||'')}</span>
      ${a.imported ? '<span class="dim">（已在池中）</span>' : ''}
    </label>`).join('');
}
async function doIcloudImport(){
  const emails = icloudBoxes().filter(b => b.checked && !b.disabled).map(b => b.dataset.email);
  if (!emails.length){ $('#icloudmsg').textContent = '先勾选要导入的别名'; return; }
  const d = await api('/api/icloud/import', {method:'POST', body: JSON.stringify({emails})});
  if (d.error){ $('#icloudmsg').textContent = d.error; return; }
  $('#icloudmsg').textContent = `导入 ${d.added} 个，已在池中 ${d.duplicate} 个`;
  loadIcloud(); loadAccounts(1); tick();
}

async function doImport(){
  const lines = $('#importbox').value;
  if (!lines.trim()){ $('#importmsg').textContent = '先粘贴内容'; return; }
  const d = await api('/api/accounts/import', {method:'POST', body: JSON.stringify({
    lines, protocol: $('#importproto').value })});
  if (d.error){ $('#importmsg').textContent = d.error; return; }
  $('#importmsg').textContent = `新增 ${d.added} 条，重复 ${d.duplicate} 条`;
  $('#importbox').value = '';
  loadAccounts(1); tick();
}

function clearLogView(){ $('#log').textContent = ''; logLines = 0; $('#logcount').textContent = ''; }
async function pollLog(){
  let d;
  try { d = await api('/api/log?since=' + logSeq); } catch(e){ return; }
  if (d.seq === undefined) return;
  logSeq = d.seq;
  const box = $('#log');
  for (const r of d.rows){
    const t = new Date(r.at * 1000).toTimeString().slice(0,8);
    const el = document.createElement('div');
    el.className = r.level;
    el.textContent = `[${t}] ${r.msg}`;
    box.appendChild(el);
    logLines++;
  }
  if (d.rows.length){
    while (box.childElementCount > 1500) box.removeChild(box.firstChild);
    if ($('#autoscroll').checked) box.scrollTop = box.scrollHeight;
    $('#logcount').textContent = `${logLines} 行 · 共 ${d.seq} 条`;
  }
}

const SET_KEYS = [['mode','模式'],['threads','线程数'],['shows','场次偏好（逗号分隔）'],
  ['link_timeout','等邮件超时(秒)'],['think_time','提交前停顿(秒)'],
  ['verify_success','成功后校验邮件(true/false)'],['send_captcha','发送 captcha(true/false)'],
  ['front_proxy','前置代理(链路)'],['google_proxy','Google 分流代理'],
  ['mail_proxy','取件代理'],['hme_base','iCloud 服务地址'],['hme_password','iCloud 密码'],
  ['db_path','数据库路径']];
async function loadSettings(){
  const s = await api('/api/state');
  const c = s.config || {};
  $('#setform').innerHTML = SET_KEYS.map(([k,label]) => {
    const v = Array.isArray(c[k]) ? c[k].join(',') : (c[k] ?? '');
    return `<div class="row" style="margin-top:6px">
      <label class="dim" style="width:180px;flex:none">${label}</label>
      <input data-k="${k}" value="${esc(v)}" style="flex:1;min-width:200px"></div>`;
  }).join('') + `<div class="row" style="margin-top:12px">
      <button class="btn pri" onclick="saveSettings()">保存设置</button></div>`;
}
async function saveSettings(){
  const body = {};
  document.querySelectorAll('#setform input[data-k]').forEach(i => {
    let v = i.value;
    if (i.dataset.k === 'threads') v = parseInt(v || '1');
    if (v === 'true') v = true; else if (v === 'false') v = false;
    if (i.dataset.k === 'shows') v = v.split(',').map(x => x.trim()).filter(Boolean);
    body[i.dataset.k] = v;
  });
  await api('/api/config', {method:'POST', body: JSON.stringify(body)});
  alert('已保存');
}

async function resetFailed(){
  const d = await api('/api/accounts/reset', {method:'POST'});
  alert('重置 ' + d.changed + ' 条'); loadAccounts(curPage); tick();
}
async function vacuum(){ await api('/api/vacuum', {method:'POST'}); alert('已压缩'); }
async function delBucket(){
  const s = $('#delstatus').value;
  if (!confirm('将永久删除所有「' + (LABEL[s]||s) + '」账号，不可撤销。继续？')) return;
  const d = await api('/api/accounts/delete', {method:'POST', body: JSON.stringify({status:s})});
  alert('已删除 ' + d.deleted + ' 条'); loadAccounts(1); tick();
}
function exportFile(what){ location.href = '/api/export?what=' + what; }

(async function boot(){
  try {
    const r = await fetch('/api/state');
    if (r.status === 401){ showLogin(); $('#pw').focus(); return; }
    showApp(); tick(); setInterval(tick, 3000);
    pollLog(); setInterval(pollLog, 2000);
  } catch(e){ showLogin(); }
})();
$('#pw')?.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
document.addEventListener('input', e => {
  if (e.target && e.target.id === 'proxybox') e.target.dataset.dirty = '1';
});
</script></body></html>
"""
