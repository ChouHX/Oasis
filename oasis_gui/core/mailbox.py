#!/usr/bin/env python3
"""Mailbox readers: every reader returns the same thing - a page of Mail.

The job here is now only to *read* an inbox. Nothing in this module submits
anything, waits for a link, or decides what a message means: it hands back
subject / sender / body / timestamp and `core.hitcheck` decides the rest. That
split is what let the registration half of the program be deleted outright.

A reader is one credential line. Five shapes are supported:

    email----password----client_id----refresh_token                    (Outlook)
    email----password----client_id----client_secret----refresh_token    (Gmail)
    email----acc_xxxxxxxx----hme                    (iCloud alias, local service)
    alias@icloud.com----<gmail>----<app password>----gmail-imap
                                                        (iCloud alias via Gmail)

The fourth field of the Microsoft form is a refresh token. It can be redeemed
for either audience, so the same line works for both protocols:

  * Graph -> scope https://graph.microsoft.com/.default
             GET https://graph.microsoft.com/v1.0/me/messages
  * IMAP  -> scope https://outlook.office.com/IMAP.AccessAsUser.All
             AUTH XOAUTH2 against outlook.office365.com:993

Note the IMAP scope is on outlook.office.com; the office365.com variant is
rejected with AADSTS70011 invalid_scope for consumer accounts. The IMAP access
token comes back opaque (not a JWT), which is normal for MSA.

IMAP needs a raw TLS socket, so it cannot use the HTTP_PROXY environment
variables that urllib honours. The transport is therefore opened explicitly
through a SOCKS5 or HTTP CONNECT proxy, auto-detected from the environment.

MSA rotates the refresh token on every redemption, so the newest one is kept in
memory and handed back to the caller for persistence - losing it loses the box.
"""
import base64
import email
import email.header
import imaplib
import json
import os
import re
import socket
import threading
import time
import datetime
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime

CLIENT_ID_DEFAULT = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993

# Google has no Graph equivalent: the refresh token is exchanged at
# oauth2.googleapis.com for a https://mail.google.com/ token, which is then
# presented to imap.gmail.com with XOAUTH2.
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_HOST = "imap.gmail.com"
GMAIL_SCOPE = "https://mail.google.com/"
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")

# iCloud Hide-My-Email via a local HME service. Reads need no CSRF, only the
# session cookie, and one login covers the whole session TTL.
HME_BASE_DEFAULT = "http://127.0.0.1:8081"
HME_ICLOUD_DOMAINS = ("icloud.com", "me.com", "mac.com")

PROTOCOLS = ("graph", "imap", "auto")
PROTOCOL_LABEL = {
    "graph": "Graph API",
    "imap": "IMAP (XOAUTH2)",
    "auto": "自动（Graph 失败回退 IMAP）",
}

# iCloud Hide-My-Email aliases read through the Gmail account that owns the
# Apple ID. Stored in the accounts.protocol column, so a row says how to read
# it without needing a new column.
ALIAS_PROTOCOL = "alias-imap"


@dataclass(frozen=True)
class Mail:
    """One message, as much of it as a reader could recover.

    `subject` is already header-decoded. The site MIME-encodes it
    (`=?UTF-8?Q?Oasis_Live_=E2=80=9927_Registration_Complete?=`), and a rule
    that greps the raw header never matches - a mistake this program already
    paid for once.
    """

    subject: str = ""
    sender: str = ""
    body: str = ""
    stamp: float = 0.0
    folder: str = field(default="")

    def text(self):
        return f"{self.subject}\n{self.sender}\n{self.body}"


def is_gmail(email):
    return (email or "").rsplit("@", 1)[-1].lower() in GMAIL_DOMAINS


def parse_cred(line):
    """Parse one credential line.

    Microsoft (4 fields):
        email----password----client_id----refresh_token
    Google (5 fields, client_secret is required to refresh):
        email----password----client_id----client_secret----refresh_token

    A Gmail address with only 4 fields is rejected loudly rather than failing
    later with a confusing 401: Google will not mint a token without the secret.
    """
    parts = [p.strip() for p in line.strip().split("----")]
    # iCloud Hide-My-Email read through Gmail:
    #   alias@icloud.com----<the gmail that owns the Apple ID>----<app password>----gmail-imap
    if len(parts) >= 3 and parts[-1].lower() in ("gmail-imap", "alias-imap"):
        return {"email": parts[0], "client_id": parts[1], "password": parts[2],
                "refresh_token": "", "provider": "alias"}
    # iCloud Hide-My-Email:  alias@icloud.com----acc_xxxxxxxx----hme
    if len(parts) >= 2 and parts[-1].lower() == "hme":
        return {"email": parts[0], "hme_account": parts[1] if len(parts) > 2 else "",
                "provider": "hme"}
    if len(parts) >= 5:
        return {"email": parts[0], "password": parts[1], "client_id": parts[2],
                "client_secret": parts[3], "refresh_token": parts[4],
                "provider": "google"}
    if len(parts) >= 4:
        email = parts[0]
        if is_gmail(email):
            raise ValueError(
                f"{email}: Gmail needs 5 fields "
                "(email----password----client_id----client_secret----refresh_token); "
                "Google will not refresh a token without the client secret")
        return {"email": email, "password": parts[1],
                "client_id": (parts[2] or CLIENT_ID_DEFAULT),
                "refresh_token": parts[3], "provider": "microsoft"}
    raise ValueError("expected email----password----client_id----refresh_token "
                     "(or the 5-field Gmail form)")


# 单次网络操作的超时，以及一次读信箱允许的重试次数。
#
# 这组数字是给「不可达的邮箱」定的。实测（2026-09-18）：一个直连不通的 Gmail 账号
# 会把整轮巡检拖住六分钟以上 —— 每次 token 请求 45s、重试 4 次、IMAP 握手再来
# 3 轮，全都不报错、只是慢。检测程序每一轮要给几十个邮箱轮一遍，单个账号的预算
# 必须是「几十秒失败」，否则一个坏邮箱就能吃掉整轮。
NET_TIMEOUT = 20
NET_ATTEMPTS = 2


class MailAuthError(Exception):
    """The mailbox rejected the token. Retrying will not help.

    Seen in practice: a client_id can redeem `graph.microsoft.com/.default`
    successfully and then have Graph answer 401 UnknownError for every call -
    that registration simply has no Graph mail permission, and the account has
    to be read over IMAP instead.
    """


def _retry(fn, attempts=NET_ATTEMPTS, delay=1.2):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except MailAuthError:
            raise
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise MailAuthError(
                    f"mailbox rejected the token (HTTP {e.code}); this client_id "
                    f"likely lacks permission for this API") from e
            last = e
            time.sleep(delay * (i + 1))
        except Exception as e:
            last = e
            time.sleep(delay * (i + 1))
    raise last


def _open(req, timeout=NET_TIMEOUT, opener=None):
    op = opener if opener is not None else DIRECT_OPENER
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# Mail goes straight out by default. The residential proxy exists to make the
# *registration* traffic look like a US household; talking to Microsoft does not
# need that, and routing it through a flaky upstream is how "verification mail
# never arrived" happens. Set mail_proxy to force it through one anyway.
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------- transport
def _connect_direct(host, port, timeout):
    return socket.create_connection((host, port), timeout=timeout)


def _connect_socks5(host, port, proxy_url, timeout):
    import socks
    u = urllib.parse.urlparse(proxy_url)
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, u.hostname, u.port or 1080,
                username=u.username or None, password=u.password or None)
    s.settimeout(timeout)
    s.connect((host, port))
    return s


def _connect_http(host, port, proxy_url, timeout):
    u = urllib.parse.urlparse(proxy_url)
    s = socket.create_connection((u.hostname, u.port or 8080), timeout=timeout)
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if u.username:
        token = base64.b64encode(
            f"{u.username}:{u.password or ''}".encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    s.sendall((req + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(256)
        if not chunk:
            break
        buf += chunk
    if not buf.split(b"\r\n", 1)[0].split()[1:2] == [b"200"]:
        s.close()
        raise OSError(f"proxy CONNECT refused: {buf[:80]!r}")
    return s


def open_tunnel(host, port, timeout=NET_TIMEOUT, proxy_url=""):
    """Raw TCP socket to host:port.

    Empty proxy_url means a direct dial, which is the default for mail: the
    Microsoft endpoints are reachable without one and going direct removes a
    whole class of failure (and is measurably faster).
    """
    if not proxy_url:
        return _connect_direct(host, port, timeout)
    scheme = urllib.parse.urlparse(proxy_url).scheme.lower()
    if scheme.startswith("socks"):
        return _connect_socks5(host, port, proxy_url, timeout)
    return _connect_http(host, port, proxy_url, timeout)


class TunnelIMAP4(imaplib.IMAP4_SSL):
    """IMAP4 over a proxied, TLS-wrapped socket."""

    proxy_url = None
    timeout = 30

    def _create_socket(self, timeout=None):
        raw = open_tunnel(self.host, self.port, timeout or self.timeout,
                          self.proxy_url)
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)


# ----------------------------------------------------------------------- mailboxes
class BaseMailbox:
    protocol = "graph"
    scope = GRAPH_SCOPE

    def __init__(self, cred, proxy_url=""):
        self.email = cred["email"]
        self.password = cred.get("password", "")
        self.client_id = cred.get("client_id") or CLIENT_ID_DEFAULT
        self.refresh_token = cred["refresh_token"]
        # "" = direct. Only set this when the network actually needs a hop.
        self.proxy_url = proxy_url or ""
        self._access = None
        self._access_exp = 0.0
        self._lock = threading.Lock()

    def _opener(self):
        if not self.proxy_url:
            return DIRECT_OPENER
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy_url,
                                         "https": self.proxy_url}))

    def access_token(self, force=False):
        with self._lock:
            if not force and self._access and time.time() < self._access_exp - 60:
                return self._access
            opener = self._opener()
            data = _retry(lambda: _open(urllib.request.Request(
                TOKEN_URL,
                data=urllib.parse.urlencode({
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "scope": self.scope,
                }).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST"), opener=opener))
            self._access = data["access_token"]
            self._access_exp = time.time() + int(data.get("expires_in", 3600))
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
            return self._access

    def cred_line(self):
        """Current credential line, including any rotated refresh token."""
        return (f"{self.email}----{self.password}----{self.client_id}----"
                f"{self.refresh_token}")

    def close(self):
        """Release any transport held open between calls.

        A no-op for readers that reconnect on every fetch. The Gmail alias
        reader keeps an IMAP session alive across polls, so whoever owns it has
        to say when it is done.
        """
        return None

    def messages(self, limit=12, not_before=None):
        """Newest-first page of Mail for this mailbox.

        `not_before` is an epoch floor. Readers that can push the filter to the
        server (Gmail's SEARCH SINCE) do; readers that cannot accept it and
        filter at the end. Either way the caller sees only what it asked for.
        """
        raise NotImplementedError


GRAPH_SELECT = "subject,receivedDateTime,from,body"


class GraphMailbox(BaseMailbox):
    protocol = "graph"
    scope = GRAPH_SCOPE

    def _folder_messages(self, path, top):
        token = self.access_token()
        opener = self._opener()
        url = f"{GRAPH}{path}?$top={top}&$select={GRAPH_SELECT}"
        data = _retry(lambda: _open(urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token,
                          "Accept": "application/json"}), opener=opener))
        return data.get("value", [])

    def messages(self, limit=15, not_before=None):
        """Inbox **and** Junk, newest first.

        Graph's /me/messages returns the Inbox only - measured on a real
        mailbox: 13 messages, all Inbox, with the Junk folder invisible even
        though it held mail. The site's mail is routinely filed as junk, so the
        junk folder is queried separately (well-known name `junkemail`).
        """
        msgs = self._folder_messages("/me/messages", limit)
        try:
            msgs += self._folder_messages("/me/mailFolders/junkemail/messages", limit)
        except Exception:
            pass                      # folder missing or not exposed by the API
        seen, out = set(), []
        for m in msgs:
            key = m.get("id") or m.get("internetMessageId") or id(m)
            if key in seen:
                continue
            seen.add(key)
            out.append(_graph_mail(m))
        out.sort(key=lambda x: x.stamp or 0, reverse=True)
        return out[:limit]


def _folder_name(line):
    """Mailbox name from one LIST response line.

    The format is `(flags) "delimiter" name`, and the name may or may not be
    quoted - so split off the flags, then the delimiter, and take the rest.
    (Naively taking the last quoted token yields the delimiter itself.)
    """
    after_flags = line.split(") ", 1)[-1]
    parts = after_flags.split(" ", 1)
    if len(parts) < 2:
        return None
    return parts[1].strip().strip('"') or None


def _candidate_folders(M):
    """INBOX plus whatever the server flags as junk.

    Outlook files a good share of these verification mails under Junk, so
    searching INBOX alone silently misses them and the caller reports "mail
    never arrived". The folder name is localised ("Junk", "垃圾邮件", ...), so it
    is discovered from the LIST attributes rather than hardcoded.
    """
    out = ["INBOX"]
    try:
        typ, boxes = M.list()
        for raw in boxes or []:
            line = raw.decode(errors="replace")
            if "\\Junk" not in line and "\\Spam" not in line:
                continue
            name = _folder_name(line)
            if name and name not in out:
                out.append(name)
    except Exception:
        pass
    return out


class ImapMailbox(BaseMailbox):
    protocol = "imap"
    scope = IMAP_SCOPE

    def __init__(self, cred, host=IMAP_HOST, port=IMAP_PORT, proxy_url="",
                 timeout=NET_TIMEOUT):
        super().__init__(cred, proxy_url)
        self.host = host
        self.port = port
        self.timeout = timeout

    def connect(self, attempts=NET_ATTEMPTS):
        """Authenticated IMAP session.

        Outlook intermittently answers XOAUTH2 with "User is authenticated but
        not connected" even for a perfectly good token, so the handshake is
        retried with a fresh access token.

        The token fetch itself is deliberately *outside* the retry loop. It used
        to be inside it, which multiplied the two budgets: measured on a network
        that cannot reach Google, one Gmail account took 8.5 minutes to fail
        (3 token attempts × 3 handshakes × 25s), and a sweep that has to visit
        dozens of mailboxes cannot afford that for one dead address. A token
        that cannot be fetched is a network or credential problem; retrying the
        IMAP handshake will not change it.
        """
        token = self.access_token()
        last = None
        for i in range(attempts):
            M = None
            try:
                cls = type("BoundTunnelIMAP4", (TunnelIMAP4,), {
                    "proxy_url": self.proxy_url, "timeout": self.timeout})
                M = cls(self.host, self.port, timeout=self.timeout)
                auth = (f"user={self.email}\x01auth=Bearer "
                        f"{token}\x01\x01")
                M.authenticate("XOAUTH2", lambda _: auth.encode())
                return M
            except Exception as e:
                last = e
                try:
                    M.logout()
                except Exception:
                    pass
                if i + 1 < attempts:
                    # A fresh token is the documented cure for the intermittent
                    # "not connected" rejection; a network failure will raise
                    # here instead of burning the rest of the budget.
                    token = self.access_token(force=True)
                    time.sleep(1.0)
        raise last

    def messages(self, limit=12, not_before=None):
        """Newest-first page of Mail, INBOX + Junk.

        `not_before` is accepted and ignored here: this reader always pulls the
        newest N regardless, which is right for a mailbox that only holds the
        few accounts pointed at it. Subclasses with a busier inbox use it to
        narrow the search on the server instead of filtering at the end.
        """
        M = self.connect()
        try:
            out = []
            for folder in _candidate_folders(M):
                try:
                    typ, _ = M.select(folder, readonly=True)
                    if typ != "OK":
                        continue
                    typ, data = M.search(None, "ALL")
                    if typ != "OK":
                        continue
                except Exception:
                    continue
                for i in reversed(data[0].split()[-limit:]):
                    try:
                        typ, raw = M.fetch(i, "(RFC822)")
                    except Exception:
                        continue
                    if not raw or not isinstance(raw[0], tuple):
                        continue
                    out.append(_message_mail(
                        email.message_from_bytes(raw[0][1]), folder))
            out.sort(key=lambda m: m.stamp or 0, reverse=True)
            return out[:limit]
        finally:
            try:
                M.logout()
            except Exception:
                pass


class HmeMailbox(BaseMailbox):
    """Reads an iCloud Hide-My-Email alias through a local HME service.

    Two things about that service shape this class:

      * The Oasis verification mail puts its link in an HTML button ("click the
        button above"), so the plain-text `body` never contains it - the link
        exists only in `body_html`. Any reader that trusts `body`/`preview`
        will spin until the timeout.
      * Reads need only the session cookie (no CSRF), and one login lasts the
        whole TTL, so the session is opened lazily and re-opened once on 401.
    """

    protocol = "hme"
    scope = None

    def __init__(self, cred, proxy_url="", timeout=NET_TIMEOUT):
        self.email = cred["email"]
        # client_id is where add_mailbox persists the HME account id
        self.account = cred.get("hme_account") or cred.get("client_id") or ""
        self.base = (cred.get("hme_base") or os.environ.get("ICLOUD_HME_BASE")
                     or HME_BASE_DEFAULT).rstrip("/")
        self.password = (cred.get("hme_password")
                         or os.environ.get("ICLOUD_HME_ADMIN_PASSWORD") or "")
        self.proxy_url = proxy_url or ""
        self.timeout = timeout
        self.refresh_token = ""
        self._cj = http.cookiejar.CookieJar()
        # ProxyHandler({}) on purpose: the local HME service must not be
        # dialled through the sandbox's ALL_PROXY - that answers 400.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cj))
        self._lock = threading.Lock()
        self._logged_in = False

    # -------------------------------------------------------------- transport
    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("accept", "application/json")
        if data:
            req.add_header("content-type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw or "{}")
            except Exception:
                return e.code, {"raw": raw[:200]}
        except Exception as e:
            raise MailAuthError(f"HME service unreachable: {type(e).__name__}")

    def _call(self, path, body=None, method="GET"):
        with self._lock:
            if not self._logged_in:
                self._login()
        status, data = self._request(method, path, body)
        if status == 401:                      # session expired - one retry
            with self._lock:
                self._logged_in = False
                self._login()
            status, data = self._request(method, path, body)
        if status >= 400:
            raise MailAuthError(f"HME {status}: "
                                f"{str(data.get('error') or data)[:120]}")
        return data.get("data") or {}

    def _login(self):
        if not self.password:
            raise MailAuthError(
                "iCloud HME needs ICLOUD_HME_ADMIN_PASSWORD (or hme_password)")
        status, data = self._request("POST", "/api/auth/login",
                                     {"password": self.password})
        if status != 200 or not data.get("success"):
            raise MailAuthError(f"HME login failed: HTTP {status} "
                                f"{str(data.get('error') or '')[:80]}")
        self._logged_in = True
        if not self.account:
            accts = (self._request("GET", "/api/accounts")[1].get("data") or [])
            if accts:
                self.account = accts[0].get("id", "")

    def access_token(self, force=False):
        """No bearer token here; the session cookie is the credential."""
        with self._lock:
            if not self._logged_in:
                self._login()
        return None

    def cred_line(self):
        return f"{self.email}----{self.account}----hme"

    # ----------------------------------------------------------------- reading
    def messages(self, limit=12, not_before=None):
        """Newest messages for this alias, link-bearing HTML flattened in.

        `preview` is returned by the list endpoint but omits the button URL, so
        each message's detail is fetched to recover `body_html`. Those are
        local calls - a handful per poll is cheaper than a miss.
        """
        out = []
        days = 2
        if not_before:
            days = max(1, min(90, int((time.time() - not_before) / 86400) + 1))
        listing = self._call(f"/api/inbox?account_id={urllib.parse.quote(self.account)}"
                             f"&alias={urllib.parse.quote(self.email)}"
                             f"&limit={max(1, min(int(limit), 100))}&days={days}")
        for m in (listing.get("messages") or [])[:limit]:
            mid = m.get("id")
            if mid is None:
                continue
            body = m.get("preview") or ""
            try:
                detail = self._call(f"/api/inbox/{urllib.parse.quote(str(mid))}"
                                    f"?account_id={urllib.parse.quote(self.account)}")
                body += "\n" + (detail.get("body_html") or detail.get("body") or "")
            except Exception:
                pass                            # keep the preview-only entry
            out.append(Mail(subject=_decode_header(m.get("subject") or ""),
                            sender=_hme_sender(m), body=body,
                            stamp=_iso_to_epoch(m.get("date")) or 0.0,
                            folder="inbox"))
        out.sort(key=lambda m: m.stamp or 0, reverse=True)
        return out[:limit]


def _iso_to_epoch(value):
    """'2026-09-16T14:32:10+08:00' -> epoch seconds, or None."""
    if not value:
        return None
    try:
        txt = str(value).strip().replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(txt).timestamp()
    except Exception:
        return None


class AutoMailbox:
    """Graph first, IMAP as fallback.

    Both readers redeem the same refresh token through the same base-class
    access_token() - only the scope differs - so a mailbox that cannot be read
    over one channel can simply be read over the other. The primary gets half the
    budget; if it errors or comes up empty the fallback takes the rest, and the
    rotated refresh token travels with the switch because Graph may already have
    replaced it.
    """

    protocol = "auto"

    def __init__(self, cred, proxy_url=""):
        self.email = cred["email"]
        self.proxy_url = proxy_url or ""
        self.primary = GraphMailbox(cred, proxy_url)
        self.fallback = ImapMailbox(cred, proxy_url=proxy_url)
        self._current = self.primary

    @property
    def refresh_token(self):
        return self._current.refresh_token or self.primary.refresh_token

    def cred_line(self):
        return self._current.cred_line()

    def messages(self, limit=12, not_before=None):
        """Read through whichever channel can actually see this mailbox.

        Graph first. A 401 means this registration has no Graph mail permission
        - it fails identically every time, so switch instead of retrying. Any
        other error switches too, and the rotated refresh token travels with
        the switch because Graph may already have replaced it.
        """
        try:
            out = self.primary.messages(limit, not_before)
            self._current = self.primary
            return out
        except MailAuthError as e:
            first = str(e)[:90]
        except Exception as e:
            first = f"{type(e).__name__}: {str(e)[:90]}"
        self.fallback.refresh_token = (self.primary.refresh_token
                                       or self.fallback.refresh_token)
        self._current = self.fallback
        try:
            return self.fallback.messages(limit, not_before)
        except Exception as e:
            raise MailAuthError(f"Graph 与 IMAP 都读不到：{first} / "
                                f"{type(e).__name__}: {str(e)[:90]}") from e

    def close(self):
        for reader in (self.primary, self.fallback):
            try:
                reader.close()
            except Exception:
                pass


class GmailMailbox(ImapMailbox):
    """Gmail over IMAP + Google OAuth2.

    Same XOAUTH2 handshake as the Microsoft side, different everything else:
    Google's token endpoint, a required client_secret, the mail.google.com
    scope, and imap.gmail.com. Gmail's spam folder is `[Gmail]/Spam` and is
    picked up by the same LIST-attribute discovery used for Outlook's Junk.
    """

    provider = "google"
    scope = GMAIL_SCOPE

    def __init__(self, cred, proxy_url="", timeout=NET_TIMEOUT):
        BaseMailbox.__init__(self, cred, proxy_url)
        self.client_secret = cred.get("client_secret", "")
        self.host = GMAIL_HOST
        self.port = 993
        self.timeout = timeout

    def access_token(self, force=False):
        with self._lock:
            if not force and self._access and time.time() < self._access_exp - 60:
                return self._access
            opener = self._opener()
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": GMAIL_SCOPE,
            }

            def attempt():
                return _open(urllib.request.Request(
                    GOOGLE_TOKEN_URL,
                    data=urllib.parse.urlencode(payload).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST"), opener=opener)

            try:
                data = _retry(attempt)
            except Exception:
                # some client types reject an explicit scope on refresh
                payload.pop("scope", None)
                data = _retry(attempt)
            self._access = data["access_token"]
            self._access_exp = time.time() + int(data.get("expires_in", 3600))
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
            return self._access


class GmailAliasMailbox(ImapMailbox):
    """An iCloud Hide-My-Email alias, read through the Gmail account behind it.

    An alias is not a mailbox. iCloud forwards it to whatever address the Apple
    ID really is, so the mail lands in a shared Gmail inbox - measured on the
    account this class was written against: 68k messages, 20k of them addressed
    to one iCloud alias or another.

    That single fact dictates the design. "Take the newest N in INBOX and grep
    the body" - what the Outlook reader can afford - would hand this alias
    somebody else's mail, because the newest N usually belong to a different
    alias. Every read is scoped on the server with `SEARCH TO <this alias>`,
    which Gmail answers from its index in milliseconds, and narrowed with SINCE
    once a `not_before` is known so polling only ever sees mail that is new.

    Auth is an app password over plain LOGIN, not XOAUTH2: this is a person's
    Gmail behind 2FA, and an app password is what Google issues for IMAP. The
    credential line carries it, so nothing here needs a Google OAuth client.

    Both of those were verified against the live inbox during development -
    `SEARCH TO <alias>` returned 36/22/39/23 messages for the four aliases in
    the pool, each addressed `To: Hide My Email <alias@icloud.com>`.
    """

    protocol = ALIAS_PROTOCOL

    # Gmail drops an idle session somewhere around half an hour. Ten minutes is
    # far inside that and still saves a login across every poll of the wait.
    _SESSION_TTL = 600
    _JUNK_EVERY = 15.0

    def __init__(self, cred, proxy_url="", timeout=NET_TIMEOUT):
        BaseMailbox.__init__(self, cred, proxy_url)
        self.inbox = cred.get("client_id") or cred.get("inbox") or ""
        self.password = cred.get("password", "")
        # No OAuth here at all; the base class only set it for the token flow.
        self.refresh_token = ""
        self.host = GMAIL_HOST
        self.port = 993
        self.timeout = timeout
        self._session = None
        self._session_at = 0.0
        # Junk is checked at most this often, never on every poll: a second
        # SELECT+SEARCH is ~0.5s and the poll loop runs every few seconds.
        self._junk_after = 0.0
        self._junk_folders = None

    def connect(self, attempts=NET_ATTEMPTS):
        """Authenticated IMAP session, by app password.

        A rejected password fails identically every time, so it raises
        MailAuthError rather than spending the retry budget on it - the operator
        needs to hear "wrong app password", not "mail fetch failed" three times.
        """
        last = None
        for i in range(attempts):
            M = None
            try:
                cls = type("BoundTunnelIMAP4", (TunnelIMAP4,), {
                    "proxy_url": self.proxy_url, "timeout": self.timeout})
                M = cls(self.host, self.port, timeout=self.timeout)
                M.login(self.inbox, self.password)
                return M
            except imaplib.IMAP4.error as e:
                self._logout(M)
                raise MailAuthError(
                    f"{self.inbox}: Gmail 拒绝登录（{str(e)[:90]}）——要用应用专用"
                    f"密码，并在 Gmail 设置里开启 IMAP") from e
            except Exception as e:
                last = e
                self._logout(M)
                time.sleep(1.5 * (i + 1))
        raise last

    @staticmethod
    def _logout(M):
        if M is None:
            return
        try:
            M.logout()
        except Exception:
            pass

    def _live(self):
        """A session that survives between polls.

        Logging in costs a TLS handshake and a SASL round trip through the proxy
        - measured 1.1-1.8s against 0.4s for the search itself. The poll loop
        runs every few seconds for the whole link_timeout, so reconnecting each
        time spends most of the wait on handshakes. One session is kept until
        the server drops it or it goes stale.
        """
        with self._lock:
            if self._session is not None and \
                    time.time() < self._session_at + self._SESSION_TTL:
                try:
                    typ, _ = self._session.noop()
                    if typ == "OK":
                        return self._session
                except Exception:
                    pass
            self._logout(self._session)
            self._session = self.connect()
            self._session_at = time.time()
            # The folder list belongs to the server, not the session, but it is
            # only worth asking once per reconnect either way.
            self._junk_folders = None
            return self._session

    def close(self):
        """Drop the held session. Called when the account is finished with."""
        with self._lock:
            self._logout(self._session)
            self._session = None
            self._junk_folders = None

    def _junk(self, M):
        """Folders flagged as junk, discovered once per session."""
        if self._junk_folders is None:
            self._junk_folders = _candidate_folders(M)[1:]
        return self._junk_folders

    def _search_args(self, not_before=None):
        args = ["TO", self.email]
        if not_before:
            # SINCE has day granularity and compares the time the server took
            # the message, so widen by a day and let the exact timestamp decide.
            # It is also cheap: measured 0.4s against 0.9s for an unbounded
            # SEARCH, because the server can cut straight to a date range.
            args += ["SINCE", time.strftime(
                "%d-%b-%Y", time.gmtime(not_before - 86400))]
        return args

    def _fetch_folder(self, M, folder, limit, not_before):
        try:
            typ, _ = M.select(folder, readonly=True)
            if typ != "OK":
                return []
            typ, hit = M.search(None, *self._search_args(not_before))
            if typ != "OK" or not hit or not hit[0]:
                return []
        except Exception:
            return []
        out = []
        # Gmail returns ids in ascending arrival order, so the tail is the
        # newest mail for this alias.
        for i in reversed(hit[0].split()[-limit:]):
            try:
                typ, raw = M.fetch(i, "(RFC822)")
            except Exception:
                continue
            if not raw or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1])
            # _message_mail returns whichever MIME part holds the body, which
            # matters here: measured, the site's mail is multipart/mixed with a
            # text/html part and no text/plain part at all, so a reader that
            # insists on plain text sees nothing.
            out.append(_message_mail(msg, folder))
        return out

    def messages(self, limit=12, not_before=None):
        """Newest-first page of Mail, for this alias only."""
        M = self._live()
        out = self._fetch_folder(M, "INBOX", limit, not_before)
        # Junk is checked on a timer, not "only when the inbox was empty": these
        # aliases receive plenty of unrelated mail - the inbox of one here holds
        # 36 messages, including an LA28 draw confirmation - so an empty inbox
        # is not the signal. Missing the one mail that matters because the
        # provider filed it as spam costs a whole account, and the check is one
        # SELECT+SEARCH on a folder list that is already cached.
        if time.time() >= self._junk_after:
            self._junk_after = time.time() + self._JUNK_EVERY
            for folder in self._junk(M):
                out += self._fetch_folder(M, folder, limit, not_before)
        out.sort(key=lambda m: m.stamp or 0, reverse=True)
        return out[:limit]


def hme_catalog(base="", password="", timeout=NET_TIMEOUT):
    """Every alias the iCloud Hide-My-Email service knows about.

    Returns [{"email", "account_id", "account_name", "label", "active"}] so a
    caller can let the operator pick which ones to import - iCloud caps alias
    creation (roughly 10/hour), so importing the whole list blindly wastes the
    ones that are already spent.

    Raises MailAuthError when the service is unreachable or the password is
    wrong, with a message that says which of the two it was.
    """
    base = (base or os.environ.get("ICLOUD_HME_BASE")
            or HME_BASE_DEFAULT).rstrip("/")
    password = password or os.environ.get("ICLOUD_HME_ADMIN_PASSWORD") or ""
    if not password:
        raise MailAuthError("iCloud 服务密码未配置（设置页的 iCloud 密码，"
                            "或环境变量 ICLOUD_HME_ADMIN_PASSWORD）")

    opener = urllib.request.build_opener(
        # never dial the local service through ALL_PROXY - it answers 400
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data,
                                     method="POST" if data else "GET")
        req.add_header("accept", "application/json")
        if data:
            req.add_header("content-type", "application/json")
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if e.code in (401, 403):
                raise MailAuthError(f"iCloud 服务拒绝了密码（HTTP {e.code}）")
            raise MailAuthError(f"iCloud 服务返回 HTTP {e.code}: {raw[:120]}")
        except Exception as e:
            raise MailAuthError(
                f"连不上 iCloud 服务 {base}（{type(e).__name__}）——"
                f"容器里要用 host.docker.internal，不是 127.0.0.1")

    status, _ = _hme_raw(opener, base, "/api/auth/login",
                         {"password": password}, timeout)
    if status != 200:
        raise MailAuthError(f"iCloud 服务登录失败（HTTP {status}）")

    out = []
    for acct in (call("/api/accounts").get("data") or []):
        aid = acct.get("id") or ""
        try:
            aliases = call(f"/api/aliases?account_id={urllib.parse.quote(aid)}")
        except MailAuthError:
            continue
        for a in ((aliases.get("data") or {}).get("aliases") or []):
            out.append({"email": a.get("email") or "",
                        "account_id": aid,
                        "account_name": acct.get("name") or "",
                        "label": a.get("label") or "",
                        "active": bool(a.get("active"))})
    return out


def _hme_raw(opener, base, path, body, timeout):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("content-type", "application/json")
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        raise MailAuthError(
            f"连不上 iCloud 服务 {base}（{type(e).__name__}）——"
            f"容器里要用 host.docker.internal，不是 127.0.0.1")


def is_hme(email):
    return (email or "").rsplit("@", 1)[-1].lower() in HME_ICLOUD_DOMAINS


def make_mailbox(cred, protocol="graph", proxy_url=""):
    """Build the reader for one credential.

    Gmail is detected from the credential itself and always uses the Gmail
    reader - it has no Graph channel, so the batch's protocol choice does not
    apply to it.

    proxy_url defaults to empty (direct dial): mail does not need to share the
    registration traffic's exit.
    """
    if cred.get("provider") == "alias" or protocol == ALIAS_PROTOCOL:
        # Before the HmeMailbox test on purpose: these addresses are @icloud.com
        # too, and is_hme() would otherwise route them to the Apple service.
        return GmailAliasMailbox(cred, proxy_url=proxy_url)
    if cred.get("provider") == "hme" or is_hme(cred.get("email", "")):
        return HmeMailbox(cred, proxy_url=proxy_url)
    if cred.get("provider") == "google" or is_gmail(cred.get("email", "")):
        return GmailMailbox(cred, proxy_url=proxy_url)
    protocol = (protocol or "graph").lower()
    if protocol == "imap":
        return ImapMailbox(cred, proxy_url=proxy_url)
    if protocol == "auto":
        return AutoMailbox(cred, proxy_url)
    return GraphMailbox(cred, proxy_url)


# -------------------------------------------------------------------------- helpers
def _decode_header(value):
    """Decode a MIME-encoded header into readable text.

    The site's subject arrives as `=?UTF-8?Q?Oasis_Live_=E2=80=9927_...?=`,
    where spaces are underscores - a rule that greps the raw header never
    matches and reports every success as a miss. Decoding is done once, here, so
    nothing downstream has to know about it.
    """
    if not value:
        return ""
    try:
        return str(email.header.make_header(
            email.header.decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _message_body(msg):
    """Every text part of a message, flattened and decoded.

    Text/plain and text/html are both kept, not just the first match: measured,
    the site's mail is multipart with an HTML-only second half, and the HME
    service puts the actionable part in `body_html` alone. Joining them means a
    keyword written into either half is still found.
    """
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    chunks = []
    for want in ("text/plain", "text/html"):
        for part in parts:
            if part.get_content_type() != want:
                continue
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8",
                                  errors="ignore")
            chunks.append(re.sub(r"=\r?\n", "", text).replace("&amp;", "&"))
    if not chunks:
        # No declared text part at all: hand back whatever text exists rather
        # than an empty body that reads as "nothing arrived".
        for part in parts:
            if part.get_content_maintype() != "text":
                continue
            try:
                chunks.append((part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="ignore"))
            except Exception:
                continue
    return "\n".join(chunks)


def _message_stamp(msg):
    try:
        return parsedate_to_datetime(msg.get("Date")).astimezone(
            timezone.utc).timestamp()
    except Exception:
        return None


def _message_mail(msg, folder=""):
    return Mail(subject=_decode_header(msg.get("Subject")),
                sender=_decode_header(msg.get("From")),
                body=_message_body(msg),
                stamp=_message_stamp(msg) or 0.0,
                folder=folder)


def _graph_mail(m):
    frm = ((m.get("from") or {}).get("emailAddress") or {})
    return Mail(subject=_decode_header(m.get("subject")),
                sender=f"{frm.get('name') or ''} "
                       f"{frm.get('address') or ''}".strip(),
                body=(m.get("body") or {}).get("content") or "",
                stamp=_iso_to_epoch(m.get("receivedDateTime")) or 0.0,
                folder="inbox")


def _hme_sender(m):
    for key in ("from", "sender", "from_email", "fromAddress"):
        value = m.get(key)
        if isinstance(value, dict):
            value = (value.get("address") or value.get("email")
                     or value.get("name"))
        if value:
            return _decode_header(str(value))
    return ""


def check_credentials(line, protocol="graph", proxy_url=""):
    """Quick validity probe used by the import screen."""
    protocol = (protocol or "graph").lower()
    if protocol == "auto":
        try:
            return check_credentials(line, "graph", proxy_url)
        except Exception:
            return check_credentials(line, "imap", proxy_url)
    mb = make_mailbox(parse_cred(line), protocol, proxy_url)
    if isinstance(mb, ImapMailbox):
        M = mb.connect()
        try:
            typ, boxes = M.list()
            return f"IMAP ok, {len(boxes)} folders", mb.email
        finally:
            try:
                M.logout()
            except Exception:
                pass
    token = mb.access_token()
    opener = mb._opener()
    data = _retry(lambda: _open(urllib.request.Request(
        GRAPH + "/me?$select=displayName,userPrincipalName",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json"}), opener=opener))
    return data.get("displayName", ""), data.get("userPrincipalName", "")


# backwards-compatible alias
Mailbox = GraphMailbox


if __name__ == "__main__":
    import sys
    from hitcheck import classify
    line = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    proto = sys.argv[2] if len(sys.argv) > 2 else "graph"
    mb = make_mailbox(parse_cred(line), proto)
    print("protocol:", mb.protocol, "| email:", mb.email)
    for m in mb.messages(limit=15):
        kind = classify(m) or "-"
        when = time.strftime("%m-%d %H:%M", time.localtime(m.stamp)) if m.stamp else "?"
        print(f"[{kind:7}] {when} | {m.subject[:60]} | {m.sender[:40]}")
