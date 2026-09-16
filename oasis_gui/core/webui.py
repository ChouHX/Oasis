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
:root{
  --bg:#0f1216; --panel:#171b21; --line:#252b33; --fg:#e6e9ee; --dim:#8b93a1;
  --ok:#3fa88a; --warn:#c8871a; --bad:#d64545; --accent:#4a8fe0;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
     background:radial-gradient(1200px 600px at 20% -10%,#1b2430,transparent),var(--bg);color:var(--fg)}
a{color:var(--accent)}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line);
       background:rgba(15,18,22,.85);backdrop-filter:blur(8px);position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav button{background:none;border:1px solid transparent;color:var(--dim);padding:6px 12px;border-radius:8px;
           cursor:pointer;font-size:13px}
nav button.on{color:var(--fg);background:var(--panel);border-color:var(--line)}
main{padding:20px;max-width:1400px;margin:0 auto}
section{display:none}section.on{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .n{font-size:24px;font-weight:600;margin-top:2px}
.card .l{font-size:12px;color:var(--dim)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.panel h2{font-size:13px;margin:0 0 12px;color:var(--dim);font-weight:600;letter-spacing:.4px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap;
      overflow:hidden;text-overflow:ellipsis;max-width:280px}
th{color:var(--dim);font-weight:500;font-size:12px}
tr:hover td{background:#1c2129}
.tag{padding:1px 8px;border-radius:99px;font-size:11px;border:1px solid}
.tag.pending{color:var(--dim);border-color:var(--line)}
.tag.running{color:var(--warn);border-color:var(--warn)}
.tag.registered{color:var(--ok);border-color:var(--ok)}
.tag.submitted{color:#5cc0a8;border-color:#5cc0a8}
.tag.failed{color:var(--bad);border-color:var(--bad)}
button.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;
           cursor:pointer;font-size:13px}
button.btn.ghost{background:none;border:1px solid var(--line);color:var(--fg)}
button.btn.danger{background:var(--bad)}
button.btn:disabled{opacity:.45;cursor:default}
input,select{background:#12161b;border:1px solid var(--line);color:var(--fg);border-radius:8px;
             padding:7px 10px;font-size:13px;font-family:inherit}
textarea{width:100%;background:#0c0f12;border:1px solid var(--line);color:var(--fg);
         border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
         resize:vertical}
code{background:#12161b;border:1px solid var(--line);border-radius:4px;padding:1px 5px;
     font-size:12px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.dim{color:var(--dim);font-size:12px}
#log{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:#0c0f12;border:1px solid var(--line);
     border-radius:10px;padding:12px;height:60vh;overflow:auto;white-space:pre-wrap}
#log .ok{color:var(--ok)}#log .error{color:var(--bad)}#log .warn{color:var(--warn)}
#log .info{color:var(--fg)}
.login{max-width:340px;margin:18vh auto;text-align:center}
.login input{width:100%;margin:12px 0}
.err{color:var(--bad);font-size:13px;min-height:18px}
</style></head><body>

<div id="login" class="login" style="display:none">
  <h1>Oasis 控制台</h1>
  <p class="dim">需要密码才能访问</p>
  <input id="pw" type="password" placeholder="管理密码" autocomplete="current-password">
  <button class="btn" onclick="doLogin()">进入</button>
  <div id="loginerr" class="err"></div>
</div>

<div id="app" style="display:none">
<header>
  <h1>Oasis 控制台</h1>
  <nav>
    <button data-t="dash" class="on">仪表盘</button>
    <button data-t="mail">邮箱池</button>
    <button data-t="proxy">代理池</button>
    <button data-t="reg">预约记录</button>
    <button data-t="log">日志</button>
    <button data-t="set">设置</button>
  </nav>
  <span style="flex:1"></span>
  <span id="runstate" class="dim"></span>
  <button class="btn ghost" onclick="doLogout()">退出</button>
</header>
<main>
  <section id="t-dash" class="on">
    <div class="cards" id="cards"></div>
    <div class="panel"><h2>运行控制</h2>
      <div class="row">
        <label>模式 <select id="mode">
          <option value="browser">浏览器（Playwright 全流程）</option>
          <option value="hybrid">混合（共享浏览器取 captcha）</option>
        </select></label>
        <label>线程 <input id="threads" type="number" min="1" max="64" style="width:80px"></label>
        <button class="btn" id="btnStart" onclick="startEngine()">启动</button>
        <button class="btn danger" onclick="stopEngine()">停止</button>
      </div>
      <p class="dim" id="hosthint" style="margin:10px 0 0"></p>
    </div>
  </section>

  <section id="t-mail">
    <div class="panel"><h2>iCloud 别名（勾选后导入）</h2>
      <p class="dim">从本地 iCloud 隐藏邮箱服务拉取别名列表。iCloud 建别名有配额
        （约每小时 10 个），所以没有全选——<b>只导入你真正要用的</b>。
        已导入的会标出来。服务地址和密码在「设置」页里配。</p>
      <div class="row" style="margin-bottom:8px">
        <button class="btn" onclick="loadIcloud()">拉取别名列表</button>
        <button class="btn ghost" onclick="icloudAll(false)">全不选</button>
        <button class="btn ghost" onclick="toggleUnused()">只选未导入的</button>
        <button class="btn" onclick="doIcloudImport()">导入勾选项</button>
        <span class="dim" id="icloudmsg"></span>
      </div>
      <div id="icloudlist" class="dim">还没有拉取。</div>
    </div>

    <div class="panel"><h2>手动导入账号</h2>
      <p class="dim">每行一个，支持三种格式；空行与 # 开头的行会忽略。
        格式：<code>outlook@x.com----密码----client_id----refresh_token</code> ·
        <code>gmail@x.com----密码----client_id----client_secret----refresh_token</code> ·
        <code>别名@icloud.com----acc_xxxxxxxx----hme</code></p>
      <textarea id="importbox" rows="6" placeholder="粘贴凭据行…"></textarea>
      <div class="row" style="margin-top:8px">
        <label>协议 <select id="importproto">
          <option value="auto">自动</option>
          <option value="graph">Microsoft Graph</option>
          <option value="imap">IMAP</option>
          <option value="hme">iCloud HME</option>
        </select></label>
        <button class="btn" onclick="doImport()">导入</button>
        <span class="dim" id="importmsg"></span>
      </div>
    </div>

    <div class="panel"><h2>账号池</h2>
      <div class="row" style="margin-bottom:10px">
        <select id="fstatus" onchange="loadAccounts(1)">
          <option value="">全部</option>
          <option value="pending">待注册</option>
          <option value="running">进行中</option>
          <option value="registered">邮件确认</option>
          <option value="submitted">页面确认</option>
          <option value="failed">失败</option>
        </select>
        <button class="btn ghost" onclick="resetFailed()">重置失败与卡住项</button>
        <button class="btn ghost" onclick="exportFile('creds')">导出凭据</button>
        <span style="flex:1"></span>
        <span class="dim" id="mailcount"></span>
      </div>
      <div style="overflow:auto"><table id="mtable"><thead><tr>
        <th>ID</th><th>邮箱</th><th>协议</th><th>状态</th><th>姓名</th><th>电话</th><th>备注</th>
      </tr></thead><tbody></tbody></table></div>
      <div class="row" style="margin-top:10px">
        <button class="btn ghost" onclick="loadAccounts(curPage-1)" id="prev">上一页</button>
        <span class="dim" id="pageno"></span>
        <button class="btn ghost" onclick="loadAccounts(curPage+1)" id="next">下一页</button>
      </div>
    </div>
  </section>

  <section id="t-proxy">
    <div class="panel"><h2>代理池配置</h2>
      <p class="dim">每行一个。支持 <code>http://user:pass@host:port</code>、
        <code>socks5://user:pass@host:port</code>、<code>host:port</code>。
        链路写法：<code>前置代理|上游</code>。保存后立即生效，并写进配置文件，
        重启不会丢。</p>
      <textarea id="proxybox" rows="10" placeholder="每行一个代理…"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="btn" onclick="saveProxies()">保存并生效</button>
        <span class="dim" id="proxymsg"></span>
      </div>
      <p class="dim" style="margin-top:8px">注意：保存时会用输入框里的内容**整体替换**。
        凭据在加载时是遮罩显示的，如果不动就原样保存——要改动请整行重写。</p>
    </div>

    <div class="panel"><h2>代理健康度</h2>
      <p class="dim" id="proxysummary"></p>
      <div style="overflow:auto"><table id="ptable"><thead><tr>
        <th>代理</th><th>成功</th><th>失败</th><th>冷却中</th><th>最后错误</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
  </section>

  <section id="t-reg">
    <div class="panel"><h2>预约记录</h2>
      <div style="overflow:auto"><table id="rtable"><thead><tr>
        <th>账号</th><th>场次（按偏好顺序）</th><th>模式</th><th>状态</th><th>时间</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
  </section>

  <section id="t-log">
    <div class="panel"><h2>实时日志</h2>
      <div class="row" style="margin-bottom:8px">
        <label><input type="checkbox" id="autoscroll" checked> 自动滚动</label>
        <button class="btn ghost" onclick="clearLogView()">清屏</button>
      </div>
      <div id="log"></div>
    </div>
  </section>

  <section id="t-set">
    <div class="panel"><h2>设置</h2>
      <div id="setform" class="row" style="flex-direction:column;align-items:stretch"></div>
      <p class="dim">改动会写回服务端配置文件。代理池、Google 出口这类多行内容用换行分隔。</p>
    </div>
    <div class="panel"><h2>数据库维护</h2>
      <div class="row">
        <button class="btn ghost" onclick="vacuum()">VACUUM 压缩</button>
        <button class="btn ghost" onclick="exportFile('accounts')">导出账号 JSON</button>
        <span style="flex:1"></span>
        <select id="delstatus">
          <option value="failed">失败</option>
          <option value="submitted">页面确认</option>
          <option value="registered">邮件确认</option>
          <option value="pending">待注册</option>
        </select>
        <button class="btn danger" onclick="delBucket()">清理该状态</button>
      </div>
    </div>
  </section>
</main>
</div>

<script>
const $ = s => document.querySelector(s);
let curPage = 1, logSeq = 0, timer = null;

async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  if (r.status === 401){ showLogin(); throw new Error('auth'); }
  return r.json();
}
function showLogin(){ $('#login').style.display='block'; $('#app').style.display='none'; }
function showApp(){ $('#login').style.display='none'; $('#app').style.display='block'; }

async function doLogin(){
  const pw = $('#pw').value;
  const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'},
                                   body: JSON.stringify({password: pw})});
  if (r.ok){ location.reload(); }
  else {
    const j = await r.json().catch(()=>({}));
    $('#loginerr').textContent = j.error === 'RATE_LIMITED'
      ? '尝试次数过多，请稍后再试' : '密码错误';
  }
}
async function doLogout(){ await fetch('/logout', {method:'POST'}); location.reload(); }

document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('on'));
  document.querySelectorAll('section').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  $('#t-' + b.dataset.t).classList.add('on');
  if (b.dataset.t === 'log') startLog(); else stopLog();
  if (b.dataset.t === 'mail') loadAccounts(1);
  if (b.dataset.t === 'proxy') loadProxies();
  if (b.dataset.t === 'reg') loadRegs();
  if (b.dataset.t === 'set') loadSettings();
});

const LABEL = {total:'账号总数', pending:'待注册', running:'进行中',
               registered:'邮件确认', submitted:'页面确认', failed:'失败',
               registrations:'预约记录'};

async function tick(){
  let s;
  try { s = await api('/api/state'); } catch(e){ return; }
  const st = s.stats || {};
  $('#cards').innerHTML = Object.keys(LABEL).map(k =>
    `<div class="card"><div class="l">${LABEL[k]}</div>
     <div class="n">${st[k] ?? 0}</div></div>`).join('');
  $('#runstate').textContent = (s.running ? '● 运行中' : '○ 空闲') +
    ' · ' + s.state + ' · 线程 ' + (s.threads || '-');
  $('#btnStart').disabled = !!s.running;
  if (!$('#threads').value && s.host) {
    $('#threads').value = s.config?.threads || s.host.recommended || 1;
  }
  if (s.host){
    const h = s.host;
    $('#hosthint').textContent = h.total_mb
      ? `内存 ${h.total_mb - h.available_mb}/${h.total_mb} MB 在用 · ${h.cores} 核 · 建议线程 ${h.recommended}`
      : `建议线程 ${h.recommended}`;
  }
  if (s.config && !$('#mode').dataset.init){
    $('#mode').value = s.config.mode || 'browser';
    $('#threads').value = s.config.threads || 1;
    $('#mode').dataset.init = 1;
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
  if (!d.rows.length){ tb.innerHTML = '<tr><td colspan="7" class="dim">没有数据</td></tr>'; }
  else tb.innerHTML = d.rows.map(r => `<tr>
     <td>${r.id}</td><td>${esc(r.email)}</td><td>${r.protocol||''}</td>
     <td><span class="tag ${r.status}">${LABEL[r.status]||r.status}</span></td>
     <td>${esc((r.first_name||'')+' '+(r.last_name||''))}</td>
     <td>${esc(r.phone||'')}</td>
     <td class="dim">${esc((r.error||'').slice(0,60))}</td></tr>`).join('');
  $('#mailcount').textContent = `共 ${d.total} 条`;
  $('#pageno').textContent = `第 ${d.page} / ${Math.max(1, Math.ceil(d.total/d.per))} 页`;
  $('#prev').disabled = d.page <= 1;
  $('#next').disabled = d.page * d.per >= d.total;
}
// Proxy urls carry user:pass; never render the credentials into the page.
function mask(u){ return String(u||'').replace(/\/\/[^@/]+@/, '//***:***@'); }
function esc(s){ return String(s??'').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function loadProxies(){
  const d = await api('/api/proxies');
  $('#proxysummary').textContent = `共 ${d.total} 个（表格显示前 ${d.rows.length}）`;
  $('#ptable tbody').innerHTML = d.rows.map(r => `<tr>
    <td>${esc(mask(r.url))}</td><td>${r.ok ?? 0}</td><td>${r.fail ?? 0}</td>
    <td>${r.cooling ? '<span class="tag running">是</span>' : ''}</td>
    <td class="dim">${esc((r.last_error||'').slice(0,60))}</td></tr>`).join('')
    || '<tr><td colspan="5" class="dim">没有数据</td></tr>';
  // only seed the editor on first open, so a reload does not wipe typing
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
  $('#icloudmsg').textContent = `共 ${d.total} 个，已导入 ${d.aliases.filter(a=>a.imported).length} 个`;
  $('#icloudlist').innerHTML = d.aliases.map(a => `
    <label class="row" style="gap:8px;padding:4px 0;border-bottom:1px solid var(--line)">
      <input type="checkbox" data-email="${esc(a.email)}" data-imported="${a.imported?1:0}"
             ${a.imported ? 'disabled' : ''} style="width:auto">
      <span style="min-width:240px">${esc(a.email)}</span>
      <span class="tag ${a.active ? 'registered' : 'failed'}">${a.active ? '启用' : '停用'}</span>
      <span class="dim">${esc(a.account_name||'')}</span>
      <span class="dim">${esc(a.label||'')}</span>
      <span class="dim">${a.imported ? '（已在池中）' : ''}</span>
    </label>`).join('');
}
async function doIcloudImport(){
  const emails = icloudBoxes().filter(b => b.checked && !b.disabled)
                             .map(b => b.dataset.email);
  if (!emails.length){ $('#icloudmsg').textContent = '先勾选要导入的别名'; return; }
  const d = await api('/api/icloud/import', {method:'POST',
    body: JSON.stringify({emails})});
  if (d.error){ $('#icloudmsg').textContent = d.error; return; }
  $('#icloudmsg').textContent = `导入 ${d.added} 个，已在池中 ${d.duplicate} 个`;
  loadIcloud(); loadAccounts(1); tick();
}

async function doImport(){
  const lines = $('#importbox').value;
  if (!lines.trim()){ $('#importmsg').textContent = '先粘贴内容'; return; }
  const d = await api('/api/accounts/import', {method:'POST', body: JSON.stringify({
    lines, protocol: $('#importproto').value })});
  $('#importmsg').textContent = `新增 ${d.added} 条，重复 ${d.duplicate} 条`;
  $('#importbox').value = '';
  loadAccounts(1); tick();
}
async function loadRegs(){
  const d = await api('/api/registrations');
  // Field names mirror Store.registrations(); the server also adds `shows`,
  // already translated from the poll-answer uuids.
  $('#rtable tbody').innerHTML = d.rows.map(r => `<tr>
    <td>${esc(r.email || '')}</td>
    <td>${esc(r.shows || '—')}</td>
    <td>${esc(r.mode || '')}</td>
    <td><span class="tag ${esc(r.status||'')}">${LABEL[r.status] || esc(r.status||'')}</span></td>
    <td class="dim">${esc(r.created_at || '')}</td></tr>`).join('')
    || '<tr><td colspan="5" class="dim">还没有记录</td></tr>';
}

function startLog(){ if (timer) return; pollLog(); timer = setInterval(pollLog, 2000); }
function stopLog(){ if (timer){ clearInterval(timer); timer = null; } }
function clearLogView(){ $('#log').textContent = ''; }
async function pollLog(){
  const d = await api('/api/log?since=' + logSeq);
  logSeq = d.seq;
  const box = $('#log');
  for (const r of d.rows){
    const t = new Date(r.at * 1000).toTimeString().slice(0,8);
    const el = document.createElement('div');
    el.className = r.level;
    el.textContent = `[${t}] ${r.msg}`;
    box.appendChild(el);
  }
  if (d.rows.length && $('#autoscroll').checked) box.scrollTop = box.scrollHeight;
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
    const wide = ['shows','front_proxy','google_proxy','mail_proxy','hme_base','db_path'].includes(k);
    return `<div class="row"><label style="width:190px" class="dim">${label}</label>
      <input data-k="${k}" value="${esc(v)}" style="flex:1;min-width:220px"
             ${wide?'':'size=20'}></div>`;
  }).join('');
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = '<button class="btn" onclick="saveSettings()">保存</button>';
  $('#setform').appendChild(row);
}
async function saveSettings(){
  const body = {};
  document.querySelectorAll('#setform input[data-k]').forEach(i => {
    let v = i.value;
    if (i.dataset.k === 'threads') v = parseInt(v || '1');
    if (v === 'true') v = true; else if (v === 'false') v = false;
    if (i.dataset.k === 'shows') v = v.split(',').map(x=>x.trim()).filter(Boolean);
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
    if (r.status === 401){ showLogin(); return; }
    showApp(); tick(); setInterval(tick, 3000);
  } catch(e){ showLogin(); }
})();
$('#pw')?.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
// mark the proxy editor as touched, so a background reload does not clobber it
document.addEventListener('input', e => {
  if (e.target && e.target.id === 'proxybox') e.target.dataset.dirty = '1';
});
</script></body></html>
"""
