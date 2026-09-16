#!/usr/bin/env python3
"""Local HTTP relay: 127.0.0.1:8899 -> authenticated upstream HTTP proxy.

Why a relay at all
------------------
Chromium cannot be handed proxy credentials for a persistent context reliably:
on a 407 it marks that proxy bad and can fall back to DIRECT, which leaks the
host egress. So Chrome talks unauthenticated HTTP to us, and we re-issue the
request upstream with Proxy-Authorization attached.

Measured upstream behaviour
---------------------------
* a fresh residential egress per tunnel (~1.5-2 s to first byte)
* it resets connections under bursts, so tunnels are paced and retried with
  exponential backoff
* on final failure the client connection is HUNG rather than answered with
  502: an error response is exactly what makes Chromium mark the proxy dead
  and fall back to DIRECT, and that leak is worse than a timeout.

Chrome's own telemetry/component-update hosts are refused locally so they never
consume an upstream tunnel.
"""
import os
import base64
import socket
import threading
import time

UP_HOST = os.environ.get("UPSTREAM_HOST", "PROXY_HOST")
UP_PORT = int(os.environ.get("UPSTREAM_PORT", "2260"))
UP_AUTH = base64.b64encode(
    os.environ.get("UPSTREAM_AUTH", "user:pass").encode()).decode()
LISTEN = ("127.0.0.1", 8899)

MAX_CONCURRENT = 8
MIN_SPACING = 0.10
ATTEMPT_TIMEOUT = 6.0
CONNECT_BUDGET = 75.0
HANG_ON_FAILURE = 60.0

# Browser chatter that would otherwise burn upstream tunnels.
BLOCKED = (
    "update.googleapis.com",
    "edgedl.me.gvt1.com",
    "optimizationguide-pa.googleapis.com",
    "safebrowsing.googleapis.com",
    "clientservices.googleapis.com",
    "content-autofill.googleapis.com",
)

# The residential upstream refuses the entire Google estate outright (measured:
# instant ECONNRESET on www.google.com, www.gstatic.com, www.recaptcha.net),
# and reCAPTCHA Enterprise cannot initialise without it. Those hosts are routed
# through the local proxy instead, so the registration flow itself still
# egresses from the residential IP.
FALLBACK = ("127.0.0.1", 10808)
GOOGLE_SUFFIXES = (
    "google.com", "gstatic.com", "googleapis.com", "recaptcha.net",
    "googleusercontent.com", "gvt1.com", "googlevideo.com", "withgoogle.com",
)


def _is_google(host):
    h = host.lower()
    return any(h == s or h.endswith("." + s) for s in GOOGLE_SUFFIXES)

_sem = threading.Semaphore(MAX_CONCURRENT)
_pace_lock = threading.Lock()
_last = [0.0]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _pace():
    with _pace_lock:
        wait = MIN_SPACING - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def _blocked(host):
    h = host.lower()
    return any(h == b or h.endswith("." + b) for b in BLOCKED)


def upstream_connect(host, port, budget=CONNECT_BUDGET):
    """Open one CONNECT tunnel, on the egress that can actually reach `host`."""
    if _is_google(host):
        up_host, up_port, auth, route = FALLBACK[0], FALLBACK[1], None, "local"
    else:
        up_host, up_port, auth, route = UP_HOST, UP_PORT, UP_AUTH, "residential"

    deadline = time.time() + budget
    attempt = 0
    last = None
    while time.time() < deadline:
        attempt += 1
        _pace()
        s = None
        try:
            s = socket.create_connection((up_host, up_port), timeout=ATTEMPT_TIMEOUT)
            s.settimeout(ATTEMPT_TIMEOUT)
            auth_hdr = f"Proxy-Authorization: Basic {auth}\r\n" if auth else ""
            req = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n"
                   f"{auth_hdr}"
                   f"Proxy-Connection: keep-alive\r\n\r\n").encode()
            s.sendall(req)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                chunk = s.recv(4096)
                if not chunk:
                    raise IOError("upstream closed during CONNECT")
                buf += chunk
            status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 200" not in status:
                raise IOError(f"upstream CONNECT rejected: {status}")
            s.settimeout(None)
            if attempt > 1:
                log(f"  tunnel {host}:{port} ok on attempt {attempt} ({route})")
            return s
        except Exception as e:
            last = e
            if s:
                try:
                    s.close()
                except Exception:
                    pass
            backoff = min(0.3 * (2 ** min(attempt - 1, 4)), 3.0)
            time.sleep(backoff)
    raise IOError(f"upstream unavailable after {attempt} attempts "
                  f"({route}): {last!r}")


def pump(src, dst):
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


def splice(client, upstream, tag):
    t = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    t.start()
    pump(client, upstream)
    t.join(timeout=2)
    log(f"{tag} closed")


def handle(client, peer):
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
            if _blocked(host):
                log(f"{tag} refused (blocked host)")
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            with _sem:
                upstream = upstream_connect(host, int(port))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            log(f"{tag} ok")
            splice(client, upstream, tag)
        else:
            tag = f"{method} {target[:70]}"
            with _sem:
                upstream = upstream_request(lines, rest)
            log(f"{tag} ok")
            splice(client, upstream, tag)
    except Exception as e:
        log(f"  !! {peer} {tag} {e!r}")
        # Hang instead of failing fast: a 502 makes Chromium drop the proxy
        # and go DIRECT, which is a worse outcome than a stalled request.
        try:
            time.sleep(HANG_ON_FAILURE)
        except Exception:
            pass
    finally:
        for s in (client, upstream):
            if s:
                try:
                    s.close()
                except Exception:
                    pass


def upstream_request(lines, rest):
    """Forward a non-CONNECT proxied request, injecting Proxy-Authorization."""
    host = ""
    for ln in lines[1:]:
        if ln.lower().startswith("host:"):
            host = ln.split(":", 1)[1].strip()
            break
    if not host:
        raise IOError("no Host header")
    if _blocked(host.split(":")[0]):
        raise IOError(f"blocked host {host}")
    h, _, p = host.partition(":")
    s = upstream_connect(h, int(p or 80))
    out = [lines[0]]
    out += [ln for ln in lines[1:]
            if not ln.lower().startswith(("proxy-authorization", "proxy-connection"))]
    out.append(f"Proxy-Authorization: Basic {UP_AUTH}")
    out.append("Proxy-Connection: keep-alive")
    s.sendall(("\r\n".join(out) + "\r\n\r\n").encode() + rest)
    return s


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(256)
    log(f"http relay on {LISTEN} -> {UP_HOST}:{UP_PORT} "
        f"(max {MAX_CONCURRENT} tunnels, budget {CONNECT_BUDGET}s, "
        f"{len(BLOCKED)} hosts refused); google estate -> "
        f"{FALLBACK[0]}:{FALLBACK[1]}")
    while True:
        cl, peer = srv.accept()
        threading.Thread(target=handle, args=(cl, peer), daemon=True).start()


if __name__ == "__main__":
    main()
