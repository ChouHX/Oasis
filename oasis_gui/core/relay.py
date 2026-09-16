#!/usr/bin/env python3
"""Local HTTP relay, programmatically startable, one per upstream proxy.

Why a relay at all
------------------
Chromium cannot be handed proxy credentials for a persistent context reliably:
on a 407 it marks that proxy bad and can fall back to DIRECT, which leaks the
host egress. So the browser talks unauthenticated HTTP to us, and we re-issue
the request upstream with Proxy-Authorization attached. Playwright *can* take
proxy credentials, but the same DIRECT-fallback risk applies, and the split
routing below still needs a hop we control.

Why split routing
-----------------
The residential upstream refuses the entire Google estate outright (measured:
instant ECONNRESET on www.google.com, www.gstatic.com, www.recaptcha.net), and
reCAPTCHA Enterprise cannot initialise without it. Google hosts are therefore
routed through a second proxy, so the registration traffic itself still
egresses from the residential IP while the page can load its captcha script.

Measured upstream behaviour
---------------------------
* a fresh residential egress per tunnel (~1.5-2 s to first byte)
* it resets connections under bursts, so tunnels are paced and retried with
  exponential backoff
* on final failure the client connection is HUNG rather than answered with 502:
  an error response is exactly what makes Chromium mark the proxy dead and fall
  back to DIRECT, and that leak is worse than a timeout.
"""
import base64
import socket
import struct
import threading
import time
import urllib.parse

# Browser chatter that would otherwise burn upstream tunnels.
DEFAULT_BLOCKED = (
    "update.googleapis.com",
    "edgedl.me.gvt1.com",
    "optimizationguide-pa.googleapis.com",
    "safebrowsing.googleapis.com",
    "clientservices.googleapis.com",
    "content-autofill.googleapis.com",
)

GOOGLE_SUFFIXES = (
    "google.com", "gstatic.com", "googleapis.com", "recaptcha.net",
    "googleusercontent.com", "gvt1.com", "googlevideo.com", "withgoogle.com",
)


def parse_proxy_url(url):
    """http://user:pass@host:port -> (host, port, basic_auth_or_None)."""
    u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    auth = None
    if u.username:
        auth = base64.b64encode(
            f"{u.username}:{u.password or ''}".encode()).decode()
    return u.hostname, (u.port or 8080), auth


def parse_chain(url):
    """Split `front|upstream` into (front_url_or_None, upstream_url).

    A short-lived residential endpoint is often unreachable directly from where
    the console runs (China blocks many of them outright), but reachable from a
    local proxy. Chaining lets the relay dial the upstream *through* that local
    proxy:

        socks5://127.0.0.1:10808|http://user:pass@198.44.166.252:566

    The first hop is the front, the second the real egress.
    """
    if "|" not in url:
        return None, url
    front, _, upstream = url.partition("|")
    return (front.strip() or None), (upstream.strip() or url)


def is_google(host):
    h = host.lower()
    return any(h == s or h.endswith("." + s) for s in GOOGLE_SUFFIXES)


class Relay:
    """One relay bound to 127.0.0.1:<ephemeral>, forwarding to one upstream."""

    def __init__(self, upstream_url, google_fallback="",
                 blocked=DEFAULT_BLOCKED, max_concurrent=32, min_spacing=0.05,
                 attempt_timeout=6.0, connect_budget=45.0, hang_on_failure=15.0,
                 log=None):
        front_url, upstream_url = parse_chain(upstream_url)
        self.up_host, self.up_port, self.up_auth = parse_proxy_url(upstream_url)
        self.up_scheme = ("socks5" if upstream_url.lower().startswith("socks")
                          else "http")
        self.upstream_url = upstream_url
        # When set, the upstream proxy itself is only reachable through this
        # local proxy (see parse_chain).
        self.front = None
        if front_url:
            fh, fp, fa = parse_proxy_url(front_url)
            scheme = "socks5" if front_url.lower().startswith("socks") else "http"
            if fh:
                self.front = (fh, fp, fa, scheme, front_url)
        # Empty means "no split": Google goes out the same way as everything
        # else. Only set this when the upstream cannot reach Google at all.
        self.fallback = None
        if google_fallback:
            fb_host, fb_port, _ = parse_proxy_url(google_fallback)
            self.fallback = (fb_host, fb_port) if fb_host else None
        self.blocked = tuple(blocked)
        self.attempt_timeout = attempt_timeout
        self.connect_budget = connect_budget
        self.hang_on_failure = hang_on_failure
        self.log = log or (lambda m: None)

        self._sem = threading.Semaphore(max_concurrent)
        self._pace_lock = threading.Lock()
        self._last = [0.0]
        self._min_spacing = min_spacing

        # circuit breaker: a dead fallback must not spam the log forever
        self._fb_fail = 0
        self._fb_dead = False
        self._fb_lock = threading.Lock()

        self._srv = None
        self._thread = None
        self.port = None
        self._stop = threading.Event()
        self._err_lock = threading.Lock()
        self._err_count = 0

    # ------------------------------------------------------------------ lifecycle
    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}" if self.port else None

    def start(self):
        if self._srv:
            return self.url
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(256)
        self.port = srv.getsockname()[1]
        self._srv = srv
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name=f"relay-{self.port}")
        self._thread.start()
        route = (f"google -> {self.fallback[0]}:{self.fallback[1]}"
                 if self.fallback else "no google split")
        self.log(f"relay {self.url} -> {self.up_host}:{self.up_port} ({route})")
        return self.url

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None

    def _serve(self):
        while not self._stop.is_set():
            try:
                cl, peer = self._srv.accept()
            except Exception:
                return
            threading.Thread(target=self._handle, args=(cl, peer),
                             daemon=True).start()

    # -------------------------------------------------------------------- helpers
    def _pace(self):
        with self._pace_lock:
            wait = self._min_spacing - (time.time() - self._last[0])
            if wait > 0:
                time.sleep(wait)
            self._last[0] = time.time()

    def _blocked(self, host):
        h = host.lower()
        return any(h == b or h.endswith("." + b) for b in self.blocked)

    def _note_fallback(self, ok):
        """Give up on a fallback that keeps refusing, instead of retrying it
        on every Google host forever."""
        with self._fb_lock:
            if ok:
                self._fb_fail = 0
                return
            self._fb_fail += 1
            if self._fb_fail >= 3 and not self._fb_dead:
                self._fb_dead = True
                self.log(f"  google fallback {self.fallback[0]}:{self.fallback[1]} "
                         f"unreachable - disabling the split, google will use "
                         f"{self.up_host}:{self.up_port} like everything else")

    def _dial(self, host, port, timeout, use_front=True):
        """TCP socket to host:port, tunnelled through the front proxy if chained."""
        if not (self.front and use_front):
            return socket.create_connection((host, port), timeout=timeout)
        fh, fp, fa, scheme, _url = self.front
        if scheme == "socks5":
            import socks
            user = password = None
            if fa:
                cred = base64.b64decode(fa).decode(errors="replace")
                user, _, password = cred.partition(":")
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, fh, fp,
                        username=user or None, password=password or None)
            s.settimeout(timeout)
            s.connect((host, port))
            return s
        # http front: reach the upstream with a CONNECT of our own
        s = socket.create_connection((fh, fp), timeout=timeout)
        try:
            s.settimeout(timeout)
            auth_hdr = f"Proxy-Authorization: Basic {fa}\r\n" if fa else ""
            s.sendall((f"CONNECT {host}:{port} HTTP/1.1\r\n"
                       f"Host: {host}:{port}\r\n{auth_hdr}"
                       f"Proxy-Connection: keep-alive\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                chunk = s.recv(4096)
                if not chunk:
                    raise IOError("front proxy closed during CONNECT")
                buf += chunk
            status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 200" not in status:
                raise IOError(f"front proxy CONNECT rejected: {status}")
            s.settimeout(None)
            return s
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            raise

    def _route(self, host):
        """(host, port, auth, tag, use_front) for this request."""
        if self.fallback and not self._fb_dead and is_google(host):
            # the fallback is the local proxy itself - never chain it back
            return (*self.fallback, None, "google-fallback", False)
        return self.up_host, self.up_port, self.up_auth, "upstream", True

    @staticmethod
    def _socks5_handshake(s, host, port, auth):
        """Talk SOCKS5 to the upstream and ask it for host:port.

        Short-lived residential endpoints are usually SOCKS5, not HTTP: a plain
        CONNECT just hangs until the socket times out, which looks exactly like
        a dead proxy.
        """
        user = password = None
        if auth:
            cred = base64.b64decode(auth).decode(errors="replace")
            user, _, password = cred.partition(":")
        s.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        resp = s.recv(2)
        if len(resp) < 2 or resp[0] != 5:
            raise IOError("socks5 upstream: bad greeting")
        method = resp[1]
        if method == 2:
            if not user:
                raise IOError("socks5 upstream: wants auth but none configured")
            u, p = user.encode(), (password or "").encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            ar = s.recv(2)
            if len(ar) < 2 or ar[1] != 0:
                raise IOError("socks5 upstream: auth rejected")
        elif method != 0:
            raise IOError(f"socks5 upstream: unsupported auth method {method}")
        hb = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
        rep = s.recv(4)
        if len(rep) < 4 or rep[1] != 0:
            code = rep[1] if len(rep) > 1 else "?"
            raise IOError(f"socks5 upstream: CONNECT failed (code {code})")
        atyp = rep[3]
        if atyp == 1:
            s.recv(4 + 2)
        elif atyp == 3:
            n = s.recv(1)
            s.recv((n[0] if n else 0) + 2)
        elif atyp == 4:
            s.recv(16 + 2)

    def _http_connect(self, s, host, port, auth):
        auth_hdr = f"Proxy-Authorization: Basic {auth}\r\n" if auth else ""
        s.sendall((f"CONNECT {host}:{port} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n{auth_hdr}"
                   f"Proxy-Connection: keep-alive\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = s.recv(4096)
            if not chunk:
                raise IOError("upstream closed during CONNECT")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200" not in status:
            raise IOError(f"upstream CONNECT rejected: {status}")

    def upstream_connect(self, host, port):
        """Open one tunnel to host:port, on the egress that can reach it."""
        up_host, up_port, auth, route, use_front = self._route(host)
        # the fallback is always a plain local HTTP proxy
        scheme = self.up_scheme if route == "upstream" else "http"
        # Google through an upstream that blocks it will never succeed, so do
        # not spend the whole connect budget proving it 18 times.
        budget = self.connect_budget
        if is_google(host) and not self.fallback:
            budget = min(budget, 8.0)
        deadline = time.time() + budget
        attempt, last = 0, None
        while time.time() < deadline:
            attempt += 1
            self._pace()
            s = None
            try:
                s = self._dial(up_host, up_port, self.attempt_timeout, use_front)
                s.settimeout(self.attempt_timeout)
                if scheme == "socks5":
                    self._socks5_handshake(s, host, port, auth)
                else:
                    self._http_connect(s, host, port, auth)
                s.settimeout(None)
                if route == "google-fallback":
                    self._note_fallback(True)
                if attempt > 1:
                    self.log(f"  tunnel {host}:{port} ok on attempt {attempt} ({route})")
                return s
            except Exception as e:
                last = e
                if route == "google-fallback":
                    self._note_fallback(False)
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
                time.sleep(min(0.3 * (2 ** min(attempt - 1, 4)), 3.0))
        hint = ""
        if is_google(host) and not self.fallback:
            # Google is not reachable from most residential upstreams. Without
            # a fallback the relay keeps the traffic on the registration exit,
            # where it is reset - which looks like a dead proxy but is really a
            # missing setting.
            hint = (" - this host is Google, and no Google split is configured; "
                    "set the Google proxy setting to an egress that can reach it "
                    "(reCAPTCHA cannot load without one)")
        raise IOError(f"upstream unavailable after {attempt} attempts "
                      f"({route}): {last!r}{hint}")

    def upstream_request(self, lines, rest):
        """Forward a non-CONNECT proxied request, injecting Proxy-Authorization."""
        host = ""
        for ln in lines[1:]:
            if ln.lower().startswith("host:"):
                host = ln.split(":", 1)[1].strip()
                break
        if not host:
            raise IOError("no Host header")
        if self._blocked(host.split(":")[0]):
            raise IOError(f"blocked host {host}")
        h, _, p = host.partition(":")
        s = self.upstream_connect(h, int(p or 80))
        out = [lines[0]]
        out += [ln for ln in lines[1:]
                if not ln.lower().startswith(("proxy-authorization", "proxy-connection"))]
        if self.up_auth:
            out.append(f"Proxy-Authorization: Basic {self.up_auth}")
        out.append("Proxy-Connection: keep-alive")
        s.sendall(("\r\n".join(out) + "\r\n\r\n").encode() + rest)
        return s

    # ---------------------------------------------------------------------- pump
    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def _splice(self, client, upstream, tag):
        t = threading.Thread(target=self._pump, args=(upstream, client), daemon=True)
        t.start()
        self._pump(client, upstream)
        t.join(timeout=2)

    def _handle(self, client, peer):
        client.settimeout(180)
        upstream = None
        tag = "?"
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode(errors="replace").split("\r\n")
            parts = (lines[0].split() + ["", ""])[:3]
            method, target = parts[0], parts[1]

            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                tag = f"CONNECT {target}"
                if self._blocked(host):
                    client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    return
                with self._sem:
                    upstream = self.upstream_connect(host, int(port))
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                self._splice(client, upstream, tag)
            else:
                tag = f"{method} {target[:70]}"
                with self._sem:
                    upstream = self.upstream_request(lines, rest)
                self._splice(client, upstream, tag)
        except Exception as e:
            # A broken upstream fails every connection; logging each one buries
            # the rest of the run. Report the first few, then summarise.
            with self._err_lock:
                self._err_count += 1
                n = self._err_count
                show = n <= 3 or n % 25 == 0
            if show:
                self.log(f"  !! {tag} failed ({n} so far): {type(e).__name__} "
                         f"{str(e)[:110]}")
            # Hang instead of failing fast: a 502 makes Chromium drop the proxy
            # and go DIRECT, which is worse than a stalled request.
            try:
                time.sleep(self.hang_on_failure)
            except Exception:
                pass
        finally:
            for s in (client, upstream):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass


class RelayPool:
    """One relay per distinct upstream, started on demand and shared.

    `front` is an optional local proxy that every upstream must be dialled
    through, for endpoints that cannot be reached directly from where the
    console runs. Setting it once beats pasting `front|upstream` into every
    proxy line.
    """

    def __init__(self, google_fallback="", log=None, front=""):
        self.google_fallback = google_fallback or ""
        self.front = front or ""
        self.log = log or (lambda m: None)
        self._lock = threading.Lock()
        self._relays = {}

    def chain(self, upstream_url):
        """Apply the front proxy to an upstream, unless it already has one."""
        if not self.front or "|" in upstream_url:
            return upstream_url
        return f"{self.front}|{upstream_url}"

    def is_chained(self, upstream_url):
        return "|" in self.chain(upstream_url)

    def get(self, upstream_url):
        upstream_url = self.chain(upstream_url)
        with self._lock:
            r = self._relays.get(upstream_url)
            if r is None:
                r = Relay(upstream_url, self.google_fallback, log=self.log)
                r.start()
                self._relays[upstream_url] = r
            return r

    def stop_all(self):
        with self._lock:
            for r in self._relays.values():
                r.stop()
            self._relays.clear()
