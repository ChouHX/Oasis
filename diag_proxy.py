#!/usr/bin/env python3
"""Why is the browser egress not the proxy? Inspect the real Chrome cmdline."""
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/home/beacon/dev/ai/tickets/oasis")
import register  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "TEST_PROXY", "http://user:pass@PROXY_HOST:PORT")
PROFILE = "/tmp/oasis_diag_profile"


def chrome_cmdline():
    out = subprocess.run(["pgrep", "-af", PROFILE], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "--proxy" in line or "--type=" not in line:
            toks = [t for t in line.split() if t.startswith("--proxy")
                    or t.startswith("--host-resolver") or t.startswith("--headless")]
            if toks:
                return toks
    return ["<no proxy flag found>"]


def main():
    a = SimpleNamespace(proxy_url=PROXY, proxy=False, fp_seed=None, fp_os=None,
                        fp_json=None, profile=PROFILE, headless=True)
    with sync_playwright() as p:
        ctx, page, fp = register.launch(p, a)
        time.sleep(2)
        print("CHROME PROXY FLAGS:", chrome_cmdline())
        print("context proxy via CDP:", ctx.new_cdp_session(page).send("Network.enable") is not None)
        for url in ("https://api.ipify.org",
                    "https://api.openstage.live/fan2/page/28400196-01ba-4920-810b-9592f9f1045d/registration"):
            try:
                r = page.request.get(url, timeout=30000)
                body = r.text()[:80].replace("\n", " ")
                print(f"{url} -> {r.status} {body!r}")
            except Exception as e:
                print(f"{url} -> ERR {type(e).__name__} {e}")
        ctx.close()


if __name__ == "__main__":
    main()
