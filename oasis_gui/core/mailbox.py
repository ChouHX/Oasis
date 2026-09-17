#!/usr/bin/env python3
"""Mailbox readers: Microsoft Graph and IMAP, both driven by one credential line.

Credential line: email----password----client_id----refresh_token

The fourth field is a Microsoft refresh token. It can be redeemed for either
audience, so the same line works for both protocols:

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

TOKEN_RE = re.compile(r"https://oasis\.hq\.fan/registration\?token=[A-Za-z0-9._\-]+")

# iCloud Hide-My-Email via a local HME service. Reads need no CSRF, only the
# session cookie, and one login covers the whole session TTL.
HME_BASE_DEFAULT = "http://127.0.0.1:8081"
HME_ICLOUD_DOMAINS = ("icloud.com", "me.com", "mac.com")

# Body text of the "Oasis Live '27 Registration Complete" mail. Verified to
# appear in 4/4 success mails and 0/15 verification mails.
SUCCESS_MARK = "successfully registered"

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


class MailAuthError(Exception):
    """The mailbox rejected the token. Retrying will not help.

    Seen in practice: a client_id can redeem `graph.microsoft.com/.default`
    successfully and then have Graph answer 401 UnknownError for every call -
    that registration simply has no Graph mail permission, and the account has
    to be read over IMAP instead.
    """


def _retry(fn, attempts=4, delay=1.2):
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


def _open(req, timeout=45, opener=None):
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


def open_tunnel(host, port, timeout=30, proxy_url=""):
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

    def find_verification_link(self, timeout=300, interval=6, not_before=None,
                               log=None):
        raise NotImplementedError

    def _candidates(self, not_before=None):
        """(body, stamp) pairs, whatever shape this backend's messages() has."""
        raise NotImplementedError

    def _recent(self, stamp, not_before):
        """Is this message new enough? Graph stamps are ISO strings, IMAP floats,
        so the comparison cannot live in shared code."""
        raise NotImplementedError

    def find_success(self, timeout=180, interval=8, not_before=None, log=None):
        """Wait for the "Registration Complete" mail. Returns (found, when).

        Match on the body, never the subject: the subject arrives MIME-encoded

            =?UTF-8?Q?Oasis_Live_=E2=80=9927_Registration_Complete?=

        where the spaces are underscores, so a naive `"Registration Complete"
        in subject` test against the raw header never matches and reports every
        successful registration as a failure. The body of that mail says
        "successfully registered", which appears in no verification mail at all
        (checked across 15 verification and 4 success mails).
        """
        started = time.time()
        deadline = started + timeout
        polls = 0
        while time.time() < deadline:
            polls += 1
            try:
                cands = self._candidates(not_before)
            except MailAuthError:
                raise
            except Exception as e:
                if log:
                    log(f"    success-mail fetch failed: {type(e).__name__}")
                time.sleep(interval)
                continue
            for body, stamp in cands:
                if SUCCESS_MARK not in body.lower():
                    continue
                if not self._recent(stamp, not_before):
                    continue
                return True, stamp
            if log and polls % 4 == 0:
                log(f"    waiting for success mail: {int(time.time() - started)}s/"
                    f"{int(timeout)}s")
            time.sleep(interval)
        if log:
            log(f"    no success mail within {int(timeout)}s - confirm answered OK "
                f"but the registration did not complete")
        return False, None


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

    def messages(self, top=15):
        """Inbox **and** Junk.

        Graph's /me/messages returns the Inbox only - measured on a real
        mailbox: 13 messages, all Inbox, with the Junk folder invisible even
        though it held mail. A verification mail the server filed as junk is
        therefore never seen and the caller waits out the whole timeout. The
        junk folder is queried separately (well-known name `junkemail`).
        """
        msgs = self._folder_messages("/me/messages", top)
        try:
            msgs += self._folder_messages("/me/mailFolders/junkemail/messages", top)
        except Exception:
            pass                      # folder missing or not exposed by the API
        seen, unique = set(), []
        for m in msgs:
            key = m.get("id") or m.get("internetMessageId") or id(m)
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        return {"value": unique}

    def _candidates(self, not_before):
        out = []
        for m in self.messages().get("value", []):
            body = (m.get("body") or {}).get("content", "") or ""
            out.append((body, m.get("receivedDateTime")))
        return out

    @staticmethod
    def _recent(stamp, not_before):
        return _recent_enough_iso(stamp, not_before)

    def find_verification_link(self, timeout=300, interval=6, not_before=None,
                               log=None):
        started = time.time()
        deadline = started + timeout
        last_error = None
        polls = 0
        while time.time() < deadline:
            polls += 1
            try:
                cands = self._candidates(not_before)
            except MailAuthError:
                raise                       # no point polling a dead credential
            except Exception as e:
                # Never swallow this: "mail never arrived" is a lie when the
                # real cause is an unreachable mailbox.
                last_error = e
                if log:
                    log(f"    mail fetch failed: {type(e).__name__}: {str(e)[:110]}")
                time.sleep(interval)
                continue
            for body, stamp in cands:
                hit = TOKEN_RE.search(re.sub(r"=\r?\n", "", body).replace("&amp;", "&"))
                if hit and _recent_enough_iso(stamp, not_before):
                    return hit.group(0), stamp
            if log and (polls == 1 or polls % 5 == 0):
                log(f"    waiting for mail: {int(time.time() - started)}s/"
                    f"{int(timeout)}s, {len(cands)} message(s) visible")
            time.sleep(interval)
        if log:
            tail = (f"; last error {type(last_error).__name__}: {str(last_error)[:90]}"
                    if last_error else "")
            log(f"    no verification mail within {int(timeout)}s "
                f"({polls} polls){tail}")
        return None, None


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
                 timeout=30):
        super().__init__(cred, proxy_url)
        self.host = host
        self.port = port
        self.timeout = timeout

    def connect(self, attempts=4):
        """Authenticated IMAP session.

        Outlook intermittently answers XOAUTH2 with "User is authenticated but
        not connected" even for a perfectly good token, so the handshake is
        retried with a fresh access token.
        """
        last = None
        for i in range(attempts):
            try:
                cls = type("BoundTunnelIMAP4", (TunnelIMAP4,), {
                    "proxy_url": self.proxy_url, "timeout": self.timeout})
                M = cls(self.host, self.port, timeout=self.timeout)
                auth = (f"user={self.email}\x01auth=Bearer "
                        f"{self.access_token(force=i > 0)}\x01\x01")
                M.authenticate("XOAUTH2", lambda _: auth.encode())
                return M
            except Exception as e:
                last = e
                try:
                    M.logout()
                except Exception:
                    pass
                time.sleep(2.0 * (i + 1))
        raise last

    def messages(self, limit=12, not_before=None):
        """Newest-first (body, received datetime) pairs, INBOX + Junk.

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
                    msg = email.message_from_bytes(raw[0][1])
                    out.append((_message_text(msg), _message_stamp(msg)))
            out.sort(key=lambda pair: pair[1] or 0, reverse=True)
            return out[:limit]
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def _candidates(self, not_before=None):
        """(body, stamp) pairs. IMAP already returns that shape; Graph wraps
        its payload in a dict, so both backends expose this instead."""
        return list(self.messages(not_before=not_before))

    @staticmethod
    def _recent(stamp, not_before):
        if not not_before or stamp is None:
            return True
        try:
            return float(stamp) >= not_before - 10
        except (TypeError, ValueError):
            return True

    def find_verification_link(self, timeout=300, interval=6, not_before=None,
                               log=None):
        started = time.time()
        deadline = started + timeout
        last_error = None
        polls = 0
        while time.time() < deadline:
            polls += 1
            try:
                cands = self._candidates(not_before)
            except MailAuthError:
                raise
            except Exception as e:
                last_error = e
                if log:
                    log(f"    mail fetch failed: {type(e).__name__}: {str(e)[:110]}")
                time.sleep(interval)
                continue
            for body, stamp in cands:
                hit = TOKEN_RE.search(body)
                if hit and (not not_before or stamp is None or stamp >= not_before - 10):
                    return hit.group(0), (time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)) if stamp else "")
            if log and (polls == 1 or polls % 5 == 0):
                log(f"    waiting for mail: {int(time.time() - started)}s/"
                    f"{int(timeout)}s, {len(cands)} message(s) visible")
            time.sleep(interval)
        if log:
            tail = (f"; last error {type(last_error).__name__}: {str(last_error)[:90]}"
                    if last_error else "")
            log(f"    no verification mail within {int(timeout)}s "
                f"({polls} polls){tail}")
        return None, None


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

    def __init__(self, cred, proxy_url="", timeout=30):
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
    def messages(self, limit=12):
        """Newest messages for this alias, link-bearing HTML flattened in.

        `preview` is returned by the list endpoint but omits the button URL, so
        each message's detail is fetched to recover `body_html`. Those are
        local calls - a handful per poll is cheaper than a miss.
        """
        out = []
        listing = self._call(f"/api/inbox?account_id={urllib.parse.quote(self.account)}"
                             f"&alias={urllib.parse.quote(self.email)}"
                             f"&limit={max(1, min(int(limit), 100))}&days=2")
        for m in (listing.get("messages") or [])[:limit]:
            mid = m.get("id")
            if mid is None:
                continue
            text = f"{m.get('subject') or ''}\n{m.get('preview') or ''}"
            try:
                detail = self._call(f"/api/inbox/{urllib.parse.quote(str(mid))}"
                                    f"?account_id={urllib.parse.quote(self.account)}")
                text += "\n" + (detail.get("body_html") or detail.get("body") or "")
            except Exception:
                pass                            # keep the preview-only entry
            out.append((text, _iso_to_epoch(m.get("date"))))
        return out

    def _candidates(self, not_before=None):
        return self.messages()

    @staticmethod
    def _recent(stamp, not_before):
        if not not_before or stamp is None:
            return True
        try:
            return float(stamp) >= not_before - 10
        except (TypeError, ValueError):
            return True

    def find_verification_link(self, timeout=300, interval=6, not_before=None,
                               log=None):
        """Same polling shape as the other readers, over the HME listing.

        Not inherited: BaseMailbox deliberately leaves this abstract, and the
        Graph/IMAP versions carry their own body-massaging that does not apply
        here (the link comes out of `body_html` already unescaped).
        """
        started = time.time()
        deadline = started + timeout
        last_error = None
        polls = 0
        while time.time() < deadline:
            polls += 1
            try:
                cands = self.messages()
            except MailAuthError:
                raise
            except Exception as e:
                last_error = e
                if log:
                    log(f"    mail fetch failed: {type(e).__name__}: {str(e)[:110]}")
                time.sleep(interval)
                continue
            for body, stamp in cands:
                hit = TOKEN_RE.search(body)
                if hit and self._recent(stamp, not_before):
                    return hit.group(0), (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
                        if stamp else "")
            if log and (polls == 1 or polls % 5 == 0):
                log(f"    waiting for mail: {int(time.time() - started)}s/"
                    f"{int(timeout)}s, {len(cands)} message(s) visible")
            time.sleep(interval)
        if log:
            tail = (f"; last error {type(last_error).__name__}: {str(last_error)[:90]}"
                    if last_error else "")
            log(f"    no verification mail within {int(timeout)}s ({polls} polls){tail}")
        return None, None


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

    def find_verification_link(self, timeout=300, interval=6, not_before=None,
                               log=None):
        started = time.time()
        half = max(30, timeout // 2)
        try:
            url, when = self.primary.find_verification_link(
                half, interval, not_before, log)
            if url:
                self._current = self.primary
                return url, when
        except MailAuthError as e:
            # Graph refused the token outright - switch now instead of burning
            # the rest of the budget polling a channel that cannot work.
            if log:
                log(f"    graph unavailable ({str(e)[:70]}), switching to IMAP")
        except Exception:
            pass
        # The primary may have failed in a second (a 401) or used its whole
        # half; either way the fallback gets whatever time is actually left.
        remaining = max(30, timeout - (time.time() - started))
        if log:
            log(f"    IMAP gets {int(remaining)}s of the budget")
        self.fallback.refresh_token = (
            self.primary.refresh_token or self.fallback.refresh_token)
        self._current = self.fallback
        return self.fallback.find_verification_link(
            remaining, interval, not_before, log)

    def find_success(self, timeout=180, interval=8, not_before=None, log=None):
        """Ask whichever reader already proved it can see this mailbox."""
        return self._current.find_success(timeout, interval, not_before, log)


class GmailMailbox(ImapMailbox):
    """Gmail over IMAP + Google OAuth2.

    Same XOAUTH2 handshake as the Microsoft side, different everything else:
    Google's token endpoint, a required client_secret, the mail.google.com
    scope, and imap.gmail.com. Gmail's spam folder is `[Gmail]/Spam` and is
    picked up by the same LIST-attribute discovery used for Outlook's Junk.
    """

    provider = "google"
    scope = GMAIL_SCOPE

    def __init__(self, cred, proxy_url="", timeout=30):
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

    def __init__(self, cred, proxy_url="", timeout=30):
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
        # Junk is checked at most this often, and only when the inbox was empty.
        # A LIST plus a second SELECT+SEARCH is ~1.5s, and the poll loop runs
        # every few seconds - doing it on every poll doubles the cost of the
        # common case (mail not there yet) to catch a case that did not happen
        # once across the aliases measured.
        self._junk_after = 0.0
        self._junk_folders = None

    def connect(self, attempts=3):
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
            # _message_text already returns whichever MIME part holds the token,
            # which matters here: measured, the Oasis verification mail is
            # multipart/mixed with a text/html part and no text/plain part at
            # all, so a reader that insists on plain text sees nothing.
            out.append((_message_text(msg), _message_stamp(msg)))
        return out

    def messages(self, limit=12, not_before=None):
        """Newest-first (body, stamp) pairs, for this alias only."""
        M = self._live()
        out = self._fetch_folder(M, "INBOX", limit, not_before)
        if not out and time.time() >= self._junk_after:
            self._junk_after = time.time() + self._JUNK_EVERY
            for folder in self._junk(M):
                out = self._fetch_folder(M, folder, limit, not_before)
                if out:
                    break
        out.sort(key=lambda pair: pair[1] or 0, reverse=True)
        return out[:limit]


def hme_catalog(base="", password="", timeout=30):
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
def _message_text(msg):
    """Prefer text/plain, fall back to text/html, decode defensively.

    walk() is a generator: it has to be materialised first, otherwise the
    text/plain pass exhausts it and every multipart mail decodes to "".
    """
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
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
            text = re.sub(r"=\r?\n", "", text).replace("&amp;", "&")
            if TOKEN_RE.search(text):
                return text
    # nothing matched: hand back whatever text exists
    for part in parts:
        if part.get_content_maintype() == "text":
            try:
                return (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                continue
    return ""


def _message_stamp(msg):
    try:
        return parsedate_to_datetime(msg.get("Date")).astimezone(
            timezone.utc).timestamp()
    except Exception:
        return None


def _recent_enough_iso(stamp, not_before):
    if not not_before or not stamp:
        return True
    try:
        from datetime import datetime
        dt = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
        return dt.timestamp() >= not_before - 10
    except Exception:
        return True


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
    line = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    proto = sys.argv[2] if len(sys.argv) > 2 else "graph"
    mb = make_mailbox(parse_cred(line), proto)
    print("protocol:", mb.protocol, "| email:", mb.email)
    url, when = mb.find_verification_link(timeout=20, interval=4)
    print("link:", (url or "-")[:90], when)
