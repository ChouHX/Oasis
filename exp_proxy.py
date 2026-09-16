#!/usr/bin/env python3
"""Does the page's own network stack actually use the relay?

Fetches the echo IP three times from inside the page and prints what the
browser saw. Cross-check the relay log: matching CONNECT lines mean the tunnel
was used; a matching count of zero means Chromium went DIRECT.
"""
import json
import sys
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/home/beacon/dev/ai/tickets/oasis")
import register  # noqa: E402

JS = """async () => {
  const out = {https: [], http: []};
  for (let i = 0; i < 3; i++) {
    const t0 = Date.now();
    try {
      const r = await fetch('https://api.ipify.org?format=json&cb=' + t0 + i,
                            {cache: 'no-store'});
      out.https.push({ip: (await r.json()).ip, ms: Date.now() - t0});
    } catch (e) { out.https.push({err: e.message, ms: Date.now() - t0}); }
  }
  for (let i = 0; i < 3; i++) {
    const t0 = Date.now();
    try {
      const r = await fetch('http://ip-api.com/json?cb=' + t0 + i, {cache: 'no-store'});
      const j = await r.json();
      out.http.push({ip: j.query, ms: Date.now() - t0});
    } catch (e) { out.http.push({err: e.message, ms: Date.now() - t0}); }
  }
  out.ua = navigator.userAgent;
  return out;
}"""


def main():
    proxy_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
    a = SimpleNamespace(proxy_url=proxy_url, proxy=False, fp_seed=None,
                        fp_os=None, fp_json=None,
                        profile="/tmp/oasis_exp_profile", headless=True)
    with sync_playwright() as p:
        ctx, page, fp = register.launch(p, a)
        page.goto("https://oasis.hq.fan/registration",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        print("PAGE FETCH:", json.dumps(page.evaluate(JS), indent=1))
        ctx.close()


if __name__ == "__main__":
    main()
