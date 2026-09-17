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
import hashlib
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

from . import identity as ident_mod
from . import registrar

# Once the page has confirmed the registration, the success mail is a bonus, not
# a gate - browser mode is measured not to send it. Looking for it for the full
# success_timeout costs a minute per account for something that usually never
# arrives, so cap that second look hard.
MAIL_GRACE_AFTER_PAGE = 30

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


def _headless_shell_on_disk():
    """Playwright's chrome-headless-shell, when it is installed.

    Newer Playwright releases ship this as the browser that headless=true
    actually uses: the same engine with the parts headless never needs stripped
    out. It starts with a fraction of the machinery of the full build - notably
    a smaller process tree at startup - which is why it is worth trying when the
    full chromium dies before it ever opens its debug port.
    """
    root = _browsers_root()
    if not os.path.isdir(root):
        return None
    if sys.platform == "darwin":
        return None
    if sys.platform == "win32":
        rel = ("chrome-headless-shell-win64", "chrome-headless-shell.exe")
    else:
        rel = ("chrome-headless-shell-linux64", "chrome-headless-shell")
    for name in sorted(os.listdir(root), reverse=True):
        if not name.startswith("chromium_headless_shell-"):
            continue
        path = os.path.join(root, name, *rel)
        if os.path.exists(path):
            return path
    return None


def _shared_candidates():
    """Every browser worth hand-launching here, best first.

    The full chromium leads because that is what browser mode has always run
    on, so the fingerprint stays the one the site has been seeing. The headless
    shell is the second opinion for hosts where the full build dies during
    startup: same engine and version, much less to go wrong.
    """
    out = []
    full = _chromium_on_disk() or _system_chrome()
    if full:
        out.append(("chromium", full, ["--headless=new"]))
    shell = _headless_shell_on_disk()
    if shell:
        out.append(("chrome-headless-shell", shell, []))
    return out


def _build_stamp():
    """Which build of this file is running, and on what.

    A failure report is only actionable if it says which code produced it - a
    stale image and a genuinely broken host need opposite fixes, and the log
    lines around a crash look identical either way. So the digest of this file,
    the browser binaries present, and the few environment facts that decide
    whether chromium can start at all all travel with the error.
    """
    try:
        path = os.path.abspath(__file__)
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:12]
        when = time.strftime("%m-%d %H:%M",
                             time.localtime(os.path.getmtime(path)))
    except Exception:
        digest, when = "?", "?"
    shm = "?"
    try:
        st = os.statvfs("/dev/shm")
        shm = f"{st.f_bsize * st.f_blocks // (1024 * 1024)}MB"
    except Exception:
        pass
    home = os.path.expanduser("~")
    engines = ",".join(f"{label}:{exe}" for label, exe, _ in _shared_candidates())
    # getuid is Unix-only, and the desktop console runs this same code on Windows.
    uid = getattr(os, "getuid", lambda: -1)()
    return (f"构建={digest}@{when} uid={uid} HOME={home}"
            f"[{'rw' if os.access(home, os.W_OK) else 'ro'}]"
            f" tmp[{'rw' if os.access(tempfile.gettempdir(), os.W_OK) else 'ro'}]"
            f" /dev/shm={shm} 可用内核={engines or '无'}")


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

    def __init__(self, proxy_url, log=None):
        self.proxy_url = proxy_url
        self.log = log or (lambda m: None)
        self.proc = None
        self.port = None
        self._errlog = None
        self._argv = []
        # Which candidate engine last worked here, so a host that refuses one of
        # them does not pay for the refusal on every account.
        self._preferred = None
        self._home_noted = False
        # For eviction: the least recently handed-out browser is the one to
        # close when the cap is reached.
        self.last_used = 0.0
        # RLock: endpoint() holds this while starting, and callers may already
        # hold it (see BrowserRegistrar._shared_browser) to close the race where
        # two workers both see "not started yet" and each launch a browser.
        self._lock = threading.RLock()

    def endpoint(self):
        """Start chromium if needed; return the CDP http endpoint."""
        with self._lock:
            self.last_used = time.time()
            if self.proc and self.proc.poll() is None:
                return f"http://127.0.0.1:{self.port}"
            self._start()
            return f"http://127.0.0.1:{self.port}"

    def _tail_err(self, lines=10):
        """Last few lines chromium wrote before dying."""
        if self._errlog is None:
            return "（进程没起来，没有输出）"
        try:
            self._errlog.flush()
            with open(self._errlog.name, encoding="utf-8", errors="replace") as fh:
                tail = fh.read().strip().split("\n")[-lines:]
            return " | ".join(x.strip()[:200] for x in tail if x.strip()) \
                   or "（没有输出）"
        except Exception as e:
            return f"（读不到：{type(e).__name__}）"

    def _cmd(self):
        return " ".join(self._argv)

    # One retry per candidate: a browser that dies during startup dies in well
    # under a second, so a second attempt costs a moment, and it rules out the
    # transient port/tmp collision before the candidate is written off.
    _START_ATTEMPTS = 2

    def _start(self):
        """Bring up whichever chromium this host will actually run.

        _shared_candidates() is ordered best-first; a candidate that has already
        worked here is tried first. Every failure is collected rather than
        raised immediately, because a host can refuse one build and accept the
        next - and if they all fail, the caller needs the whole list plus the
        environment it happened in, not just the last message.
        """
        if self.proc and self.proc.poll() is None:
            return
        candidates = _shared_candidates()
        if not candidates:
            raise TransientError(
                f"这台机器上没有可用的 chromium | {_build_stamp()}")
        if self._preferred:
            candidates.sort(key=lambda c: c[0] != self._preferred)
        failures = []
        for label, exe, extra in candidates:
            for attempt in range(self._START_ATTEMPTS):
                try:
                    self._launch(exe, extra, label)
                except TransientError as e:
                    failures.append(str(e)[:240])
                    self.log(f"    {label} 起不来（第 {attempt + 1} 次）："
                             f"{str(e)[:200]}")
                    self.stop()          # leave nothing behind for the retry
                    continue
                if label != self._preferred:
                    self.log(f"    browser engine: {label} ({exe})")
                self._preferred = label
                return
        raise TransientError(
            "shared chromium 在本机起不来："
            + " | ".join(failures) + f" || {_build_stamp()}")

    def _launch(self, exe, extra, label):
        """Start one chromium and wait for its debug port.

        Raises TransientError rather than returning a flag: whatever this build
        of chromium is unhappy about says nothing about the account that
        happened to be first in the queue.
        """
        self.port = _free_port()
        profile = tempfile.mkdtemp(prefix="oasis-browser-")
        args = [exe] + extra + [
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            # Playwright's connect_over_cdp asserts on targets it does not
            # recognise, and a component extension's service worker is exactly
            # that - the whole browser connection dies with "Assertion error".
            # Nothing here needs extensions, so keep them out entirely.
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-component-update",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-client-side-phishing-detection",
            "--disable-popup-blocking",
            "--disable-ipc-flooding-protection",
            "--no-service-autorun",
            "--force-color-profile=srgb",
            # The crash handler is what took the browser down on the box this
            # was first deployed to: chromium spawns chrome_crashpad_handler,
            # which died with "--database is required" and took the browser with
            # it as SIGTRAP ("code -5"). These are the switches Playwright's own
            # launch() passes for that reason - launching the process by hand
            # means remembering them.
            "--disable-breakpad",
            "--disable-crash-reporter",
            # A few more of Playwright's defaults that keep a headless browser
            # from being throttled or waiting on things nobody is watching.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-hang-monitor",
            "--disable-prompt-on-repost",
            "--metrics-recording-only",
            "--password-store=basic",
            "--use-mock-keychain",
            # In a container chromium runs as an unprivileged user without the
            # namespaces its sandbox needs, so it dies during startup with
            # SIGTRAP ("exited immediately (code -5)"). Playwright's own
            # launch() adds these; launching the process by hand means adding
            # them here. The browser only ever opens one site.
            "--no-sandbox",
            "--disable-setuid-sandbox",
            # One --disable-features only: chromium keeps the last one it sees,
            # so a second occurrence silently discards the first.
            #
            # OptimizationHints is deliberately NOT in this list. Disabling it
            # segfaults the full chromium during startup - isolated by bisection
            # against the shipped image: that name alone gives "exited
            # immediately (code -11)", every other name in this list is
            # harmless on its own, and the crash disappears the moment it is
            # removed. Nothing here wants optimisation hints switched off
            # anyway; the browser opens one page and is driven by CDP.
            "--disable-features=Translate,BackForwardCache,AcceptCHFrame,"
            "MediaRouter,IsolateOrigins,site-per-process",
        ]
        if self.proxy_url:
            args.append(f"--proxy-server={self.proxy_url}")
        self._argv = [f"--user-data-dir={os.path.basename(profile)}"
                      if a.startswith("--user-data-dir") else a for a in args]
        # Keep chromium's own stderr instead of discarding it: when it dies at
        # startup, that text is the only thing that says why.
        self._errlog = tempfile.NamedTemporaryFile(
            prefix="oasis-chromium-", suffix=".log", delete=False)
        # A read-only $HOME kills the full build before it opens its port, so
        # give the browser one it can write to rather than failing on a host
        # where nothing else is wrong.
        env = _browser_env()
        if env is not None and not self._home_noted:
            self._home_noted = True
            self.log(f"    $HOME={os.environ.get('HOME') or '(未设)'} 不可写"
                     f" —— 浏览器改用 {env['HOME']}")
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=self._errlog, env=env)
        except Exception as e:
            raise TransientError(
                f"cannot start {label} ({exe}): "
                f"{type(e).__name__}: {str(e)[:120]}") from e
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise TransientError(
                    f"{label} exited immediately (code {self.proc.returncode}) "
                    f"cmd={self._cmd()[:200]} :: {self._tail_err()}")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2):
                    pass
            except Exception:
                time.sleep(0.25)
                continue
            # The port answers before the browser websocket accepts clients.
            # Measured: attaching in that window fails with
            # "connect_over_cdp: read ECONNRESET" four times out of four, and
            # the worker that lost that race then tore down a browser the other
            # workers were standing on. A second probe, after a pause, is what
            # separates "the HTTP endpoint is up" from "this browser can be
            # driven" - and it costs a fraction of a second once per process.
            time.sleep(self._PROBE_GAP)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2):
                    self.log(f"    shared browser on :{self.port} "
                             f"(pid {self.proc.pid}, {label}, "
                             f"proxy={self.proxy_url})")
                    return
            except Exception:
                time.sleep(0.25)
        self.stop()
        raise TransientError(
            f"{label} never opened its debug port "
            f"cmd={self._cmd()[:160]} :: {self._tail_err()}")

    # How long to wait between the two readiness probes. Long enough for the
    # browser websocket to start accepting, short enough not to matter.
    _PROBE_GAP = 0.4

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


def _proxy_arg(url):
    """Playwright's proxy dict, from a relay URL.

    Chromium takes the proxy as one --proxy-server string, which is why the
    shared path can pass the URL straight through. launch() wants it split into
    server/username/password, and the relay URL can carry credentials quoted for
    a URL, so they are unquoted here.
    """
    if not url:
        return None
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    if not parts.hostname:
        return {"server": url}
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


def _needs_home_fix():
    """True when $HOME is missing or not writable by this process.

    Chromium keeps its profile, its crash database and its first-run state
    under $HOME, and with a read-only $HOME the full build dies during startup
    with SIGTRAP - "exited immediately (code -5)" - before it ever opens its
    debug port. Docker leaves HOME=/root in place after the entrypoint drops to
    uid 10001, and /root is not writable by that user, which is exactly that
    case. Reproduced by running the shared browser with HOME=/root as uid 10001:
    the full chromium exits -5, the same code reported from the server.
    """
    if sys.platform == "win32":
        # Windows chromium reads USERPROFILE/LOCALAPPDATA, and HOME is normally
        # absent there - treating that as "unwritable" would rewrite the
        # environment of every browser on the desktop build for no reason.
        return False
    home = os.environ.get("HOME") or ""
    return not (home and os.access(home, os.W_OK))


_HOME_FIX = None


def _browser_env():
    """Environment for a hand-launched chromium, or None to inherit as-is.

    The substitute HOME is made once and shared: it is a scratch directory the
    browser only uses for its profile and first-run state, and making a fresh
    one per worker would leave a trail of them behind on a long run.
    """
    global _HOME_FIX
    if not _needs_home_fix():
        return None
    if _HOME_FIX is None:
        _HOME_FIX = tempfile.mkdtemp(prefix="oasis-home-")
    return dict(os.environ, HOME=_HOME_FIX)


# How many times the shared browser may fail before browser mode gives up on it
# and lets every worker launch its own. Two, because one failure is a fluke
# worth rebuilding for and two in a row is the host telling us something.
SOLO_AFTER = 2


# TransientError lives in registrar: both flows raise it, and only one of them
# involves a browser. Re-exported here so existing imports keep working.
from .registrar import RegistrationError, TransientError   # noqa: F401,E402


# Substrings Chromium and the relay use for transport failures. Matched against
# the exception text because Playwright wraps everything in its own types.
_TRANSIENT_MARKERS = (
    "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED",
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_TIMED_OUT", "ERR_NAME_NOT_RESOLVED", "ERR_ADDRESS_UNREACHABLE",
    "ERR_SOCKS_CONNECTION_FAILED", "ERR_EMPTY_RESPONSE",
    "upstream unavailable", "connection closed while reading",
    "ConnectionResetError", "ConnectionRefusedError",
    "Target page, context or browser has been closed",
    "net::ERR_",
)


def is_transient(exc):
    """True when the failure looks like transport rather than a refusal."""
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in _TRANSIENT_MARKERS)


class BrowserRegistrationError(Exception):
    pass


class BrowserRegistrar:
    """One browser session per registration."""

    def __init__(self, relay_pool, headless=True, browser_timeout=90, log=None,
                 max_browsers=3):
        self.relays = relay_pool
        self.headless = headless
        self.browser_timeout = browser_timeout
        self.log = log or (lambda m: None)
        # Each thread gets its own warm context; the chromium process behind
        # them is shared per proxy (see _SharedBrowser), because contexts are
        # what isolate accounts and separate processes cost twice the memory
        # for an identical fingerprint.
        self._shared = {}
        self._shared_lock = threading.Lock()
        # How many workers are standing on each shared browser. A worker that
        # failed to attach must not tear one down while someone is using it.
        self._shared_users = {}
        # Ceiling on live browsers. The engine sets this to the thread count,
        # which is the number of browsers that can actually be in use at once.
        self._max_browsers = max(1, int(max_browsers or 1))
        # Set once the shared scheme has failed often enough to be called
        # broken on this host; every worker then launches its own browser.
        self._solo = False
        self._shared_failures = 0

    # ------------------------------------------------------- warm captcha minting
    _EXEC_JS = """async (k) => await new Promise((res, rej) => {
        window.grecaptcha.enterprise.ready(async () => {
            try { res(await window.grecaptcha.enterprise.execute(k,
                      {action: 'fan_verification'})); }
            catch (e) { rej(String(e)); }
        });
    })"""


    def _shared_browser(self, relay_url):
        """The single browser process for this proxy, guaranteed started.

        Everything slow happens outside the lock: starting chromium takes
        seconds and eviction stops a process (up to ten more), while the lock is
        held by every other worker looking for its own browser. The lock is also
        not reentrant, so calling eviction from inside it deadlocks outright -
        which is exactly what a first attempt at this did.
        """
        with self._shared_lock:
            shared = self._shared.get(relay_url)
            if shared is not None and shared.alive():
                shared.endpoint()
                return shared

        self._evict_excess(keep=relay_url)

        fresh = _SharedBrowser(relay_url, self.log)
        with self._shared_lock:
            existing = self._shared.get(relay_url)
            if existing is not None and existing.alive():
                shared = existing                  # another worker got there first
            else:
                self._shared[relay_url] = fresh
                shared = fresh
        if shared is not fresh:
            try:
                fresh.stop()
            except Exception:
                pass
        shared.endpoint()
        return shared

    def cap_browsers(self, n):
        """Set how many shared browsers may live at once, and trim to it.

        One chromium per *proxy* is the wrong unit - the unit is one per worker,
        because only a worker can be using one at a time. Measured on the
        deployed box: ten proxies with three threads produced ten browsers at
        ~210MB each and took a 3.8GB machine to the edge of OOM. Past the thread
        count the extra browsers buy nothing; they exist only because a
        different upstream happened to be dialled.
        """
        self._max_browsers = max(1, int(n or 1))
        self._evict_excess()

    def _evict_excess(self, keep=None):
        """Close least-recently-used browsers until the cap is met.

        Never closes one a worker is standing on: going over the cap for a
        moment is survivable, killing the browser under someone's feet is not.
        """
        while True:
            with self._shared_lock:
                if len(self._shared) < self._max_browsers:
                    return
                idle = [(sb.last_used, url) for url, sb in self._shared.items()
                        if url != keep and not self._shared_users.get(url)]
                if not idle:
                    return
                _when, url = min(idle)
                shared = self._shared.pop(url, None)
            if shared is None:
                return
            self.log(f"    closing idle browser for {url} "
                     f"(cap {self._max_browsers})")
            try:
                shared.stop()
            except Exception:
                pass


    def close_warm(self):
        """Shut down every shared browser process.

        Named for the warm-browser scheme it started as; what it closes now is
        the shared chromium that browser mode runs every account in.
        """
        with self._shared_lock:
            for shared in self._shared.values():
                shared.stop()
            self._shared.clear()
            self._shared_users.clear()

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

    def _connect_shared(self, relay_url, attempts=2):
        """(playwright, browser, owns_browser) for this worker.

        A shared process is one point of failure for every worker, and a driver
        crash ("Assertion error" from an unexpected target) kills the whole
        playwright session, not just the call. So on failure: throw the shared
        browser away, build a new one, and try once more from a clean
        playwright - losing one account's worth of work beats wedging the run.

        `owns_browser` says whether this worker has to close the browser when it
        is done. On the shared path it must not: the whole point is that the
        browser outlives the account.
        """
        if self._solo:
            return self._launch_solo(relay_url)

        from playwright.sync_api import sync_playwright

        last = None
        for attempt in range(attempts):
            try:
                shared = self._shared_browser(relay_url)
                pw = sync_playwright().start()
            except TransientError as e:
                # Chromium itself would not start. Rebuilding cannot fix a host
                # that refuses to run it, so stop burning attempts.
                last = e
                self._shared_failures += 1
                if self._shared_failures >= SOLO_AFTER:
                    self.log("    shared browser 在本机起不来（连续 "
                             f"{self._shared_failures} 次）——改为每个 worker "
                             "自己开一个浏览器")
                    self._solo = True
                    return self._launch_solo(relay_url)
                raise
            try:
                browser = pw.chromium.connect_over_cdp(shared.endpoint())
            except Exception as e:
                last = e
                try:
                    pw.stop()
                except Exception:
                    pass
                # One worker failing to attach says nothing about whether the
                # browser is usable - and it is shared. Tearing it down here is
                # what turned a single bad attach into "every worker's page
                # disappeared at once": measured on the deployed run, two
                # workers each rebuilt the browser twice in twelve seconds and
                # the third died with "Target page, context or browser has been
                # closed" every time. So the browser is only discarded when it
                # is actually dead or has nobody on it, and the attach itself is
                # simply retried.
                self._retire_if_idle(relay_url)
                if attempt < attempts - 1:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                break
            with self._shared_lock:
                self._shared_users[relay_url] = \
                    self._shared_users.get(relay_url, 0) + 1
            return pw, browser, False

        # Every attempt to attach failed against a browser that is alive. That
        # is the shared scheme failing, not this account.
        self._shared_failures += 1
        if self._shared_failures >= SOLO_AFTER:
            self.log("    shared browser 接不上（连续 "
                     f"{self._shared_failures} 次）——改为每个 worker 自己开一个"
                     "浏览器：慢一些、更吃内存，但能跑完")
            self._solo = True
            return self._launch_solo(relay_url)
        if isinstance(last, TransientError):
            raise last
        raise BrowserRegistrationError(f"could not attach to a browser: {last}")

    def _retire_if_idle(self, relay_url):
        """Throw away the shared browser for this relay, but only if nobody is
        on it.

        A worker that failed to attach has no claim on the browser every other
        worker is using, so this is deliberately conservative: a browser that is
        still running with a live user is left alone, and the caller retries
        instead.
        """
        with self._shared_lock:
            if self._shared_users.get(relay_url, 0) > 0:
                return
            shared = self._shared.pop(relay_url, None)
        if shared is not None:
            try:
                shared.stop()
            except Exception:
                pass

    def _release_shared(self, relay_url):
        """This worker is done with the shared browser for this relay."""
        with self._shared_lock:
            n = self._shared_users.get(relay_url, 0)
            if n <= 1:
                self._shared_users.pop(relay_url, None)
            else:
                self._shared_users[relay_url] = n - 1

    def _launch_solo(self, relay_url):
        """One browser for this worker, started by Playwright itself.

        The fallback for hosts where the hand-launched shared chromium dies
        before opening its debug port. launch() carries its own argument list
        and its own crash-handler handling, so it comes up in places our own
        list does not - in a container too, which is the case that matters.
        What it costs is a browser process per worker instead of one per proxy,
        so it is the fallback and not the default.
        """
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        kwargs = {}
        fixed = _browser_env()
        if fixed is not None:
            # Same reason as the shared path: a read-only $HOME kills chromium
            # during startup, whichever way it was started.
            kwargs["env"] = {"HOME": fixed["HOME"]}
        try:
            browser = pw.chromium.launch(
                headless=True,
                proxy=_proxy_arg(relay_url),
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage"],
                **kwargs,
            )
        except Exception as e:
            try:
                pw.stop()
            except Exception:
                pass
            raise TransientError(
                f"playwright launch() 也起不来浏览器：{type(e).__name__}: "
                f"{str(e)[:200]} || {_build_stamp()}") from e
        return pw, browser, True

    def _new_context(self, browser, rnd, ident):
        """An isolated context for one account on a shared browser.

        The viewport, user agent, timezone and stealth patch are all drawn per
        account, so two accounts on the same chromium still look like two
        different machines to the site. Cookies, storage and cache are isolated
        by the context, which is the part that actually matters.
        """
        country = (ident.get("location") or {}).get("countryCode", "US")
        ctx = browser.new_context(
            viewport=rnd.choice(VIEWPORTS),
            user_agent=self._user_agent(rnd),
            locale="en-US",
            timezone_id=rnd.choice(TIMEZONES.get(country, TIMEZONES["US"])),
            device_scale_factor=1,
        )
        # patch the signals Chromium leaks in headless before any page script runs
        ctx.add_init_script(self._stealth_js(rnd))
        ctx.set_default_timeout(self.browser_timeout * 1000)
        return ctx


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
    @staticmethod
    def _settle(page, condition, timeout_ms=3000):
        """Wait for a condition, but only as long as it actually takes.

        Every fixed wait_for_timeout in this file was a guess at how long the
        SPA needs; measured, its DOM is ready in about a second. Waiting on the
        element the next step needs returns as soon as it appears, so a fast
        machine stops paying for a slow one's timings.
        """
        try:
            page.wait_for_function(condition, timeout=timeout_ms)
            return True
        except Exception:
            return False

    @staticmethod
    def _await_options(page, timeout_ms=4000):
        """Wait for a dropdown's options to render. True when some appeared."""
        try:
            page.wait_for_selector("[role=option]", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _fill_details(self, page, ident, log):
        """Step 1. Typing, not fill(): these are custom components and fill()
        does not raise the input events they listen for (the phone field then
        reports "Please enter a valid phone number")."""
        for sel, val in (("#soundcheckConfirmFirstName", ident["first_name"]),
                         ("#soundcheckConfirmLastName", ident["last_name"])):
            page.fill(sel, val)
            # fill() dispatches the input event React listens for; confirming
            # the value landed beats sleeping on a guess.
            self._settle(page, f"() => document.querySelector({sel!r}).value"
                               f" === {val!r}", 1200)
        phone = ident["phone"]
        for attempt in range(4):
            if attempt:
                # Retrying the same number never worked - measured, 12 attempts
                # across the per-field and per-step loops all failed on the same
                # value. Whatever the form dislikes is about that number, so
                # draw a new one instead of repeating it.
                phone = ident_mod.random_phone(random.Random(),
                                               (ident.get("location") or {})
                                               .get("countryCode", "US"))
                log(f"    phone retry {attempt + 1} with a fresh number")
            field = page.query_selector("input#soundcheckConfirmPhoneNumber")
            field.click()
            # select-all + delete, not fill(""): the component tracks its own
            # model and a plain clear can leave the old value in it.
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(300)
            page.type("input#soundcheckConfirmPhoneNumber", phone, delay=110)
            typed = page.input_value("#soundcheckConfirmPhoneNumber")
            page.keyboard.press("Tab")
            # Either the error shows up (fast) or it never does; either way we
            # are done in ~0.7s instead of always paying 1.2s.
            #
            # Both messages matter, and only one was checked before. Measured on
            # a live run: with the form freshly reloaded the component sometimes
            # drops the value again, and the page then says "Phone number is
            # required" - which this probe did not look for, so it reported
            # success and the whole details step died one step later.
            bad = self._settle(
                page, "() => {const t = document.body.innerText;"
                      "return t.includes('Please enter a valid phone number')"
                      " || t.includes('Phone number is required');}", 700)
            if not bad:
                break
            log(f"    phone attempt {attempt + 1} rejected "
                f"(field holds {typed!r}), retrying")
        # The date picker is custom: a text input that ignores typing, plus two
        # select-style triggers and a day grid. Reopening it is the only reliable
        # retry, and the field has to be non-empty or Continue silently stalls.
        for attempt in range(4):
            if page.input_value("#birthDate"):
                break
            page.click("#birthDate")
            self._await_options(page, 3000)          # the calendar has rendered
            for tid, val in (("#year", str(ident["dob_year"])),
                             ("#month", ident["dob_month"])):
                try:
                    page.click(tid, force=True, timeout=6000)
                    self._await_options(page, 2500)
                    page.click(f'[role=option]:has-text("{val}")', timeout=4000)
                    page.wait_for_timeout(250)
                except Exception:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
            cal = page.query_selector("[data-testid=calendar]")
            if cal:
                for cell in cal.query_selector_all("button, [role=gridcell], td"):
                    if (cell.inner_text() or "").strip() == str(ident["dob_day"]):
                        try:
                            cell.click()
                            break
                        except Exception:
                            pass
            self._settle(page, "() => !!document.querySelector('#birthDate').value",
                         1200)
            if not page.input_value("#birthDate"):
                log(f"    birthdate attempt {attempt + 1} did not take, reopening")
        for attempt in range(3):
            page.click("input#soundcheckConfirmLocation")
            page.fill("input#soundcheckConfirmLocation", "")
            page.wait_for_timeout(250)
            page.type("input#soundcheckConfirmLocation", ident["location_query"],
                      delay=90)
            # radar.io's autocomplete answers in well under a second; the old
            # 4.2s blind wait was the single biggest waste in the flow.
            if self._await_options(page, 6000):
                page.query_selector_all("[role=option]")[0].click()
                page.wait_for_timeout(400)
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

    @staticmethod
    def _recaptcha_ready(page):
        """Is the page's reCAPTCHA client actually loaded and usable?

        The SPA needs a token from it before it will leave the details step, and
        when the script never arrived the page simply sits there. Checked before
        blaming the form.
        """
        try:
            return bool(page.evaluate(
                "() => !!(window.grecaptcha && window.grecaptcha.enterprise"
                " && typeof window.grecaptcha.enterprise.execute === 'function')"))
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
        self._await_options(page, 3000)
        for opt in page.query_selector_all("[role=option]"):
            if want and want in (opt.inner_text() or "").lower():
                try:
                    opt.click()
                    page.wait_for_timeout(350)
                    return True
                except Exception:
                    return False
        page.keyboard.press("Escape")
        return False

    def _drive_form(self, page, ident, order, log, before_submit=None,
                    mail_url=None):
        """Walk the whole SPA form and let the SPA submit itself.

        The API calls are the site's own, so the body carries everything a real
        visitor sends - county, ip, consentMessaging - which a hand-built body
        cannot reproduce. Only the page's confirmation is available afterwards:
        browser mode is measured not to send the success mail.
        """
        page.wait_for_selector("#soundcheckConfirmFirstName", timeout=90000)
        # Let the reCAPTCHA client arrive before touching the form.
        #
        # It loads asynchronously, and the page will not leave this step without
        # it. Checking only after a failed Continue means judging it at the worst
        # possible moment - the client may simply not have finished loading, and
        # the account gets blamed for a slow script. Waiting here also gives the
        # "reCAPTCHA never loaded" diagnosis a fair chance to be true.
        deadline = time.time() + 25
        while time.time() < deadline and not self._recaptcha_ready(page):
            page.wait_for_timeout(500)
        log(f"    recaptcha ready: {self._recaptcha_ready(page)}")
        last_shown = []
        for attempt in range(2):
            if attempt and mail_url:
                # Start the step over on a freshly loaded page. Measured: the
                # phone field accepts anything (8 of 8 numbers, including
                # 0005551234), and a controlled probe never reproduced the
                # error - the difference is that the probe reloaded between
                # tries. On the same page the validation message appears to
                # stick, which is why 12 retries in place never got past it.
                page.goto(mail_url, wait_until="domcontentloaded",
                          timeout=self.browser_timeout * 1000)
                page.wait_for_selector("#soundcheckConfirmFirstName", timeout=60000)
                # A fresh number on the outer retry too. The inner loop does draw
                # new ones, but only once it decides the field is unhappy - and
                # measured, the page sometimes says so only after Continue, by
                # which point the inner loop has already finished and this one is
                # about to hand the site the same number it just refused.
                if "Please enter a valid phone number" in last_shown:
                    ident = dict(ident)
                    ident["phone"] = ident_mod.random_phone(
                        random.Random(),
                        (ident.get("location") or {}).get("countryCode", "US"))
                    log(f"    retrying with a fresh number {ident['phone']}")
            self._fill_details(page, ident, log)
            if self._click_continue(page, until="cities", timeout=60):
                break
            body = page.inner_text("body")
            shown = [m for m in ("Please enter a valid phone number",
                                 "Location is required", "Phone number is required",
                                 "Name is required", "Date of birth is required")
                     if m in body]
            last_shown = shown
            # A form that neither advances nor complains is nearly always a
            # reCAPTCHA that never loaded: the page needs a token before it will
            # move on, and it says nothing at all when it cannot get one. Caught
            # live - the console showed "reCAPTCHA script failed to load -
            # marked as blocked by content blocker" and window.grecaptcha was
            # undefined while every field held the right value and the Continue
            # button was enabled. Without this check the whole thing reads as
            # "the form silently refuses", which sends you looking at the phone
            # field for a day.
            if not shown and not self._recaptcha_ready(page):
                raise TransientError(
                    "reCAPTCHA 没有加载，页面因此不会前进（字段都填对了，"
                    "Continue 也可点，但页面需要 captcha token 才肯走）。"
                    "说明 Google 在当前出口上不可达 —— "
                    "给「Google 分流代理」填一个能访问 www.google.com 的出口。"
                    "这不是账号的问题，账号未消耗。")
            log(f"    details rejected ({shown or 'no message'}), "
                f"retry {attempt + 1} on a fresh page")
            if attempt == 1:
                # The field accepts anything - measured, 8 of 8 numbers went
                # through including 0005551234 - and the same identity succeeds
                # on a later run, so this is the site refusing intermittently
                # rather than anything about this account. Report it as
                # transport so the engine requeues instead of burning it.
                raise TransientError(
                    f"details step did not advance; page says {shown or 'nothing'}")

        picked = []
        for venue in list(order)[:3]:
            if self._pick_show(page, venue, log):
                picked.append(venue)
        if not picked:
            raise BrowserRegistrationError("no venue could be selected")
        for cb in page.query_selector_all("[role=combobox]"):
            if "Select an option" in (cb.inner_text() or ""):
                cb.click()
                self._await_options(page, 3000)
                opts = page.query_selector_all("[role=option]")
                if opts:
                    opts[-1].click()            # travel: No
                    page.wait_for_timeout(350)
                break
        self._click_continue(page, until="question", timeout=60)
        log(f"    {len(picked)} venue(s) selected: {', '.join(picked)}")

        album = ""
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
                        self._await_options(page, 3000)
                        opts = page.query_selector_all("[role=option]")
                        log(f"      album options: {len(opts)}")
                        if opts:
                            album = (opts[0].inner_text() or "").strip()
                            log(f"      picking {album[:26]!r}")
                            opts[0].click()
                            page.wait_for_timeout(350)
                        break
            self._click_continue(page, until="terms", timeout=60)

        # The T&C page will not submit until the terms have been scrolled to the
        # end; the button does nothing (and reports nothing) otherwise.
        if "click the submit button below" not in page.inner_text("body"):
            raise BrowserRegistrationError("never reached the terms step")
        scroll = page.query_selector("button:has-text('Scroll to bottom')")
        if scroll:
            scroll.click()
            # Submit is *not* disabled before the scroll - measured, both buttons
            # read disabled=False from the start, and the site just ignores the
            # click until its own scroll position reaches the end. So waiting on
            # the button (which is what this used to do) returns immediately and
            # the submit silently does nothing. Watch the scroll instead: the
            # terms live in the tallest scrollable element on the page.
            done = self._settle(page, """() => {
                const els = [...document.querySelectorAll('*')].filter(e => {
                  const s = getComputedStyle(e);
                  return e.scrollHeight > e.clientHeight + 40
                         && /auto|scroll/.test(s.overflowY);
                }).sort((a, b) => b.scrollHeight - a.scrollHeight);
                const e = els[0];
                return !e || e.scrollTop + e.clientHeight >= e.scrollHeight - 24;
            }""", 6000)
            if not done:
                # condition missed (different markup, or nothing scrollable):
                # fall back to the delay that is known to work
                page.wait_for_timeout(2500)
        # Extension point: the only moment a caller can still change something
        # (a captcha source, say) after the page is fully rendered but before
        # the SPA builds and sends its request.
        if before_submit:
            before_submit(page)

        submit = page.query_selector("button:has-text('Submit')")
        if not submit:
            raise BrowserRegistrationError("no submit button on the T&C page")
        submit.click()
        try:
            page.wait_for_function(
                "() => document.body.innerText.toLowerCase()"
                ".includes('thanks for registering')", timeout=90000)
        except Exception as e:
            # What the page is showing right now is the only evidence of why the
            # submit did not land - a reCAPTCHA that never loaded looks exactly
            # like a rejected submission otherwise, and both look like a bare
            # timeout. Carry it out in the error instead of losing it.
            raise BrowserRegistrationError(
                "submitted but the page never said 'thanks for registering' "
                "within 90s; page says: "
                + " ".join(page.inner_text("body").split())[:400]) from e
        page.wait_for_timeout(1500)
        # Hand back what was actually chosen. The SPA builds its own request
        # body, so this is the only record of the answers; without it the row
        # lands in the database with an empty poll_answer_ids.
        return {"venues": picked, "album": album}



    def register(self, mailbox, ident, order=None, proxy_url=None,
                 link_timeout=300, log=print, verify_success=False,
                 success_timeout=180, mail_since=None):
        """Drive the official SPA through one registration.

        `mail_since` is the epoch the verification mail was requested at. A
        caller that already asked for it (the engine prefetches one account
        ahead) passes that timestamp; otherwise this sends the request itself.
        """
        from playwright.sync_api import sync_playwright

        order = order or registrar.DEFAULT_ORDER
        t0 = time.time()
        rnd = random.Random()

        if not proxy_url:
            raise BrowserRegistrationError("browser mode needs a proxy")
        relay = self.relays.get(proxy_url)
        log(f"  [{mailbox.email}] relay {relay.url} -> {proxy_url.split('@')[-1]}")

        # One chromium serves every worker (see _SharedBrowser). Doing this per
        # account instead - launching a fresh browser each time - costs 2-4s of
        # startup plus a ~220MB process, and buys no fingerprint diversity:
        # measured across three separate browsers, the canvas hash and WebGL
        # string came out identical, because the fingerprint comes from the
        # Chromium build and the machine, not the process.
        pw, browser, owns_browser = self._connect_shared(relay.url)
        ctx = None
        try:
            ctx = self._new_context(browser, rnd, ident)
            page = ctx.new_page()
            self.log(f"    context on "
                     f"{'this worker' if owns_browser else 'shared'} "
                     f"browser via {relay.url}")
            # 1. ask for the verification mail over curl.
            #
            #    The submit is what must come from the browser: measured, a real
            #    captcha sent through curl_cffi never completed, while the same
            #    flow submitted by the SPA always does. The verify call is a
            #    different matter - the old hybrid flow sent it with curl and
            #    the mail arrived normally every time - so sending it here is
            #    free of the risk that killed hybrid, and it buys two things:
            #
            #      * the bare /registration page load disappears (it only
            #        existed to give the verify fetch an origin)
            #      * the mail can be requested ahead of time, so the wait moves
            #        off the worker's critical path
            if mail_since is None:
                session = registrar.make_session(
                    relay.url, timeout=self.browser_timeout)
                try:
                    registrar.request_verification(
                        session, mailbox.email,
                        lambda m: log(f"  [{mailbox.email}]{m}"))
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
                mail_since = time.time()

            # 2. wait for the link, then open it: this renders the form and
            #    is what makes reCAPTCHA initialise
            url, received = mailbox.find_verification_link(
                timeout=link_timeout, not_before=mail_since, log=log)
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
            if claims.get("closed"):
                # The form will render without ever accepting a submission, and
                # the page says nothing about why - see RegistrationClosed.
                raise registrar.RegistrationClosed(
                    f"站点已关闭该会话（closed=true, "
                    f"session={claims.get('sessionId')}）：该地址已处理过，"
                    f"或站点因重复请求作废了它")
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
            driven = self._drive_form(page, ident, order, log, mail_url=url)
            if not self._page_says_registered(page):
                raise BrowserRegistrationError(
                    "form submitted but the page never confirmed "
                    "the registration")
            log(f"  [{mailbox.email}] page confirms the registration")
            # The SPA submits its own body, so record what we chose here -
            # otherwise the row lands in the database with nothing but the
            # account and a fixed artist id.
            venues = (driven or {}).get("venues") or []
            body = {"pollAnswerIds": [registrar.SHOWS[v][0]
                                      for v in venues if v in registrar.SHOWS],
                    "journeyPollAnswers": [{"venue": v} for v in venues]}
            res = {"status": 200, "text": '{"status":"OK"}'}
            captcha = "spa"

            # Judge the page first, because we are standing on it. Browser mode
            # is measured not to send the success mail, so waiting the full mail
            # budget before looking at the page spends a minute per account on
            # something that usually never arrives.
            page_ok = self._page_says_registered(page)
            if page_ok:
                log(f"  [{mailbox.email}] page confirms the registration")
                evidence = "page"
                # The mail is an upgrade from here, not a gate: look briefly and
                # take it if it shows up, otherwise move on.
                if verify_success:
                    found, _when = mailbox.find_success(
                        timeout=min(success_timeout, MAIL_GRACE_AFTER_PAGE),
                        not_before=t0, log=log)
                    if found:
                        evidence = "mail"
                        log(f"  [{mailbox.email}] success mail also arrived")
            else:
                # No page confirmation, so the mail is the only evidence left and
                # it is worth the whole budget before calling this a failure.
                found, _when = mailbox.find_success(
                    timeout=success_timeout, not_before=t0, log=log) \
                    if verify_success else (False, None)
                if found:
                    evidence = "mail"
                    log(f"  [{mailbox.email}] success mail confirmed")
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
        except (TransientError, BrowserRegistrationError):
            raise
        except Exception as e:
            # Playwright wraps everything, so decide by content: if it smells
            # like transport, the account is untouched and goes back in the
            # queue instead of being written off as failed.
            if is_transient(e):
                raise TransientError(str(e)[:200]) from e
            raise
        finally:
            # Only this worker's context. The shared browser outlives the
            # account on purpose - that is what makes it warm. A solo browser
            # belongs to this worker alone, so it goes too.
            try:
                if ctx:
                    ctx.close()
            except Exception:
                pass
            if owns_browser:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                # Hand the shared browser back before tearing down the session,
                # so another worker's failed attach does not decide this one is
                # idle and throw it away underneath them.
                self._release_shared(relay.url)
            try:
                pw.stop()
            except Exception:
                pass
