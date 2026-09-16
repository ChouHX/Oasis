#!/usr/bin/env python3
"""Randomised, internally-consistent desktop fingerprint for headless Chromium.

Everything a page can observe is emitted as ONE coherent bundle: UA string,
Sec-CH-UA client hints, navigator platform/hardware, screen geometry, WebGL
vendor+renderer, permissions and media devices.

Linux is deliberately absent from the OS pool: the site's fraud stack reads a
Linux desktop as a headless-automation tell, and the proxy egress is a US
residential line, so the platform has to look like a US desktop.

Chrome major is pinned to the local build (153) so the UA string, the client
hints and the actual JS engine never disagree.
"""
import json
import random

CHROME_MAJOR = "153"
CHROME_FULL = "153.0.0.0"

# (WebGL vendor, WebGL renderer) pairs as a real ANGLE backend reports them.
_WIN_GPUS = [
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]
_MAC_GPUS = [
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, ANGLE Metal Renderer: Intel(R) Iris(TM) Plus Graphics 655, Unspecified Version)"),
]

# CSS-pixel geometry as the browser reports it (physical / display scaling),
# which is what screen.width and window.screen.availHeight actually expose.
_WIN_SCREENS = [
    {"w": 1920, "h": 1080, "dpr": 1.0},
    {"w": 1536, "h": 864, "dpr": 1.25},     # 1080p @125%
    {"w": 2048, "h": 1152, "dpr": 1.25},    # 1440p @125%
    {"w": 1280, "h": 720, "dpr": 1.5},      # 1080p @150%
    {"w": 2560, "h": 1440, "dpr": 1.0},
    {"w": 1680, "h": 1050, "dpr": 1.0},
    {"w": 1366, "h": 768, "dpr": 1.0},
]
_MAC_SCREENS = [
    {"w": 1512, "h": 982, "dpr": 2.0},      # 14" MacBook Pro
    {"w": 1440, "h": 900, "dpr": 2.0},      # 15" MacBook Pro (scaled)
    {"w": 1280, "h": 800, "dpr": 2.0},      # 13" MacBook Air
]

_PLUGINS = [
    {"name": "PDF Viewer", "file": "internal-pdf-viewer",
     "desc": "Portable Document Format",
     "types": [["application/pdf", "pdf"], ["text/pdf", "pdf"]]},
    {"name": "Chrome PDF Viewer", "file": "internal-pdf-viewer",
     "desc": "Portable Document Format",
     "types": [["application/pdf", "pdf"], ["text/pdf", "pdf"]]},
    {"name": "Chromium PDF Viewer", "file": "internal-pdf-viewer",
     "desc": "Portable Document Format",
     "types": [["application/pdf", "pdf"], ["text/pdf", "pdf"]]},
    {"name": "Microsoft Edge PDF Viewer", "file": "internal-pdf-viewer",
     "desc": "Portable Document Format",
     "types": [["application/pdf", "pdf"], ["text/pdf", "pdf"]]},
    {"name": "WebKit built-in PDF", "file": "internal-pdf-viewer",
     "desc": "Portable Document Format",
     "types": [["application/pdf", "pdf"], ["text/pdf", "pdf"]]},
]


def make(seed=None, os_family=None, timezone="America/New_York", locale="en-US"):
    """Build one random but self-consistent fingerprint bundle."""
    rnd = random.Random(seed)
    family = os_family or rnd.choice(["windows", "windows", "windows", "macos"])

    if family == "macos":
        ua_platform = "Macintosh; Intel Mac OS X 10_15_7"
        js_platform = "MacIntel"
        ch_platform = "macOS"
        ch_platform_version = rnd.choice(["15.3.0", "14.5.0", "15.1.0"])
        gpu = rnd.choice(_MAC_GPUS)
        screen = rnd.choice(_MAC_SCREENS)
        cores = rnd.choice([8, 10, 12])
        memory = 8
        bitness, arch = "64", "arm"
    else:
        ua_platform = "Windows NT 10.0; Win64; x64"
        js_platform = "Win32"
        ch_platform = "Windows"
        ch_platform_version = rnd.choice(["15.0.0", "10.0.0"])   # 11 / 10
        gpu = rnd.choice(_WIN_GPUS)
        screen = rnd.choice(_WIN_SCREENS)
        cores = rnd.choice([4, 8, 12, 16])
        memory = rnd.choice([8, 8, 16])
        bitness, arch = "64", "x86"

    ua = (f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{CHROME_FULL} Safari/537.36")

    # Window is maximised: the viewport is the screen minus browser chrome.
    vw = screen["w"]
    vh = max(600, screen["h"] - int(rnd.uniform(88, 132) * min(screen["dpr"], 1.25)))

    brands = [
        {"brand": "Not)A;Brand", "version": "8"},
        {"brand": "Chromium", "version": CHROME_MAJOR},
        {"brand": "Google Chrome", "version": CHROME_MAJOR},
    ]
    full_versions = [
        {"brand": "Not)A;Brand", "version": "8.0.0.0"},
        {"brand": "Chromium", "version": CHROME_FULL},
        {"brand": "Google Chrome", "version": CHROME_FULL},
    ]
    metadata = {
        "platform": ch_platform,
        "platformVersion": ch_platform_version,
        "architecture": arch,
        "model": "",
        "mobile": False,
        "brands": brands,
        "fullVersionList": full_versions,
        "fullVersion": CHROME_FULL,
        "bitness": bitness,
        "wow64": False,
    }
    return {
        "family": family,
        "ua": ua,
        "platform": js_platform,
        "ch_metadata": metadata,
        "locale": locale,
        "languages": [locale, locale.split("-")[0]],
        "timezone": timezone,
        "screen": screen,
        "viewport": {"width": vw, "height": vh},
        "cores": cores,
        "memory": memory,
        "webgl_vendor": gpu[0],
        "webgl_renderer": gpu[1],
        "plugins": _PLUGINS,
        "gpu_short": gpu[1].split(",")[1].strip() if "," in gpu[1] else gpu[1],
    }


_INIT_JS = r"""
(() => {
  const FP = $FP;
  const def = (o, p, v) => {
    try { Object.defineProperty(o, p, {get: () => v, configurable: true}); } catch (e) {}
  };
  const defFn = (o, p, fn) => {
    try { Object.defineProperty(o, p, {value: fn, configurable: true, writable: true}); } catch (e) {}
  };

  // --- navigator -----------------------------------------------------------
  def(navigator, 'webdriver', false);
  def(navigator, 'platform', FP.platform);
  def(navigator, 'hardwareConcurrency', FP.cores);
  def(navigator, 'deviceMemory', FP.memory);
  def(navigator, 'maxTouchPoints', 0);
  def(navigator, 'language', FP.languages[0]);
  def(navigator, 'languages', Object.freeze(FP.languages.slice()));

  // --- screen / window geometry -------------------------------------------
  const s = window.screen;
  def(s, 'width', FP.screen.w);
  def(s, 'height', FP.screen.h);
  def(s, 'availWidth', FP.screen.w);
  def(s, 'availHeight', FP.screen.h - 40);
  def(s, 'colorDepth', 24);
  def(s, 'pixelDepth', 24);
  def(window, 'outerWidth', FP.screen.w);
  def(window, 'outerHeight', FP.screen.h - 40);

  // --- WebGL ---------------------------------------------------------------
  const patchGL = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    defFn(proto, 'getParameter', function (p) {
      if (p === 37445) return FP.webgl_vendor;    // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return FP.webgl_renderer;  // UNMASKED_RENDERER_WEBGL
      return orig.apply(this, arguments);
    });
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // --- notifications / permissions (headless default is 'denied') ----------
  try { def(Notification.prototype, 'permission', 'default'); } catch (e) {}
  try {
    const q = navigator.permissions.query.bind(navigator.permissions);
    defFn(navigator.permissions, 'query', function (p) {
      if (p && p.name === 'notifications') {
        return Promise.resolve({state: 'default', name: 'notifications',
                                onchange: null});
      }
      return q(p);
    });
  } catch (e) {}

  // --- media devices (headless enumerates nothing) -------------------------
  try {
    const md = navigator.mediaDevices;
    if (md) {
      defFn(md, 'enumerateDevices', function () {
        return Promise.resolve([
          {deviceId: 'default', kind: 'audioinput', label: '', groupId: 'g1'},
          {deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'g1'},
          {deviceId: 'd1', kind: 'videoinput', label: '', groupId: 'g2'},
        ]);
      });
    }
  } catch (e) {}

  // --- plugins -------------------------------------------------------------
  try {
    const mk = (p) => {
      const o = Object.create(Plugin.prototype);
      def(o, 'name', p.name); def(o, 'filename', p.file);
      def(o, 'description', p.desc); def(o, 'length', p.types.length);
      p.types.forEach((t, i) => def(o, i, Object.assign(Object.create(MimeType.prototype),
        {type: t[0], suffixes: t[1], description: p.desc, enabledPlugin: o})));
      return o;
    };
    const arr = FP.plugins.map(mk);
    const pa = Object.create(PluginArray.prototype);
    arr.forEach((pl, i) => { def(pa, i, pl); def(pa, pl.name, pl); });
    def(pa, 'length', arr.length);
    defFn(pa, 'item', (i) => arr[i] || null);
    defFn(pa, 'namedItem', (n) => arr.find((p) => p.name === n) || null);
    def(navigator, 'plugins', pa);
    const ma = Object.create(MimeTypeArray.prototype);
    const mimes = arr.flatMap((p) => p.types ? Array.from({length: p.length}, (_, i) => p[i]) : []);
    mimes.forEach((m, i) => { def(ma, i, m); def(ma, m.type, m); });
    def(ma, 'length', mimes.length);
    defFn(ma, 'item', (i) => mimes[i] || null);
    def(navigator, 'mimeTypes', ma);
  } catch (e) {}

  // --- chrome object -------------------------------------------------------
  try {
    if (!window.chrome) def(window, 'chrome', {});
    if (!window.chrome.runtime) def(window.chrome, 'runtime', {});
    if (!window.chrome.app) def(window.chrome, 'app', {isInstalled: false});
    if (!window.chrome.csi) def(window.chrome, 'csi', function () {
      return {onloadT: Date.now(), startE: Date.now() - 1200, pageT: 1200,
              tran: 15};
    });
  } catch (e) {}
})();
"""


def apply(ctx, page, fp):
    """Install the bundle on a live persistent context + its first page."""
    ctx.add_init_script(_INIT_JS.replace("$FP", json.dumps(fp, ensure_ascii=False)))
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.setUserAgentOverride", {
        "userAgent": fp["ua"],
        "acceptLanguage": ",".join(fp["languages"]) + ";q=0.9",
        "platform": fp["platform"],
        "userAgentMetadata": fp["ch_metadata"],
    })
    return cdp


def describe(fp):
    return (f"{fp['family']} chrome{CHROME_MAJOR} "
            f"{fp['screen']['w']}x{fp['screen']['h']}@{fp['screen']['dpr']} "
            f"vp={fp['viewport']['width']}x{fp['viewport']['height']} "
            f"cores={fp['cores']} mem={fp['memory']} gpu={fp['gpu_short']}")


if __name__ == "__main__":
    import sys
    fp = make(seed=int(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(describe(fp))
    print(json.dumps(fp, indent=1, ensure_ascii=False))
