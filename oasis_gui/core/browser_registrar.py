#!/usr/bin/env python3
"""Browser-driven registration: the whole flow runs inside a real Chromium.

Why this exists
---------------
The plain-HTTP path posts /fan2/verify/confirm with an empty captcha field. The
server accepts it today, but reCAPTCHA Enterprise exists precisely to score that
call, so an empty token is the one thing most likely to get an account flagged.
This module runs the same three calls from inside a real browser page, with a
genuine reCAPTCHA Enterprise token produced by the page's own client:

    grecaptcha.enterprise.execute(SITEKEY, {action: 'fan_verification'})

Measured: the page only initialises reCAPTCHA once the form renders, which
requires a valid token in the URL - loading /registration bare leaves
window.grecaptcha undefined. So the mail link has to be opened before the
captcha can be produced.

The API calls are issued with fetch() from the page context, so they carry the
page's origin, referer and the browser's TLS stack. That keeps the browser
fingerprint, the captcha token and the request that consumes it consistent with
each other - which is the entire point of this mode.

Transport: Chromium talks to a local relay (core.relay) because the residential
upstream refuses the Google estate outright and reCAPTCHA cannot load without
it. The relay splits Google to a second egress; the registration traffic itself
still leaves from the residential IP.
"""
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import registrar

VIEWPORTS = [
    {"width": 1280, "height": 720},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
]
TIMEZONES = {
    "US": ["America/New_York", "America/Chicago", "America/Denver",
           "America/Los_Angeles"],
    "DE": ["Europe/Berlin"],
    "FR": ["Europe/Paris"],
    "GB": ["Europe/London"],
}
# Real Chrome desktop UA strings. Playwright's default says
# "HeadlessChrome/151..." right in the user agent, which on its own is enough for
# a site to tell automation apart.
CHROME_VERSIONS = ["131.0.0.0", "136.0.0.0", "142.0.0.0", "145.0.0.0",
                   "146.0.0.0", "150.0.0.0"]
GPUS = [
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]

# Headless Chromium reports plugins=0, mimeTypes=0, no window.chrome, a
# SwiftShader WebGL renderer and navigator.webdriver=true. Real Chrome reports
# none of that. These are the signals that make a site quietly skip sending the
# verification mail while still answering {"status":"OK"}.
_STEALTH_JS = r"""
(() => {
  const define = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, {get: () => value, configurable: true}); }
    catch (e) {}
  };

  // window.chrome is absent in headless
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      OnInstalledReason: {CHROME_UPDATE:'chrome_update', INSTALL:'install',
                          SHARED_MODULE_UPDATE:'shared_module_update', UPDATE:'update'},
      PlatformArch: {ARM:'arm', ARM64:'arm64', MIPS:'mips', MIPS64:'mips64',
                     X86_32:'x86-32', X86_64:'x86-64'},
      PlatformOs: {ANDROID:'android', CROS:'cros', LINUX:'linux', MAC:'mac',
                   OPENBSD:'openbsd', WIN:'win'},
    };
  }
  if (!window.chrome.app) {
    window.chrome.app = {
      isInstalled: false,
      InstallState: {DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed'},
      RunningState: {CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running'},
    };
  }
  if (!window.chrome.csi) {
    window.chrome.csi = () => ({onloadT: Date.now(), startE: Date.now(),
                                pageT: 100.0, tran: 15});
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = () => ({
      commitLoadTime: Date.now()/1000, connectionInfo: 'h2',
      finishDocumentLoadTime: Date.now()/1000, finishLoadTime: Date.now()/1000,
      firstPaintAfterLoadTime: 0, firstPaintTime: Date.now()/1000,
      navigationType: 'Other', npnNegotiatedProtocol: 'h2',
      requestTime: Date.now()/1000, startLoadTime: Date.now()/1000,
      wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: true,
      wasNpnNegotiated: true,
    });
  }

  // plugins / mimeTypes: headless reports zero, real Chrome reports 5 and 2
  try {
    const mimeSpecs = [
      {type:'application/pdf', suffixes:'pdf', description:'Portable Document Format'},
      {type:'text/pdf', suffixes:'pdf', description:'Portable Document Format'},
    ];
    const mkMime = (s) => {
      const m = Object.create(MimeType.prototype);
      Object.defineProperties(m, {
        type:{value:s.type, enumerable:true},
        suffixes:{value:s.suffixes, enumerable:true},
        description:{value:s.description, enumerable:true},
        enabledPlugin:{value:null, enumerable:true, writable:true},
      });
      return m;
    };
    const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                   'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
    const plugins = names.map((n) => {
      const p = Object.create(Plugin.prototype);
      const items = mimeSpecs.map(mkMime);
      Object.defineProperties(p, {
        name:{value:n, enumerable:true},
        filename:{value:'internal-pdf-viewer', enumerable:true},
        description:{value:'Portable Document Format', enumerable:true},
        length:{value:items.length, enumerable:true},
      });
      items.forEach((m, i) => {
        Object.defineProperty(p, i, {value:m, enumerable:true});
        Object.defineProperty(m, 'enabledPlugin', {value:p, enumerable:true});
      });
      return p;
    });
    const pa = Object.create(PluginArray.prototype);
    plugins.forEach((p, i) => Object.defineProperty(pa, i, {value:p, enumerable:true}));
    Object.defineProperty(pa, 'length', {value:plugins.length, enumerable:false});
    pa.item = (i) => plugins[i] || null;
    pa.namedItem = (n) => plugins.find((p) => p.name === n) || null;
    pa.refresh = () => {};
    define(navigator, 'plugins', pa);

    const ma = Object.create(MimeTypeArray.prototype);
    mimeSpecs.forEach((s, i) =>
      Object.defineProperty(ma, i, {value:mkMime(s), enumerable:true}));
    Object.defineProperty(ma, 'length', {value:mimeSpecs.length, enumerable:false});
    ma.item = (i) => mimeSpecs[i] || null;
    ma.namedItem = (n) => mimeSpecs.find((s) => s.type === n) || null;
    define(navigator, 'mimeTypes', ma);
  } catch (e) {}

  // WebGL: headless renders through SwiftShader, real desktops do not
  try {
    const GPU = __GPU__;
    const patch = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return GPU.vendor;
        if (p === 37446) return GPU.renderer;
        return orig.call(this, p);
      };
    };
    patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}

  define(navigator, 'platform', 'Win32');
  define(navigator, 'hardwareConcurrency', __CORES__);
  define(navigator, 'deviceMemory', 8);
  define(navigator, 'webdriver', false);
  define(navigator, 'languages', ['en-US', 'en']);

  // headless answers "denied" for notifications; a normal profile says "default"
  try {
    if (window.Notification) {
      Object.defineProperty(Notification, 'permission',
                            {get: () => 'default', configurable: true});
    }
  } catch (e) {}
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const q = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (p) => (
        p && p.name === 'notifications'
          ? Promise.resolve({state: 'prompt', onchange: null})
          : q(p)
      );
    }
  } catch (e) {}
})();
"""

# "" means Playwright's own chromium, "chrome" means a system Chrome install.
# None is the "not resolved yet" sentinel.
_CHANNEL = None
_CHANNEL_LOCK = threading.Lock()


def _install_chromium(log):
    """Fetch Playwright's chromium once. Works from a frozen exe too, because
    the driver (node + cli.js) is bundled inside the app.

    _browsers_root() has already exported PLAYWRIGHT_BROWSERS_PATH, so the
    install lands where _chromium_on_disk() will look for it afterwards.
    """
    from playwright._impl._driver import compute_driver_executable
    node, cli = compute_driver_executable()
    dest = _browsers_root()
    log(f"no browser found - downloading Playwright chromium "
        f"(one time, ~150 MB) into {dest}")
    r = subprocess.run([node, cli, "install", "chromium"], env=dict(os.environ),
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise BrowserRegistrationError(
            "could not install chromium: " + (r.stderr or r.stdout)[-300:])


def _browsers_root():
    """Where the browser lives, and make Playwright agree with us.

    The environment variable is *set*, not merely read: Playwright's own
    launch() resolves PLAYWRIGHT_BROWSERS_PATH itself, so a download placed at
    <exe>/browsers while the variable is unset would be looked for somewhere
    else on the next run and fetched all over again.

    A frozen build keeps the ~150MB download next to the exe (portable, and
    visible to whoever installed it) instead of deep inside the user profile.
    An explicitly set PLAYWRIGHT_BROWSERS_PATH always wins.
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        if getattr(sys, "frozen", False):
            root = os.path.join(os.path.dirname(sys.executable), "browsers")
        elif sys.platform == "win32":
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                "ms-playwright")
        elif sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Caches/ms-playwright")
        else:
            root = os.path.expanduser("~/.cache/ms-playwright")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = root
    return root


def _chromium_on_disk():
    """Locate an installed Playwright chromium without starting the driver.

    Recent Chromium builds unpack to `chrome-linux64/chrome`; older ones used
    `chrome-linux/chrome`. The official Playwright image ships the former, and
    only checking the latter made the container decide no browser was present
    and download 150MB it already had.
    """
    root = _browsers_root()
    if not os.path.isdir(root):
        return None
    if sys.platform == "win32":
        candidates = [("chrome-win", "chrome.exe"),
                      ("chrome-win64", "chrome.exe")]
    elif sys.platform == "darwin":
        candidates = [("chrome-mac", "Chromium.app", "Contents", "MacOS",
                       "Chromium")]
    else:
        candidates = [("chrome-linux64", "chrome"), ("chrome-linux", "chrome")]
    for name in sorted(os.listdir(root), reverse=True):
        if not name.startswith("chromium-"):
            continue
        for rel in candidates:
            path = os.path.join(root, name, *rel)
            if os.path.exists(path):
                return path
    return None


def _system_chrome():
    """Path to a system Chrome, when there is no bundled chromium."""
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 os.path.expandvars(
                     r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                 "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if path and os.path.exists(path):
            return path
    return None


def resolve_channel(log):
    """Pick a usable browser: bundled chromium, then system Chrome, then install.

    Presence is checked on disk rather than by launching a probe. A bare argless
    launch can fail for reasons unrelated to the browser being absent (container
    sandboxing), which used to trigger a pointless 150 MB download.
    """
    global _CHANNEL
    with _CHANNEL_LOCK:
        if _CHANNEL is not None:
            return _CHANNEL or None

        found = _chromium_on_disk()
        if found:
            _CHANNEL = ""
            log(f"browser engine: Playwright chromium ({found})")
            return None

        for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                     os.path.expandvars(
                         r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                     "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
            if path and os.path.exists(path):
                _CHANNEL = "chrome"
                log(f"browser engine: system Chrome ({path})")
                return "chrome"

        _install_chromium(log)
        _CHANNEL = ""
        return None


class _SharedBrowser:
    """One chromium process per proxy, shared by every worker over CDP.

    Playwright's sync API binds a browser to the thread that launched it, so
    workers cannot share one directly - that is the only reason this file used
    to launch a browser per thread. Launching chromium ourselves with
    --remote-debugging-port and having each thread connect_over_cdp() lifts
    that restriction: one browser process, one isolated context per worker.

    Why it is worth doing: a separate browser costs ~423MB per worker against
    ~199MB for a context, and the extra processes buy no fingerprint diversity
    at all. Measured on three instances each way, the canvas hash and the WebGL
    string came out byte-identical across separate browsers - the fingerprint
    comes from the Chromium build and the machine, not the process. Contexts
    still isolate cookies, storage and cache, which is what actually matters.
    """

    def __init__(self, exe, proxy_url, log=None):
        self.exe = exe
        self.proxy_url = proxy_url
        self.log = log or (lambda m: None)
        self.proc = None
        self.port = None
        # RLock: endpoint() holds this while starting, and callers may already
        # hold it (see BrowserRegistrar._shared_browser) to close the race where
        # two workers both see "not started yet" and each launch a browser.
        self._lock = threading.RLock()

    def endpoint(self):
        """Start chromium if needed; return the CDP http endpoint."""
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return f"http://127.0.0.1:{self.port}"
            self._start()
            return f"http://127.0.0.1:{self.port}"

    def _start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.port = _free_port()
        profile = tempfile.mkdtemp(prefix="oasis-browser-")
        args = [
            self.exe, "--headless=new",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if self.proxy_url:
            args.append(f"--proxy-server={self.proxy_url}")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise BrowserRegistrationError(
                    f"shared chromium exited immediately (code {self.proc.returncode})")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2):
                    self.log(f"    shared browser on :{self.port} "
                             f"(pid {self.proc.pid}, proxy={self.proxy_url})")
                    return
            except Exception:
                time.sleep(0.25)
        self.stop()
        raise BrowserRegistrationError("shared chromium never opened its debug port")

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def stop(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    self.proc.kill()
            self.proc = None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrowserRegistrationError(Exception):
    pass


class BrowserRegistrar:
    """One browser session per registration."""

    def __init__(self, relay_pool, headless=True, browser_timeout=90, log=None,
                 strict_egress=False):
        self.relays = relay_pool
        self.headless = headless
        self.browser_timeout = browser_timeout
        self.log = log or (lambda m: None)
        # when True, a captcha/submit egress mismatch fails the account instead
        # of only being logged
        self.strict_egress = strict_egress
        # Each thread gets its own warm context; the chromium process behind
        # them is shared per proxy (see _SharedBrowser), because contexts are
        # what isolate accounts and separate processes cost twice the memory
        # for an identical fingerprint.
        self._local = threading.local()
        self._shared = {}
        self._shared_lock = threading.Lock()

    # ------------------------------------------------------- warm captcha minting
    _EXEC_JS = """async (k) => await new Promise((res, rej) => {
        window.grecaptcha.enterprise.ready(async () => {
            try { res(await window.grecaptcha.enterprise.execute(k,
                      {action: 'fan_verification'})); }
            catch (e) { rej(String(e)); }
        });
    })"""

    def _warm_entry(self, relay_url, ident):
        """A loaded page on the site's origin, kept alive between accounts.

        The captcha does not depend on the registration token: reCAPTCHA only
        needs the page's origin plus the site key. So the browser loads the
        origin once and mints every subsequent token in ~0.3s, instead of
        paying a full page load per account.

        One chromium process serves every worker (see _SharedBrowser); the
        context below is this thread's private slice of it.
        """
        from playwright.sync_api import sync_playwright

        cache = getattr(self._local, "warm", None)
        if cache is None:
            cache = self._local.warm = {}
        entry = cache.get(relay_url)
        if entry and entry.get("alive"):
            return entry
        if entry:                                   # drop a dead connection
            try:
                entry["pw"].stop()
            except Exception:
                pass

        shared = self._shared_browser(relay_url)
        endpoint = shared.endpoint()
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(endpoint)
            ctx = browser.new_context(
                viewport=random.choice(VIEWPORTS),
                user_agent=self._user_agent(random.Random()),
                locale="en-US",
                timezone_id=random.choice(
                    TIMEZONES.get((ident.get("location") or {}).get("countryCode"),
                                  TIMEZONES["US"])),
                device_scale_factor=1,
            )
            ctx.add_init_script(self._stealth_js(random.Random()))
            ctx.set_default_timeout(self.browser_timeout * 1000)
            page = ctx.new_page()
            for attempt in range(3):
                try:
                    page.goto(registrar.RETURN_URL, wait_until="domcontentloaded",
                              timeout=self.browser_timeout * 1000)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    self.log(f"    warm nav retry {attempt + 1}: {type(e).__name__}")
            page.wait_for_timeout(800)
            if not page.evaluate("() => !!(window.grecaptcha && "
                                 "window.grecaptcha.enterprise)"):
                page.add_script_tag(
                    url=f"https://www.google.com/recaptcha/enterprise.js"
                        f"?render={registrar.SITEKEY}")
                for _ in range(30):
                    if page.evaluate("() => !!(window.grecaptcha && "
                                     "window.grecaptcha.enterprise)"):
                        break
                    page.wait_for_timeout(1000)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise
        entry = {"pw": pw, "browser": browser, "ctx": ctx, "page": page,
                 "alive": True}
        cache[relay_url] = entry
        self.log(f"    warm context ready on {endpoint}")
        return entry

    def _shared_browser(self, relay_url):
        """The single browser process for this proxy, guaranteed started.

        Starting it inside the lock matters: a freshly built _SharedBrowser has
        no process yet, so two workers arriving together would both read it as
        dead and each launch one.
        """
        with self._shared_lock:
            shared = self._shared.get(relay_url)
            if shared is None or not shared.alive():
                exe = _chromium_on_disk() or _system_chrome()
                if not exe:
                    raise BrowserRegistrationError(
                        "no chromium available for the shared browser")
                shared = _SharedBrowser(exe, relay_url, self.log)
                self._shared[relay_url] = shared
            shared.endpoint()           # idempotent; starts if needed
            return shared

    def mint_captcha(self, proxy_url, ident, retries=3):
        """A genuine reCAPTCHA token, minted from the warm page."""
        relay = self.relays.get(proxy_url)
        last = None
        for attempt in range(retries):
            entry = self._warm_entry(relay.url, ident)
            try:
                tok = entry["page"].evaluate(self._EXEC_JS, registrar.SITEKEY)
                if tok:
                    return tok, self._browser_egress(entry["page"])
            except Exception as e:
                last = e
                entry["alive"] = False
                try:
                    entry["browser"].close()
                    entry["pw"].stop()
                except Exception:
                    pass
                self._local.warm.pop(relay.url, None)
            self.log(f"    captcha retry {attempt + 1}: {type(last).__name__}")
        raise BrowserRegistrationError(f"could not mint a captcha: {last}")

    def close_warm(self):
        """Tear down this thread's contexts, then any shared browser."""
        cache = getattr(self._local, "warm", None) or {}
        for entry in list(cache.values()):
            try:
                entry["ctx"].close()
                entry["pw"].stop()
            except Exception:
                pass
        cache.clear()
        with self._shared_lock:
            for shared in self._shared.values():
                shared.stop()
            self._shared.clear()

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _user_agent(rnd):
        version = rnd.choice(CHROME_VERSIONS)
        return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")

    @staticmethod
    def _stealth_js(rnd):
        gpu = rnd.choice(GPUS)
        cores = rnd.choice([4, 8, 12, 16])
        return (_STEALTH_JS
                .replace("__GPU__", json.dumps({"vendor": gpu[0],
                                                "renderer": gpu[1]}))
                .replace("__CORES__", str(cores)))

    def _launch(self, playwright, proxy_url, rnd, ident):
        country = (ident.get("location") or {}).get("countryCode", "US")
        viewport = rnd.choice(VIEWPORTS)
        timezone = rnd.choice(TIMEZONES.get(country, TIMEZONES["US"]))
        channel = resolve_channel(self.log)
        kw = {"headless": self.headless, "proxy": {"server": proxy_url},
              "args": ["--disable-blink-features=AutomationControlled",
                       "--disable-dev-shm-usage", "--no-first-run",
                       "--no-default-browser-check",
                       "--disable-features=IsolateOrigins,site-per-process"]}
        if channel:
            kw["channel"] = channel
        browser = playwright.chromium.launch(**kw)
        ctx = browser.new_context(
            viewport=viewport,
            user_agent=self._user_agent(rnd),
            locale="en-US",
            timezone_id=timezone,
            device_scale_factor=1,
        )
        # patch the signals Chromium leaks in headless before any page script runs
        ctx.add_init_script(self._stealth_js(rnd))
        ctx.set_default_timeout(self.browser_timeout * 1000)
        page = ctx.new_page()
        self.log(f"    browser: {viewport['width']}x{viewport['height']} "
                 f"{timezone} via {proxy_url}")
        return browser, ctx, page

    @staticmethod
    def _fetch(page, url, method="GET", body=None):
        """Issue a request from the page so it carries the page's fingerprint."""
        script = """
        async ([url, method, body]) => {
            const opts = {method, headers: {'accept': 'application/json, text/plain, */*'}};
            if (body !== null) {
                opts.headers['content-type'] = 'application/json';
                opts.body = JSON.stringify(body);
            }
            const r = await fetch(url, opts);
            return {status: r.status, text: await r.text()};
        }
        """
        return page.evaluate(script, [url, method, body])

    def _captcha(self, page):
        """Real reCAPTCHA Enterprise token for the page's verification action."""
        page.wait_for_function(
            "() => !!(window.grecaptcha && window.grecaptcha.enterprise)",
            timeout=self.browser_timeout * 1000)
        token = page.evaluate("""
        async (key) => await new Promise((resolve, reject) => {
            window.grecaptcha.enterprise.ready(async () => {
                try {
                    resolve(await window.grecaptcha.enterprise.execute(
                        key, {action: %s}));
                } catch (e) { reject(String(e)); }
            });
        })
        """ % json.dumps(registrar.CAPTCHA_ACTION), registrar.SITEKEY)
        if not token:
            raise BrowserRegistrationError("grecaptcha returned an empty token")
        return token

    # ---------------------------------------------------------------------- flow
    # The SPA's own end-of-flow page. Nothing else in the DOM says "done".
    SUCCESS_PAGE_HINTS = ("thanks for registering", "registration complete")

    @classmethod
    def _page_says_registered(cls, page):
        try:
            txt = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        return any(h in txt for h in cls.SUCCESS_PAGE_HINTS)


    # ---------------------------------------------------------- SPA form driving
    def _fill_details(self, page, ident, log):
        """Step 1. Typing, not fill(): these are custom components and fill()
        does not raise the input events they listen for (the phone field then
        reports "Please enter a valid phone number")."""
        page.fill("#soundcheckConfirmFirstName", ident["first_name"])
        page.wait_for_timeout(350)
        page.fill("#soundcheckConfirmLastName", ident["last_name"])
        page.wait_for_timeout(350)
        for attempt in range(4):
            field = page.query_selector("input#soundcheckConfirmPhoneNumber")
            field.click()
            # select-all + delete, not fill(""): the component tracks its own
            # model and a plain clear can leave the old value in it.
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(300)
            page.type("input#soundcheckConfirmPhoneNumber", ident["phone"], delay=130)
            page.wait_for_timeout(800)
            typed = page.input_value("#soundcheckConfirmPhoneNumber")
            page.keyboard.press("Tab")
            page.wait_for_timeout(1200)
            bad = "Please enter a valid phone number" in page.inner_text("body")
            if not bad:
                break
            log(f"    phone attempt {attempt + 1} rejected "
                f"(field holds {typed!r}), retrying")
            page.wait_for_timeout(500)
        # The date picker is custom: a text input that ignores typing, plus two
        # select-style triggers and a day grid. Reopening it is the only reliable
        # retry, and the field has to be non-empty or Continue silently stalls.
        for attempt in range(4):
            if page.input_value("#birthDate"):
                break
            page.click("#birthDate")
            page.wait_for_timeout(1800)
            for tid, val in (("#year", str(ident["dob_year"])),
                             ("#month", ident["dob_month"])):
                try:
                    page.click(tid, force=True, timeout=6000)
                    page.wait_for_timeout(1300)
                    page.click(f'[role=option]:has-text("{val}")', timeout=6000)
                    page.wait_for_timeout(1300)
                except Exception:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
            cal = page.query_selector("[data-testid=calendar]")
            if cal:
                for cell in cal.query_selector_all("button, [role=gridcell], td"):
                    if (cell.inner_text() or "").strip() == str(ident["dob_day"]):
                        try:
                            cell.click()
                            break
                        except Exception:
                            pass
            page.wait_for_timeout(1000)
            if not page.input_value("#birthDate"):
                log(f"    birthdate attempt {attempt + 1} did not take, reopening")
        page.wait_for_timeout(600)
        for attempt in range(3):
            page.click("input#soundcheckConfirmLocation")
            page.fill("input#soundcheckConfirmLocation", "")
            page.wait_for_timeout(250)
            page.type("input#soundcheckConfirmLocation", ident["location_query"],
                      delay=100)
            page.wait_for_timeout(4200)
            opts = page.query_selector_all("[role=option]")
            if opts:
                opts[0].click()
                page.wait_for_timeout(1000)
                break
        log(f"    details: name={page.input_value('#soundcheckConfirmFirstName')!r} "
            f"phone={page.input_value('#soundcheckConfirmPhoneNumber')!r} "
            f"dob={page.input_value('#birthDate')!r} "
            f"loc={page.input_value('input#soundcheckConfirmLocation')!r}")

    STEP_MARKERS = {
        "cities": "choose up to three cities",
        "question": "answer the question",
        "terms": "click the submit button below",
        "done": "thanks for registering",
    }

    def _click_continue(self, page, until=None, timeout=60):
        """Click Continue and wait for the next step, not for a fixed delay.

        Fixed sleeps are what break under concurrency: with a dozen contexts
        sharing the CPU, 9s is sometimes not enough for the next step to render,
        the following click lands on a half-built page, and the run dies with
        "page never confirmed". Waiting on the next step's own text is
        load-independent and is also faster when the machine is idle.
        """
        for label in ("Continue", "Submit", "Complete", "Register"):
            btn = page.query_selector(f"button:has-text('{label}')")
            if btn and btn.is_enabled():
                btn.click()
                break
        else:
            return False
        marker = self.STEP_MARKERS.get(until)
        if not marker:
            page.wait_for_timeout(1500)
            return True
        try:
            page.wait_for_function(
                "m => document.body.innerText.toLowerCase().includes(m)",
                arg=marker, timeout=timeout * 1000)
            return True
        except Exception:
            return False

    def _pick_show(self, page, venue, log):
        """Pick one venue in the preference multiselect, by its city name."""
        want = registrar.SHOW_META.get(venue, ("", "", ""))[1].lower()
        ms = page.query_selector("[data-testid=multiselect]")
        btn = ms.query_selector("[role=combobox]") if ms else None
        if not btn:
            return False
        btn.click()
        page.wait_for_timeout(1400)
        for opt in page.query_selector_all("[role=option]"):
            if want and want in (opt.inner_text() or "").lower():
                try:
                    opt.click()
                    page.wait_for_timeout(900)
                    return True
                except Exception:
                    return False
        page.keyboard.press("Escape")
        return False

    def _drive_form(self, page, ident, order, log):
        """Walk the whole SPA form and let the SPA submit itself.

        The API calls are the site's own, so the body carries everything a real
        visitor sends - county, ip, consentMessaging - which a hand-built body
        cannot reproduce. Only the page's confirmation is available afterwards:
        browser mode is measured not to send the success mail.
        """
        page.wait_for_selector("#soundcheckConfirmFirstName", timeout=90000)
        for attempt in range(3):
            self._fill_details(page, ident, log)
            if self._click_continue(page, until="cities", timeout=60):
                break
            body = page.inner_text("body")
            shown = [m for m in ("Please enter a valid phone number",
                                 "Location is required", "Phone number is required",
                                 "Name is required", "Date of birth is required")
                     if m in body]
            log(f"    details rejected ({shown or 'no message'}), retry {attempt + 1}")
            if attempt == 2:
                raise BrowserRegistrationError(
                    f"details step did not advance; page says {shown or 'nothing'}")

        picked = 0
        for venue in list(order)[:3]:
            if self._pick_show(page, venue, log):
                picked += 1
        if picked == 0:
            raise BrowserRegistrationError("no venue could be selected")
        for cb in page.query_selector_all("[role=combobox]"):
            if "Select an option" in (cb.inner_text() or ""):
                cb.click()
                page.wait_for_timeout(1300)
                opts = page.query_selector_all("[role=option]")
                if opts:
                    opts[-1].click()            # travel: No
                    page.wait_for_timeout(1000)
                break
        self._click_continue(page, until="question", timeout=60)
        log(f"    {picked} venue(s) selected")

        for step in range(5):
            body = page.inner_text("body")
            log(f"    step {step + 1}: {body[:110].strip()}")
            # "Terms & Conditions" also appears in the page footer on every
            # step, so match the T&C page's own sentence instead.
            if "click the submit button below" in body:
                break
            if "answer the question" in body:
                for cb in page.query_selector_all("[role=combobox]"):
                    if "Select" in (cb.inner_text() or ""):
                        cb.click()
                        page.wait_for_timeout(1400)
                        opts = page.query_selector_all("[role=option]")
                        log(f"      album options: {len(opts)}")
                        if opts:
                            log(f"      picking {(opts[0].inner_text() or '')[:26]!r}")
                            opts[0].click()
                            page.wait_for_timeout(1200)
                        break
            self._click_continue(page, until="terms", timeout=60)

        # The T&C page will not submit until the terms have been scrolled to the
        # end; the button does nothing (and reports nothing) otherwise.
        if "click the submit button below" not in page.inner_text("body"):
            raise BrowserRegistrationError("never reached the terms step")
        scroll = page.query_selector("button:has-text('Scroll to bottom')")
        if scroll:
            scroll.click()
            page.wait_for_timeout(2500)
        submit = page.query_selector("button:has-text('Submit')")
        if not submit:
            raise BrowserRegistrationError("no submit button on the T&C page")
        submit.click()
        page.wait_for_function(
            "() => document.body.innerText.toLowerCase()"
            ".includes('thanks for registering')", timeout=90000)
        page.wait_for_timeout(1500)

    @staticmethod
    def _page_client_ip(page):
        """The exit IP as Cloudflare sees it, asked from inside the page.

        The SPA fills the confirm body's `ip` this way; sending null is a
        structural difference from a real browser's request.
        """
        try:
            txt = page.evaluate(
                "async () => await (await fetch("
                "'https://www.cloudflare.com/cdn-cgi/trace',"
                "{credentials:'omit'})).text()")
            for line in (txt or "").splitlines():
                if line.startswith("ip="):
                    return line[3:].strip()
        except Exception:
            pass
        return None

    @staticmethod
    def _browser_egress(page):
        """Public IP the browser page goes out from."""
        try:
            return page.evaluate(
                "async () => (await (await fetch("
                "'https://api.ipify.org?format=json')).json()).ip")
        except Exception:
            return None

    def register_hybrid(self, session, mailbox, ident, order=None, proxy_url=None,
                        link_timeout=300, log=print, think_time=45,
                        verify_success=False, success_timeout=180,
                        send_captcha=False):
        """curl_cffi does the talking; a warm browser only mints the captcha.

        Measured: the warm page mints a genuine token in 0.21-0.29s, against
        roughly 40s when each account loads the mail link in its own browser.
        The captcha does not depend on the registration token, so the page load
        was pure overhead.
        """
        order = order or registrar.DEFAULT_ORDER
        t0 = time.time()
        rnd = random.Random()
        if not proxy_url:
            raise BrowserRegistrationError("hybrid mode needs a proxy")
        relay = self.relays.get(proxy_url)

        registrar.request_verification(session, mailbox.email, log)
        t_verify = time.time() - t0

        link, received = mailbox.find_verification_link(
            timeout=link_timeout, not_before=t0, log=log)
        if not link:
            raise BrowserRegistrationError("verification mail never arrived")
        t_mail = time.time() - t0 - t_verify
        log(f"  [{mailbox.email}] mail {received}")

        # The mail link already carries a usable token, so refresh it through
        # the API rather than loading the whole page: the browser is only
        # needed for the captcha, and that comes from the warm page in ~0.3s.
        token = link.split("token=", 1)[1]
        claims = {}
        try:
            token, claims = registrar.check_verification(session, token, log)
        except Exception as e:
            log(f"  [{mailbox.email}] check-verification: {type(e).__name__} "
                f"{str(e)[:80]}")

        t_browser0 = time.time()
        if send_captcha:
            captcha, browser_ip = self.mint_captcha(proxy_url, ident)
            log(f"  [{mailbox.email}] captcha {len(captcha)} chars "
                f"(warm browser, {time.time() - t_browser0:.2f}s)")
        else:
            captcha, browser_ip = "", None
            log(f"  [{mailbox.email}] captcha omitted on purpose "
                f"(send_captcha is off; a real token makes confirm fail)")
        t_browser = time.time() - t_browser0

        # A real visitor reads the mail, opens the link and fills the form. Do
        # not let the confirm land milliseconds after the verification - that
        # gap is the cheapest thing a backend can measure.
        if think_time > 0:
            gap = rnd.uniform(think_time * 0.6, think_time * 1.4)
            log(f"  [{mailbox.email}] think-time {gap:.0f}s")
            time.sleep(gap)

        # The browser and curl_cffi share one proxy *url*, but a rotating
        # residential upstream hands out an exit per connection - so the
        # captcha can be minted from one IP and the submit sent from another.
        # Measure both and say so, because that mismatch is exactly what a
        # scorer would notice.
        session_ip = registrar.egress_ip(session)
        if browser_ip and session_ip:
            if browser_ip == session_ip:
                log(f"  [{mailbox.email}] egress ok: captcha and submit both via "
                    f"{session_ip}")
            else:
                log(f"  [{mailbox.email}] EGRESS MISMATCH: captcha via {browser_ip}, "
                    f"submit via {session_ip} - this proxy rotates per connection; "
                    f"use a sticky-session proxy or switch to browser mode")
                if self.strict_egress:
                    raise BrowserRegistrationError(
                        f"egress mismatch: captcha {browser_ip} != submit {session_ip}")
        elif browser_ip or session_ip:
            log(f"  [{mailbox.email}] egress: browser={browser_ip} submit={session_ip}")

        url = f"{registrar.RETURN_URL}?token={token}"
        # NOTE: the real SPA fills `ip` from Cloudflare's trace. We deliberately
        # leave it null - measured 2026-09-16, sending the (correct, sticky)
        # exit IP made even the previously-working empty-captcha path fail.
        body = registrar.build_confirm(ident, token, url, order, captcha)
        t_confirm0 = time.time()
        data, text = registrar.confirm(session, body, log)
        t_confirm = time.time() - t_confirm0
        total = time.time() - t0
        log(f"  [{mailbox.email}] confirm -> {text[:60]}")
        if verify_success:
            found, when = mailbox.find_success(
                timeout=success_timeout, not_before=t0, log=log)
            if not found:
                raise BrowserRegistrationError(
                    "confirm returned OK but no Registration Complete mail "
                    "arrived - the address was refused")
            log(f"  [{mailbox.email}] success mail confirmed")
        log(f"  [{mailbox.email}] timing: verify {t_verify:.1f}s | "
            f"mail {t_mail:.1f}s | browser+captcha {t_browser:.1f}s | "
            f"confirm {t_confirm:.1f}s | total {total:.1f}s")
        return {
            "email": mailbox.email,
            "session_id": claims.get("sessionId"),
            "token": token,
            "poll_answer_ids": body["pollAnswerIds"],
            "journey": body["journeyPollAnswers"],
            "response": text,
            "elapsed": round(total, 2),
            "refresh_token": mailbox.refresh_token,
            "captcha_len": len(captcha),
            "mode": "hybrid",
        }

    def register(self, mailbox, ident, order=None, proxy_url=None,
                 link_timeout=300, log=print, verify_success=False,
                 success_timeout=180, send_captcha=True):
        from playwright.sync_api import sync_playwright

        order = order or registrar.DEFAULT_ORDER
        t0 = time.time()
        rnd = random.Random()

        if not proxy_url:
            raise BrowserRegistrationError("browser mode needs a proxy")
        relay = self.relays.get(proxy_url)
        log(f"  [{mailbox.email}] relay {relay.url} -> {proxy_url.split('@')[-1]}")

        with sync_playwright() as pw:
            browser, ctx, page = self._launch(pw, relay.url, rnd, ident)
            try:
                # 1. load the SPA so the page (and its origin) exists
                page.goto(registrar.RETURN_URL, wait_until="domcontentloaded",
                          timeout=self.browser_timeout * 1000)

                # 2. ask for the verification mail, from the page
                res = self._fetch(page, f"{registrar.API}/fan2/verify/verify",
                                  "POST", registrar.build_verify(mailbox.email))
                if res["status"] != 200:
                    raise BrowserRegistrationError(
                        f"verify/verify HTTP {res['status']}: {res['text'][:160]}")
                log(f"  [{mailbox.email}] verify/verify -> {res['text'][:60]}")

                # 3. wait for the link, then open it: this renders the form and
                #    is what makes reCAPTCHA initialise
                url, received = mailbox.find_verification_link(
                    timeout=link_timeout, not_before=t0, log=log)
                if not url:
                    raise BrowserRegistrationError("verification mail never arrived")
                log(f"  [{mailbox.email}] mail {received}")
                page.goto(url, wait_until="domcontentloaded",
                          timeout=self.browser_timeout * 1000)
                page.wait_for_timeout(1500)

                # 4. the mail-link step, re-issuing a token with emailValid=true
                token = url.split("token=", 1)[1]
                res = self._fetch(
                    page,
                    f"{registrar.API}/fan2/verify/check-verification"
                    f"?token={token}&artistId={registrar.ARTIST_ID}"
                    f"&pageId={registrar.PAGE_ID}")
                if res["status"] != 200:
                    raise BrowserRegistrationError(
                        f"check-verification HTTP {res['status']}: {res['text'][:160]}")
                data = json.loads(res["text"])
                claims = data.get("claims") or {}
                if not claims.get("emailValid"):
                    raise BrowserRegistrationError(f"emailValid false: {claims}")
                token = data.get("token") or token
                url = f"{registrar.RETURN_URL}?token={token}"
                log(f"  [{mailbox.email}] emailValid=true "
                    f"session={claims.get('sessionId')}")

                # 5+6. fill the form and let the SPA submit itself.
                #
                # Posting a body we build is not equivalent: the SPA fills in
                # fields we would have to guess (location.county from radar.io,
                # ip from Cloudflare's trace) and its own client signs the
                # captcha. Driving the form is what a real visitor does, and it
                # is the only path that ends on a page we can read back.
                self._drive_form(page, ident, order, log)
                if not self._page_says_registered(page):
                    raise BrowserRegistrationError(
                        "form submitted but the page never confirmed "
                        "the registration")
                log(f"  [{mailbox.email}] page confirms the registration")
                body = {"pollAnswerIds": [], "journeyPollAnswers": []}
                res = {"status": 200, "text": '{"status":"OK"}'}
                captcha = "spa"

                # Browser mode is measured not to send the success mail, so
                # the page's own confirmation is what we go on. It is recorded
                # as `submitted` rather than `registered` so the two kinds of
                # evidence stay distinguishable in the database.
                evidence = "mail" if not verify_success else ""
                if verify_success:
                    found, _when = mailbox.find_success(
                        timeout=success_timeout, not_before=t0, log=log)
                    if found:
                        evidence = "mail"
                        log(f"  [{mailbox.email}] success mail confirmed")
                    elif self._page_says_registered(page):
                        evidence = "page"
                        log(f"  [{mailbox.email}] page confirms the registration "
                            f"(no success mail - expected in browser mode)")
                    else:
                        raise BrowserRegistrationError(
                            "confirm returned OK but neither the success mail "
                            "nor the page confirmed the registration")
                log(f"  [{mailbox.email}] confirm -> {res['text'][:60]} "
                    f"({time.time() - t0:.1f}s)")
                return {
                    "email": mailbox.email,
                    "session_id": claims.get("sessionId"),
                    "token": token,
                    "poll_answer_ids": body["pollAnswerIds"],
                    "journey": body["journeyPollAnswers"],
                    "response": res["text"],
                    "elapsed": round(time.time() - t0, 2),
                    "refresh_token": mailbox.refresh_token,
                    "captcha_len": len(captcha),
                    "mode": "browser",
                    "evidence": evidence,
                }
            finally:
                try:
                    ctx.close()
                    browser.close()
                except Exception:
                    pass
