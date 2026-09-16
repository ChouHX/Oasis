#!/usr/bin/env python3
"""Independent re-check: reopen the (single-use) verification link.

A session the backend has already consumed should no longer offer the
registration form, which is the cheapest server-side confirmation that the
earlier submit really landed.
"""
import json
import sys
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/home/beacon/dev/ai/tickets/oasis")
import register  # noqa: E402


def main():
    url = json.load(open("/tmp/oasis_state.json"))["verify_url"]
    a = SimpleNamespace(proxy_url="http://127.0.0.1:8899", proxy=False,
                        fp_seed=None, fp_os=None, fp_json=None,
                        profile="/tmp/oasis_verify_profile", headless=True)
    with sync_playwright() as p:
        ctx, page, fp = register.launch(p, a)
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        print("URL:", page.url)
        print("--- body ---")
        print(page.inner_text("body")[:1500])
        ctx.close()


if __name__ == "__main__":
    main()
