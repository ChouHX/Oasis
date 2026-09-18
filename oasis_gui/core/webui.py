#!/usr/bin/env python3
"""Web admin for the headless hit monitor.

Mirrors the pages of the desktop console, over plain `http.server` - no extra
dependency, so the image does not grow and nothing has to be installed on the
box.

Access control is not optional: this can start/stop the sweep and read the
account pool, so without a password the server refuses to start rather than
serving that to whoever reaches the port. A session cookie carries the login;
the password is compared in constant time and logins are rate limited.
"""
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie

from core import hitcheck, mailbox, sysinfo

SESSION_COOKIE = "oasis_session"
# /api/start 里按原值落盘的键（其余按整数转）。
_BOOL_KEYS = ("skip_hits", "mail_filter")

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
    """上一版程序把场次写在 registrations.poll_answer_ids 里，这里是它的读法。

    注册已经移除，这个函数只为了让老库的预约记录在「数据库」页里仍显示成人能
    看懂的名字，而不是一串 uuid。
    """
    if raw in (None, "", "[]"):
        return ""
    try:
        ids = json.loads(raw) if isinstance(raw, str) else list(raw)
    except Exception:
        return str(raw)[:60]
    return " > ".join(str(i)[:8] for i in ids)


def split_lines(raw):
    """凭据行：换行或逗号分隔，`#` 开头的丢掉。列表也接受。

    列表**不能**再 `str()` 一遍：`str(["a@x.com", "b@x.com"])` 是
    `"['a@x.com', 'b@x.com']"`，按逗号切出来的是带引号和方括号的碎片，然后它们
    会被当邮箱存进库 —— 接口收到一个数组就静默写出 `['a@x.com` 这种地址。踩过
    一次，所以这里显式分两种情况。
    """
    if isinstance(raw, (list, tuple, set)):
        chunks = []
        for item in raw:
            chunks += str(item).replace(",", "\n").splitlines()
    else:
        chunks = str(raw or "").replace(",", "\n").splitlines()
    out = []
    for chunk in chunks:
        line = chunk.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# 凭据类配置键：它们只报「有没有设置」，内容不出网关。
#
# 这件事以前是错的：/api/state 每 3 秒被页面轮询一次，而它把整个配置原样发回
# 浏览器 —— 包括 hme_password、别名收件箱的应用专用密码、以及带 user:pass 的
# 取件代理。密码于是每 3 秒在网线上跑一遍，还留在每个打开过这个页面的浏览器
# 内存与 DevTools 历史里。脱敏之后页面照样能显示「已设置 / 未设置」，而
# 「不改就留空」的语义由 _keep_secret 在保存那一侧兜住。
SECRET_KEYS = ("hme_password", "alias_inbox_password")


def _mask_url(url):
    """把代理 URL 里的 user:pass 换成 ***:***，其余照旧。"""
    raw = str(url or "")
    if not raw:
        return ""
    try:
        u = urllib.parse.urlsplit(raw)
        if u.username:
            host = u.hostname or ""
            port = f":{u.port}" if u.port else ""
            return urllib.parse.urlunsplit(
                (u.scheme, f"***:***@{host}{port}", u.path, u.query, u.fragment))
    except Exception:
        pass
    return raw


def _stamp(value):
    """epoch -> 'YYYY-MM-DD HH:MM'（本地时区），空值给空串。

    服务端做这件事而不是页面：页面上显示的时间要是人所在时区的时间，而
    epoch 在浏览器里被 new Date() 一过就成了 UTC 的兄弟时区。
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError):
        return ""


def _csv_cell(value):
    text = "" if value is None else str(value)
    if any(c in text for c in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


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
    """Routes for the admin UI. `controller` owns the monitor lifecycle."""

    def __init__(self, store, config, status, logs, controller,
                 password="", static_dir="", log=None):
        self.store = store
        self.config = config
        self.status = status
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
            return self._json(200, {"ok": True, "build": sysinfo.build_stamp(),
                                    "hits": self.store.stats().get("hits", 0)})

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
            info["config"] = self._public_config()
            # 界面顶栏把它显示出来。这一条是为了回答一个反复出现的问题：
            # 「我看的是哪一版？」—— 部署里 pull 没生效、容器没重建、浏览器拿
            # 缓存，三种情况页面长得一模一样，而它们的修法各不相同。有了这行，
            # 页面自己就说了。
            info["running"] = self.controller.running()
            info["build"] = sysinfo.build_stamp()
            # 截止时间的可读形态：界面上要显示「从什么时候起才算结果」。
            info["cutoff"] = hitcheck.format_cutoff(hitcheck.parse_cutoff(
                self.config.get("ballot_cutoff") or hitcheck.DEFAULT_CUTOFF))
            info["version"] = 2
            return self._json(200, info)

        if path == "/api/hits":
            rows = self.store.hits()
            for r in rows:
                r["hit_at"] = _stamp(r.get("hit_at"))
                r["last_mail_at"] = _stamp(r.get("last_mail_at"))
                r["label"] = hitcheck.SOURCE_LABEL.get(r.get("hit_source"),
                                                       r.get("hit_source") or "")
            return self._json(200, {"total": len(rows), "rows": rows})

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
            # 勾选导入的别名同样是「要查的」，一并标记。
            added, dup = self.store.add_mailboxes(lines, "hme", opted_in=True)
            self.log("info", f"web: imported {added} iCloud alias(es), "
                             f"{dup} already present")
            return self._json(200, {"added": added, "duplicate": dup,
                                    "stats": self.store.stats()})

        if path == "/api/accounts":
            status = query.get("status", [""])[0]
            page = max(1, int(query.get("page", ["1"])[0] or 1))
            per = 50
            rows = self.store.accounts()
            for r in rows:
                r["checked_at"] = _stamp(r.get("checked_at"))
                r["hit_at"] = _stamp(r.get("hit_at"))
                r["last_mail_at"] = _stamp(r.get("last_mail_at"))
                r["label"] = hitcheck.SOURCE_LABEL.get(r.get("hit_source"),
                                                       r.get("hit_source") or "")
            if status == "hit":
                rows = [r for r in rows if r.get("hit_at")]
            elif status == "opted":
                rows = [r for r in rows if r.get("opted_in")]
            elif status == "unmarked":
                rows = [r for r in rows if not r.get("opted_in")]
            elif status == "waiting":
                # 「未中签」= 在检测范围里、但还没有中签结论的。
                rows = [r for r in rows if r.get("opted_in") and not r.get("hit_at")]
            elif status == "error":
                rows = [r for r in rows if r.get("check_error")]
            elif status:
                rows = [r for r in rows if r["status"] == status]
            total = len(rows)
            return self._json(200, {
                "total": total, "page": page, "per": per,
                "rows": rows[(page - 1) * per: page * per]})

        if path == "/api/registrations":
            # 上一版程序留下的预约记录，只读展示：账号能不能读、中没中签，
            # 都不再依赖这张表，但它记录了当时提交了什么。
            rows = self.store.registrations()
            for r in rows:
                r["shows"] = _shows_label(r.get("poll_answer_ids"))
            return self._json(200, {"rows": rows})

        if path == "/api/accounts/import" and method == "POST":
            lines = split_lines((body or {}).get("lines") or "")
            if not lines:
                return self._json(400, {"error": "没有可导入的行"})
            try:
                lines = self._expand_aliases(lines)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            protocol = (body or {}).get("protocol") or "auto"
            # 默认标记为已预约：粘一批地址进来，图的本来就是「查这些」。
            opted = bool((body or {}).get("opted", True))
            added, dup = self.store.add_mailboxes(lines, protocol, opted_in=opted)
            self.log("info", f"web: imported {added} account(s), {dup} duplicate"
                             + ("（已标记为已预约）" if opted else ""))
            return self._json(200, {"added": added, "duplicate": dup,
                                    "stats": self.store.stats()})

        if path == "/api/log":
            seq = int(query.get("since", ["0"])[0] or 0)
            rows = self.logs.since(seq)
            return self._json(200, {"seq": self.logs.seq,
                                    "rows": [{"n": n, "at": at, "level": lv,
                                              "msg": m} for n, at, lv, m in rows]})

        if path == "/api/start" and method == "POST":
            # 「立即巡检一轮」。间隔与并发都在这儿落地，所以改完就能生效，
            # 不必等下一轮或重启。
            body = body or {}
            for key in ("interval", "threads", "per_page", "skip_hits", "limit",
                        "mail_filter", "only_opted", "ballot_cutoff"):
                if body.get(key) in (None, ""):
                    continue
                if key in _BOOL_KEYS:
                    self.config.data[key] = body[key]
                elif key == "ballot_cutoff":
                    # 时间文本，原样存；解析失败由 monitor 退回默认值。
                    self.config.data[key] = str(body[key]).strip()
                else:
                    self.config.data[key] = int(body[key])
            self.config.save()
            if self.controller.running():
                self.controller.stop()
            self.controller.start()
            return self._json(200, {"ok": True,
                                    "config": dict(self.config.data)})

        if path == "/api/stop" and method == "POST":
            self.controller.stop()
            return self._json(200, {"ok": True})

        if path == "/api/config" and method == "POST":
            for key, value in (body or {}).items():
                if key not in self.config.data:
                    continue
                kept = self._keep_secret(key, value)
                if kept is not None:
                    self.config.data[key] = kept
            self.config.save()
            return self._json(200, {"ok": True,
                                    "config": self._public_config()})

        if path == "/api/accounts/opt" and method == "POST":
            # 把地址纳进/移出检测范围。两种用法：给一批 id（表格勾选），或者
            # 给一个 scope（「全部未标记的」—— 一整份名单一次标完才是常态）。
            body = body or {}
            on = bool(body.get("on", True))
            scope = (body.get("scope") or "").strip()
            if scope == "unmarked":
                n = self.store.mark_unmarked(on=on, source="manual")
            elif scope == "all":
                ids = [r["id"] for r in self.store.accounts()]
                n = self.store.mark_opted(ids, on=on, source="manual")
            else:
                ids = body.get("ids") or []
                if not ids:
                    return self._json(400, {"error": "没有指定账号"})
                n = self.store.mark_opted(ids, on=on, source="manual")
            self.log("info", f"web: {'标记' if on else '取消标记'} {n} 个账号为已预约")
            return self._json(200, {"changed": n, "stats": self.store.stats()})

        if path == "/api/accounts/reset" and method == "POST":
            # 重新排队 = 清掉检测记录重来（不触碰已成立的中签）。
            return self._json(200, {"changed": self.store.reset_checks()})

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
            elif what == "hits" or what == "hit_creds":
                # 中签名单要能被直接拿去用：一份给人看的 CSV，一份能再导入回来的
                # 凭据行，后者只含中签的地址。
                if what == "hits":
                    data = self._hits_csv()
                else:
                    data = "\n".join(self._hit_cred_lines()) + "\n"
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

    def _public_config(self):
        """配置的可外发形态：凭据只报有没有，代理只报主机。"""
        cfg = dict(self.config.data)
        for key in SECRET_KEYS:
            value = cfg.get(key) or ""
            cfg[key] = ""
            cfg[f"{key}_set"] = bool(value)
        if cfg.get("mail_proxy"):
            cfg["mail_proxy"] = _mask_url(cfg["mail_proxy"])
        return cfg

    def _keep_secret(self, key, value):
        """密码与遮罩代理的「留空 / 原样回传」都读作「不改」。

        页面拿到的是脱敏值，所以它回传的 mail_proxy 可能是 `socks5://***:***@host`
        —— 照着写回去就把密码换成了三个星号。密码字段同理：留空表示不改，而
        「清空」由显式的一个空格之外的方式表达不了，所以这里一律保守处理。

        返回 None 表示「不改」，否则返回要写入的值。
        """
        text = "" if value is None else str(value)
        if key in SECRET_KEYS:
            return text if text.strip() else None
        if key == "mail_proxy":
            if "***:***@" in text:
                return None
            return text
        return value

    def _hits_csv(self):
        """中签名单，人读的 CSV。来源单独一列，因为「站点已确认、邮件没来」
        与「收到成功邮件」不是同一件事，事后要能分得清。"""
        rows = [["email", "中签时间", "证据来源", "说明", "最近来信", "来信标题",
                 "协议", "检查次数"]]
        for h in self.store.hits():
            note = h.get("hit_note") or ""
            rows.append([
                h.get("email", ""), _stamp(h.get("hit_at")),
                hitcheck.SOURCE_LABEL.get(h.get("hit_source"),
                                          h.get("hit_source") or ""),
                note, _stamp(h.get("last_mail_at")),
                h.get("last_mail_subject") or "", h.get("protocol") or "",
                h.get("check_count") or 0])
        return "\n".join(",".join(_csv_cell(c) for c in row)
                         for row in rows) + "\n"

    def _hit_cred_lines(self):
        """中签账号的凭据行，格式与导入一致，能再导回来。"""
        out = []
        for h in self.store.hits():
            line = self.store.cred_line_for(h["id"])
            if line:
                out.append(line)
        return out

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


