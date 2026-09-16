#!/usr/bin/env python3
"""Hardened relay: 127.0.0.1:8899 -> authenticated SOCKS5 upstream.

Chrome cannot do SOCKS5 auth, so it talks plain HTTP CONNECT to us and we chain
to the upstream. The upstream gateway flaps (long dead windows) and dislikes
connection bursts, so this relay:

  * caps concurrent upstream tunnels (semaphore)
  * spaces out new upstream connections (min interval)
  * retries a failing tunnel with short attempts so Chrome's own timeout is met
"""
import os
import socket, threading, sys, time

UP_HOST = os.environ.get("UPSTREAM_HOST", "PROXY_HOST")
UP_PORT = int(os.environ.get("UPSTREAM_PORT", "2260"))
UP_USER = os.environ.get("UPSTREAM_USER", "user").encode()
UP_PASS = os.environ.get("UPSTREAM_PASS", "pass").encode()
LISTEN = ("127.0.0.1", 8899)

MAX_CONCURRENT = 4          # simultaneous upstream tunnels
MIN_SPACING = 0.20          # seconds between new upstream connections
ATTEMPT_TIMEOUT = 3.0       # per-attempt socket timeout
TOTAL_BUDGET = 22.0         # keep under Chrome's ~30s navigation timeout

_sem = threading.Semaphore(MAX_CONCURRENT)
_spacing_lock = threading.Lock()
_last_connect = [0.0]


def _pace():
    """Serialise the start of upstream connections."""
    with _spacing_lock:
        wait = MIN_SPACING - (time.time() - _last_connect[0])
        if wait > 0:
            time.sleep(wait)
        _last_connect[0] = time.time()


def socks_connect(host, port):
    deadline = time.time() + TOTAL_BUDGET
    last = None
    while time.time() < deadline:
        _pace()
        s = None
        try:
            s = socket.create_connection((UP_HOST, UP_PORT), timeout=ATTEMPT_TIMEOUT)
            s.settimeout(ATTEMPT_TIMEOUT)
            s.sendall(b"\x05\x01\x02")
            if s.recv(2) != b"\x05\x02":
                raise IOError("greeting")
            s.sendall(b"\x01" + bytes([len(UP_USER)]) + UP_USER +
                      bytes([len(UP_PASS)]) + UP_PASS)
            if s.recv(2)[1:2] != b"\x00":
                raise IOError("auth")
            h = host.encode()
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h +
                      (port).to_bytes(2, "big"))
            r = s.recv(10)
            if r[1:2] != b"\x00":
                raise IOError("connect rep=%r" % (r,))
            s.settimeout(None)
            return s
        except Exception as e:
            last = e
            if s:
                try: s.close()
                except Exception: pass
            time.sleep(0.25)
    raise IOError("upstream unavailable: %r" % (last,))


def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    finally:
        try: b.shutdown(socket.SHUT_WR)
        except Exception: pass


def handle(client):
    client.settimeout(120)
    upstream = None
    try:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk
        line = data.split(b"\r\n")[0].decode(errors="replace")
        if not line.startswith("CONNECT"):
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, port = line.split()[1].rsplit(":", 1)
        with _sem:
            upstream = socks_connect(host, int(port))
        print(f"CONNECT {host}:{port} ok", flush=True)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        rest = data.split(b"\r\n\r\n", 1)[1]
        if rest:
            upstream.sendall(rest)
        t = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        t.start()
        pump(client, upstream)
        t.join(timeout=2)
    except Exception as e:
        print(f"  !! {e!r}", flush=True)
        try: client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except Exception: pass
    finally:
        for s in (client, upstream):
            if s:
                try: s.close()
                except Exception: pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN); srv.listen(128)
    print(f"relay on {LISTEN} -> {UP_HOST}:{UP_PORT} "
          f"(max {MAX_CONCURRENT} concurrent, {MIN_SPACING}s spacing)", flush=True)
    while True:
        cl, _ = srv.accept()
        threading.Thread(target=handle, args=(cl,), daemon=True).start()


if __name__ == "__main__":
    main()
