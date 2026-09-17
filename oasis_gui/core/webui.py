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
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from core import mailbox, registrar
from core.browser_registrar import _build_stamp

from http.server import BaseHTTPRequestHandler

SESSION_COOKIE = "oasis_session"

# Shown when the React bundle is absent - the usual case when running from a
# source checkout without a build. Saying so beats an empty white page.
_MISSING = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Oasis 控制台</title><style>
body{font:14px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;
 margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;
 background:#f5f5f5;color:#262626}
.b{max-width:640px;background:#fff;border:1px solid #f0f0f0;border-radius:8px;
 padding:28px 32px}
h1{font-size:17px;margin:0 0 12px}
code{background:#fafafa;border:1px solid #f0f0f0;border-radius:4px;padding:1px 5px}
pre{background:#fafafa;border:1px solid #f0f0f0;border-radius:6px;padding:12px;
 overflow:auto;font-size:12px}
</style></head><body><div class="b">
<h1>前端还没有构建</h1>
<p>管理界面是 React + Ant Design，需要先构建一次。</p>
<pre>cd oasis_gui/webui
npm install
npm run build</pre>
<p>或者直接用生产镜像（CI 已经构建并发布）：</p>
<pre>docker compose pull &amp;&amp; docker compose up -d</pre>
<p>只有 API 是就绪的：<code>/api/state</code>、<code>/health</code>。</p>
</div></body></html>"""
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
                 password="", static_dir="", log=None):
        self.store = store
        self.config = config
        self.status = status
        self.proxies = proxies
        self.logs = logs
        self.controller = controller
        self.password = password or ""
        # Where the built React bundle lives. Empty means the frontend has not
        # been built, and _page() explains that instead of serving a blank page.
        self.static_dir = static_dir or ""
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
        # Built assets come first, before the auth gate. They are inert JS/CSS,
        # and the login page cannot render without them - gating them means the
        # browser gets index.html in place of the bundle and shows a blank page.
        hit = self._static(path)
        if hit:
            return hit

        # Liveness, and the one endpoint that answers "which image is this?".
        # Unauthenticated on purpose: the container healthcheck polls it before
        # any password exists in the request, and it carries nothing but the
        # build stamp. It used to fall through to the login page, so the
        # healthcheck was parsing HTML as JSON and failing forever.
        if path == "/health":
            return self._json(200, {"ok": True, "build": _build_stamp()})

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
            try:
                lines = self._expand_aliases(lines)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
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

        # Unknown non-API path: hand back the app shell so client-side routing
        # works, rather than showing the user a JSON 404 for a typo.
        if not path.startswith("/api/"):
            return self._page()
        return self._json(404, {"error": "not found"})

    def _expand_aliases(self, lines):
        """Complete bare iCloud alias addresses into full credential lines.

        Pasting the alias list is how this pool actually gets built, and the
        inbox address and app password are identical on every line: making the
        operator repeat them is noise, and one typo in one of them is a mailbox
        that silently never receives anything. So a line that is nothing but an
        iCloud address is filled in from the configured inbox.

        Raises ValueError with something the operator can act on when no inbox
        has been configured yet, rather than importing rows that cannot be read.
        """
        inbox = (self.config.get("alias_inbox") or "").strip()
        secret = (self.config.get("alias_inbox_password") or "").strip()
        out = []
        for raw in lines:
            line = raw.strip()
            if line and "----" not in line and "@" in line:
                domain = line.rsplit("@", 1)[-1].lower()
                if domain in mailbox.HME_ICLOUD_DOMAINS:
                    if not (inbox and secret):
                        raise ValueError(
                            f"{line} 是 iCloud 别名，要先在「设置」里填收件的 "
                            f"Gmail 地址和应用专用密码；或者用完整格式 "
                            f"别名----gmail地址----应用专用密码----gmail-imap")
                    out.append(f"{line}----{inbox}----{secret}----gmail-imap")
                    continue
            out.append(raw)
        return out

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

    # ------------------------------------------------------------------ static
    def _page(self):
        """The built React app's index.html, or a page saying how to build it."""
        index = os.path.join(self.static_dir, "index.html")
        try:
            with open(index, "rb") as fh:
                return 200, [("Content-Type", "text/html; charset=utf-8")], fh.read()
        except OSError:
            return 200, [("Content-Type", "text/html; charset=utf-8")], \
                _MISSING.encode("utf-8")

    def _static(self, path):
        """A file from the built bundle. Returns None when it is not there.

        Path traversal is rejected rather than sanitised: this serves whatever
        sits next to index.html, and a `..` in the URL would otherwise reach the
        account database one directory up.
        """
        rel = urllib.parse.unquote(path).lstrip("/")
        if not rel or "\x00" in rel or ".." in rel.split("/"):
            return None
        full = os.path.realpath(os.path.join(self.static_dir, rel))
        root = os.path.realpath(self.static_dir)
        if not full.startswith(root + os.sep):
            return None
        if not os.path.isfile(full):
            return None
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json",
                                                  "image/svg+xml"):
            ctype += "; charset=utf-8"
        try:
            with open(full, "rb") as fh:
                return 200, [("Content-Type", ctype)], fh.read()
        except OSError:
            return None


