#!/usr/bin/env python3
"""Smoke test: headless Chrome + authenticated HTTP proxy + spoofed fingerprint.

Reads back everything the page can observe, so a failed disguise is visible
before it ever reaches the registration flow.
"""
import json
import sys
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/home/beacon/dev/ai/tickets/oasis")
import register  # noqa: E402

PROBE = """async () => {
  const gl = (() => {
    const c = document.createElement('canvas');
    const g = c.getContext('webgl2') || c.getContext('webgl');
    if (!g) return null;
    const d = g.getExtension('WEBGL_debug_renderer_info');
    return {vendor: g.getParameter(d ? d.UNMASKED_VENDOR_WEBGL : 37445),
            renderer: g.getParameter(d ? d.UNMASKED_RENDERER_WEBGL : 37446)};
  })();
  let ip = null;
  try {
    const r = await fetch('https://api.ipify.org?format=json');
    ip = (await r.json()).ip;
  } catch (e) { ip = 'ERR ' + e.message; }
  return {
    ua: navigator.userAgent,
    uaData: navigator.userAgentData ? {
      platform: navigator.userAgentData.platform,
      brands: navigator.userAgentData.brands,
      mobile: navigator.userAgentData.mobile,
    } : null,
    platform: navigator.platform,
    webdriver: navigator.webdriver,
    cores: navigator.hardwareConcurrency,
    memory: navigator.deviceMemory,
    languages: navigator.languages,
    plugins: navigator.plugins.length,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight],
    dpr: devicePixelRatio,
    outer: [outerWidth, outerHeight],
    inner: [innerWidth, innerHeight],
    webgl: gl,
    notif: Notification.permission,
    media: navigator.mediaDevices
      ? (await navigator.mediaDevices.enumerateDevices()).length : null,
    chromeRuntime: !!(window.chrome && window.chrome.runtime),
    egressIp: ip,
  };
}"""


def main():
    proxy_url = sys.argv[1] if len(sys.argv) > 1 else None
    a = SimpleNamespace(
        proxy_url=proxy_url, proxy=False, fp_seed=None, fp_os=None,
        fp_json="/tmp/oasis_fp.json",
        profile="/tmp/oasis_smoke_profile", headless=True,
    )
    with sync_playwright() as p:
        ctx, page, fp = register.launch(p, a)
        page.goto("https://oasis.hq.fan/registration",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        print("TITLE:", page.title())
        print("URL:", page.url)
        print(json.dumps(page.evaluate(PROBE), indent=1, ensure_ascii=False))
        ctx.close()


if __name__ == "__main__":
    main()
