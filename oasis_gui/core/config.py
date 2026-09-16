#!/usr/bin/env python3
"""Operator settings, persisted as JSON next to the database."""
import json
import os

# Modes the engine accepts. A config saved before the captcha-less path was
# removed still says "http"; coerce it rather than letting the engine abort.
VALID_MODES = ("hybrid", "browser")

DEFAULTS = {
    "db_path": "oasis.db",
    "proxies": [],
    "threads": 4,
    "shows": ["knebworth", "slane", "glasgow"],
    # hybrid or browser. The captcha-less HTTP mode was removed from the UI; the
    # flow it used (curl_cffi calls) is what hybrid still runs underneath.
    "mode": "hybrid",
    "mail_proxy": "",
    "google_proxy": "",
    # Optional local proxy that every upstream is dialled through. Needed when
    # the residential endpoints cannot be reached directly (e.g. they are only
    # reachable from outside the local network) - e.g. socks5://127.0.0.1:10808
    "front_proxy": "",
    # iCloud Hide-My-Email service (see core/mailbox.HmeMailbox)
    "hme_base": "http://127.0.0.1:8081",
    "hme_password": "",
    # Warn-only by default. The theory that a captcha/confirm egress mismatch
    # makes the server drop the registration is NOT proven: the single hybrid
    # failure traced back to a bad account, which failed in http mode too.
    # Set True only after confirming on your own proxy that a mismatch bites -
    # otherwise it rejects accounts that would have gone through.
    "strict_egress": False,
    "link_timeout": 300,
    "http_timeout": 60,
    "delay_between": 0.0,
    # Randomised pause between reading the mail link and submitting, so the
    # confirm does not land milliseconds after the verification. 0 disables.
    "think_time": 45,
    # confirm answers {"status":"OK"} even for an address it refuses, so the
    # only trustworthy receipt is the "Registration Complete" mail. Turning
    # this on makes the run wait for it (and fail the account without it), at
    # the cost of one extra wait per account.
    # Whether hybrid mints and sends a real captcha. OFF deliberately - see the
    # README; measured, sending one makes confirm fail silently.
    # OFF on purpose: measured 2026-09-16, a real captcha token makes confirm
    # fail silently (empty token completes, a valid 2382-char token does not),
    # most likely because the token is bound to the client that minted it.
    "send_captcha": False,
    "verify_success": False,
    "success_timeout": 180,
    "log_file": "oasis_run.log",
    "debug": False,
}


class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                self.data.update(stored)
        except Exception:
            pass
        # A config saved before the captcha-less path was removed still says
        # "http"; coerce it rather than let the engine abort on an unknown mode.
        if self.data.get("mode") not in VALID_MODES:
            self.data["mode"] = "hybrid"
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        self.data[key] = value

    def update(self, mapping):
        self.data.update(mapping)

    @property
    def db_path(self):
        p = self.get("db_path")
        return p if os.path.isabs(p) else os.path.join(
            os.path.dirname(self.path), p)
