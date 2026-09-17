#!/usr/bin/env python3
"""Operator settings, persisted as JSON next to the database."""
import json
import os

# Modes the engine accepts. A config written by an older build may still say
# "http" or "hybrid"; load() coerces anything unrecognised to "browser" rather
# than letting the engine abort on an unknown mode.
#
# Only the browser flow is carried.
#
# A hybrid flow used to live here: mint a real captcha in a warm browser, then
# submit with curl_cffi. It is gone, but be precise about why - curl itself was
# not the problem. Measured 2026-09-16: a captcha-less curl_cffi submission
# completed six registrations, while a browser-minted captcha replayed through
# curl_cffi never did. The token appears to be bound to the client that minted
# it, so pairing the two halves fails.
#
# The stronger reason to drive the SPA is that the SPA builds its own request
# body: location.county from radar.io and `ip` from Cloudflare's trace are
# filled in by the page, and a hand-built body cannot reproduce them.
VALID_MODES = ("browser",)

DEFAULTS = {
    # --- where things live -------------------------------------------------
    "db_path": "oasis.db",
    # --- what to run -------------------------------------------------------
    "mode": "browser",
    "threads": 4,
    "shows": ["knebworth", "slane", "glasgow"],
    "delay_between": 0.0,          # pause after each account; 0 = none
    # --- proxy pool --------------------------------------------------------
    "proxies": [],
    # Optional local proxy every upstream is dialled through, for endpoints
    # only reachable from outside the local network (e.g. socks5://127.0.0.1:10808)
    "front_proxy": "",
    # Second egress for Google. Browser mode needs it: reCAPTCHA lives there,
    # and an upstream that blocks Google stalls the page on wait_for_function.
    "google_proxy": "",
    # Mailbox fetches go direct unless this is set.
    "mail_proxy": "",
    # --- iCloud Hide-My-Email service (see core/mailbox.HmeMailbox) --------
    "hme_base": "http://127.0.0.1:8081",
    "hme_password": "",
    # --- timeouts ----------------------------------------------------------
    # How long to wait for the verification mail.
    "link_timeout": 300,
    # `confirm` answers {"status":"OK"} even for an address it refuses, so when
    # the page did not confirm, the mail is the only evidence left and this is
    # how long that search runs. When the page DID confirm it only waits
    # MAIL_GRACE_AFTER_PAGE (30s) - browser mode is measured not to send the
    # success mail, so a full wait there is a minute of nothing.
    "verify_success": False,
    "success_timeout": 180,
    # --- diagnostics -------------------------------------------------------
    "debug": False,
    # Desktop-only: the console writes its own log file. The service logs to
    # stdout, which is where docker picks it up.
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
            self.data["mode"] = "browser"
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
