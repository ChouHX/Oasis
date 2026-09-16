#!/usr/bin/env python3
"""Oasis Live '27 — registration automation (oasis.hq.fan).

Full pipeline
-------------
  1. /registration            email + email-consent toggle -> "Get started"
  2. "Check your email"       verification mail read over Microsoft Graph
  3. token link               "Continue Your Registration":
                                first/last name, date of birth, location, phone
  4. polls step               ranked city select (<=3) + travel-package poll
  5. album step               "Which Oasis album was released in 1995?"
  6. terms step               T&Cs -> "Scroll to bottom" -> "Submit"
  7. confirmation

Widget notes (all discovered from the live DOM)
-----------------------------------------------
  * location   reka-ui combobox backed by api.radar.io autocomplete;
               options render as [role=option] ("New York, New York, United States")
  * birth date reka-ui calendar popover (input is aria-readonly), opened by
               clicking the wrapping div.input; month/year are custom selects
               (#month / #year) and days are cells with aria-label
               "Friday, March 16, 2000". It opens at min-age 18 (Sept 2008).
  * cities     one "Select show..." dropdown per preference, revealed in turn;
               option labels look like "August 2027 | Boston | US"
  * polls      "Select an option..." dropdowns (Yes/No, and the album list)
  * terms      "Scroll to bottom" then "Submit" — this is the irreversible step

Chrome cannot authenticate to SOCKS5, so --proxy routes through the local relay
(relay.py on 127.0.0.1:8899) which chains to the authenticated upstream.
"""
import argparse, calendar, json, os, re, sys, time, urllib.parse

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from graph_mail import parse_cred, refresh, graph_get  # noqa: E402
import fingerprint  # noqa: E402

RELAY = "http://127.0.0.1:8899"
SITE = "https://oasis.hq.fan/registration"
STATE = "/tmp/oasis_state.json"

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--disable-default-apps", "--disable-breakpad",
    "--no-first-run", "--no-default-browser-check", "--metrics-recording-only",
    "--lang=en-US",
    # a proxied residential egress must not be undercut by a WebRTC leak of
    # the host's real network address
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-features=Translate,OptimizationHints,MediaRouter,"
    "ChromeWhatsNewUI,AutofillServerCommunication",
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def save_state(d):
    st = load_state(); st.update(d)
    json.dump(st, open(STATE, "w"), indent=1)


# --------------------------------------------------------------------------- #
# mailbox (Microsoft Graph)
# --------------------------------------------------------------------------- #
TOKEN_RE = re.compile(r"https://oasis\.hq\.fan/registration\?token=[A-Za-z0-9._\-]+")


def find_verify_link(cred, proxy=None, timeout=1800, interval=20, since=None):
    """Poll the mailbox for the registration link (the site says up to 1 hour).

    `since` is an epoch seconds cutoff: messages received before it are ignored,
    so a fresh run cannot latch onto a previously delivered link.
    """
    at = refresh(cred, proxy)["access_token"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = graph_get(
                "/me/messages?$top=25&$select=subject,from,receivedDateTime,body",
                at, proxy)
        except Exception as e:
            log(f"  graph error: {type(e).__name__} {e}")
            time.sleep(interval); continue
        for m in data.get("value", []):
            rcv = m.get("receivedDateTime") or ""
            if since:
                try:
                    ts = calendar.timegm(time.strptime(rcv, "%Y-%m-%dT%H:%M:%SZ"))
                    if ts < since - 120:            # 2 min slack for clock skew
                        continue
                except Exception:
                    pass
            body = (m.get("body") or {}).get("content", "") or ""
            flat = re.sub(r"=\r?\n", "", body).replace("&amp;", "&")
            hit = TOKEN_RE.search(flat)
            if hit:
                log(f"  mail: {m.get('subject')} @ {rcv}")
                return hit.group(0)
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------- #
# browser
# --------------------------------------------------------------------------- #
def parse_proxy(url):
    """http://user:pass@host:port -> Playwright proxy dict (native auth)."""
    u = urllib.parse.urlparse(url)
    if not u.hostname:
        raise ValueError(f"bad proxy url: {url!r}")
    cfg = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = urllib.parse.unquote(u.username)
        cfg["password"] = urllib.parse.unquote(u.password or "")
    return cfg


def launch(p, a):
    """Persistent context carrying one coherent random desktop fingerprint."""
    if a.proxy_url:
        proxy = parse_proxy(a.proxy_url)
    elif a.proxy:
        proxy = {"server": RELAY}
    else:
        proxy = None

    fp = fingerprint.make(seed=a.fp_seed, os_family=a.fp_os)
    log(f"fingerprint: {fingerprint.describe(fp)}")
    log(f"  ua:    {fp['ua']}")
    log(f"  proxy: {proxy['server'] if proxy else 'direct'}")

    kw = {"proxy": proxy} if proxy else {}
    ctx = p.chromium.launch_persistent_context(
        a.profile, headless=a.headless, channel="chrome", **kw,
        viewport=fp["viewport"],
        screen={"width": fp["screen"]["w"], "height": fp["screen"]["h"]},
        device_scale_factor=fp["screen"]["dpr"],
        user_agent=fp["ua"],
        locale=fp["locale"], timezone_id=fp["timezone"], args=CHROME_ARGS)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    fingerprint.apply(ctx, page, fp)
    if a.fp_json:
        json.dump(fp, open(a.fp_json, "w"), indent=1)
        log(f"  fingerprint saved -> {a.fp_json}")
    return ctx, page, fp


DUMP_JS = """() => {
  const dig = (sel) => { const r=[]; const w=(x)=>{x.querySelectorAll(sel)
      .forEach(e=>r.push(e)); x.querySelectorAll('*').forEach(el=>{
      if(el.shadowRoot) w(el.shadowRoot);});}; w(document); return r; };
  const o = {inputs:[], buttons:[], options:[], text:'', url:location.href};
  dig('input').forEach(e=>o.inputs.push({id:e.id,name:e.name,type:e.type,
    val:e.value, ro:e.getAttribute('aria-readonly')}));
  dig('button').forEach(e=>o.buttons.push({text:(e.innerText||'').trim().slice(0,55),
    dis:e.disabled}));
  dig('[role=option]').forEach(e=>o.options.push(
    (e.innerText||'').trim().slice(0,70)));
  o.text = document.body ? document.body.innerText.slice(0,2500) : '';
  return o;
}"""


def dump(page, tag):
    info = page.evaluate(DUMP_JS)
    print("=" * 72)
    print(f"DUMP[{tag}]")
    print(info.pop("text"))
    print(json.dumps(info, indent=1, ensure_ascii=False)[:3000])
    print("=" * 72)
    return info


# --------------------------------------------------------------------------- #
# widgets
# --------------------------------------------------------------------------- #
def fill_location(page, query, pick_contains):
    """Radar.io-backed combobox: type, wait for options, click the match."""
    box = page.locator("input[data-testid='combobox-input']")
    box.click(); page.wait_for_timeout(300)
    box.fill(""); page.wait_for_timeout(300)
    box.type(query, delay=110)
    page.wait_for_selector("[role=option]", timeout=25000)
    page.wait_for_timeout(1200)
    opts = page.locator("[role=option]")
    for i in range(opts.count()):
        t = (opts.nth(i).inner_text() or "").strip()
        if pick_contains.lower() in t.lower():
            opts.nth(i).click(); page.wait_for_timeout(900)
            log(f"  location -> {t}")
            return t
    opts.first.click(); page.wait_for_timeout(900)
    log("  location -> first option (no exact match)")
    return opts.first.inner_text()


def _open_dob_calendar(page, cal):
    for attempt in range(4):
        page.keyboard.press("Escape")          # dismiss any lingering popover
        page.wait_for_timeout(500)
        page.locator("div.input:has(#birthDate)").first.click()
        try:
            cal.wait_for(state="visible", timeout=6000)
            return True
        except Exception:
            log(f"  dob popover did not open (attempt {attempt + 1})")
    return False


def _cal_state(page):
    """Read the calendar's month/year triggers and day-cell labels, if present."""
    return page.evaluate("""() => {
      const c = document.querySelector('[data-testid=calendar]');
      if (!c) return null;
      const trigs = {};
      c.querySelectorAll('[data-testid=select-trigger]')
        .forEach(e => trigs[e.id] = (e.innerText || '').trim());
      const cells = [...c.querySelectorAll('[role=gridcell]')]
        .map(x => x.getAttribute('aria-label')).filter(Boolean);
      return {ariaLabel: c.getAttribute('aria-label'), trigs,
              cellCount: cells.length, first: cells[0], last: cells[cells.length-1]};
    }""")


def set_dob(page, day, month_name, year):
    """reka-ui calendar popover: month/year selects, then the day cell.

    Selecting month/year can close the popover (and the view can reset), so
    verify the calendar's actual state and retry until the input reads back
    the wanted date.
    """
    cal = page.locator("[data-testid=calendar]")
    label = f"{month_name} {day}, {year}"
    if not _open_dob_calendar(page, cal):
        raise RuntimeError("date-of-birth calendar never opened")
    log(f"  dob calendar open: {_cal_state(page)}")

    for attempt in range(5):
        st = _cal_state(page)
        if not st:
            log(f"  dob: calendar gone (attempt {attempt + 1}), reopening")
            if not _open_dob_calendar(page, cal):
                raise RuntimeError("date-of-birth calendar would not reopen")
            st = _cal_state(page)

        if st["trigs"].get("month") != month_name or st["trigs"].get("year") != str(year):
            log(f"  dob: view is {st['trigs']}, selecting {month_name} {year}")
            cal.locator("[data-testid=select-trigger]#month").click()
            page.wait_for_timeout(900)
            page.get_by_role("option", name=month_name, exact=True).first.click()
            page.wait_for_timeout(900)
            cal = page.locator("[data-testid=calendar]")
            if not _cal_state(page):                       # popover may close
                log("  dob: calendar closed after month, reopening")
                if not _open_dob_calendar(page, cal):
                    continue
            cal.locator("[data-testid=select-trigger]#year").click()
            page.wait_for_timeout(900)
            page.get_by_role("option", name=str(year), exact=True).first.click()
            page.wait_for_timeout(1400)
            if not _cal_state(page):                       # popover may close
                log("  dob: calendar closed after year, reopening")
                if not _open_dob_calendar(page, cal):
                    continue

        cell = page.locator(f"[aria-label*='{label}']")
        n = cell.count()
        if n == 0:
            log(f"  dob attempt {attempt + 1}: day cell not rendered, "
                f"state={_cal_state(page)}")
            continue
        cell.first.click()
        page.wait_for_timeout(1000)

        got = page.locator("#birthDate").input_value()
        # the widget renders ordinals ("March 16th, 2000"), so check components
        if (month_name in got and str(year) in got
                and re.search(rf"\b{day}(st|nd|rd|th)?\b", got)):
            log(f"  dob -> {got}")
            return got
        log(f"  dob attempt {attempt + 1} read back {got!r}, retrying")
    raise RuntimeError(f"could not set date of birth to {label}")


def pick_dropdown(page, trigger_text, match, label, timeout=20000):
    """Click a 'Select ...' trigger then the option matching `match`."""
    trig = page.locator(f"button:has-text('{trigger_text}')")
    if trig.count() == 0:
        raise RuntimeError(f"{label}: no '{trigger_text}' trigger on page")
    trig.first.click()
    page.wait_for_selector("[role=option]", timeout=timeout)
    page.wait_for_timeout(1200)
    opts = page.locator("[role=option]")
    seen = []
    for i in range(opts.count()):
        t = (opts.nth(i).inner_text() or "").strip()
        seen.append(t.replace("\n", " | "))
        if re.search(match, t, re.I):
            opts.nth(i).click(); page.wait_for_timeout(1400)
            log(f"  {label} -> {t.replace(chr(10), ' | ')}")
            return t
    raise RuntimeError(f"{label}: no option matching {match!r} in {seen}")


def click_continue(page, label="Continue"):
    btn = page.locator(f"button:has-text('{label}')")
    if btn.count() == 0:
        raise RuntimeError(f"no '{label}' button")
    btn.first.click()
    page.wait_for_timeout(8000)
    log(f"  clicked {label}")


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
EGRESS_JS = """async () => {
  try {
    const r = await fetch('https://api.ipify.org?format=json&cb=' + Date.now(),
                          {cache: 'no-store'});
    return (await r.json()).ip;
  } catch (e) { return 'ERR ' + e.message; }
}"""


def check_egress(page):
    """Print the browser's real egress: a host IP here means the proxy leaked."""
    ip = page.evaluate(EGRESS_JS)
    log(f"  browser egress ip: {ip}")
    return ip


def phase_email(page, a):
    log("opening registration page")
    page.goto(SITE, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_selector("#soundcheckEmail", timeout=60000)
    check_egress(page)

    toggle = page.locator("button[aria-label*='receive updates from Oasis']")
    if toggle.count():
        st = toggle.first.get_attribute("aria-checked") or \
             toggle.first.get_attribute("data-state")
        if st not in ("true", "checked"):
            toggle.first.click(); page.wait_for_timeout(600)
        log(f"  email-consent -> {toggle.first.get_attribute('aria-checked')}")

    page.fill("#soundcheckEmail", a.email)
    page.locator("button:has-text('Get started')").first.click()
    log("  submitted email gate")
    page.wait_for_timeout(5000)
    dump(page, "check-your-email")


def fill_details(page, a):
    log("details step")
    page.fill("#soundcheckConfirmFirstName", a.first_name)
    page.fill("#soundcheckConfirmLastName", a.last_name)
    page.fill("#soundcheckConfirmPhoneNumber", a.phone)
    fill_location(page, a.location, a.location_pick)
    set_dob(page, a.dob_day, a.dob_month, a.dob_year)
    dump(page, "details-filled")


def phase_polls(page, a):
    log("polls step (cities + travel packages)")
    for i, city in enumerate(a.cities):
        pick_dropdown(page, "Select show", re.escape(city), f"preference {i + 1}")
    pick_dropdown(page, "Select an option", r"^No$", "travel packages")
    dump(page, "polls-filled")
    click_continue(page)

    log("album step")
    dump(page, "album-step")
    pick_dropdown(page, "Select an option", a.album, "album answer")
    dump(page, "album-answered")
    click_continue(page)


def phase_terms(page):
    log("terms step — this is the irreversible submit")
    dump(page, "terms-step")
    scroll = page.locator("button:has-text('Scroll to bottom')")
    if scroll.count():
        scroll.first.click(); page.wait_for_timeout(2000)
        log("  scrolled to bottom")
    submit = page.locator("button:has-text('Submit')")
    if submit.count() == 0:
        raise RuntimeError("no Submit button on the terms step")
    submit.first.click()
    log("  clicked Submit")
    page.wait_for_timeout(12000)
    dump(page, "confirmation")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all",
                    choices=["email", "details", "polls", "all"])
    ap.add_argument("--cred", default="/tmp/oasis_cred.txt")
    ap.add_argument("--profile", default="/tmp/oasis_run")
    ap.add_argument("--proxy", dest="proxy", action="store_true", default=True)
    ap.add_argument("--no-proxy", dest="proxy", action="store_false")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--proxy-url", default=None,
                    help="explicit upstream proxy, e.g. http://user:pass@host:port")
    ap.add_argument("--fp-os", default=None, choices=["windows", "macos"],
                    help="force the spoofed desktop OS (default: random)")
    ap.add_argument("--fp-seed", type=int, default=None,
                    help="reproduce one specific fingerprint")
    ap.add_argument("--fp-json", default=None,
                    help="write the fingerprint actually used to this path")
    ap.add_argument("--submit", action="store_true",
                    help="actually click Submit on the terms step")
    ap.add_argument("--reuse-link", action="store_true",
                    help="reuse the saved verification link instead of a new email")
    ap.add_argument("--first-name", default="Martha")
    ap.add_argument("--last-name", default="Chapman")
    ap.add_argument("--phone", default="6606453436")
    ap.add_argument("--location", default="New York, NY")
    ap.add_argument("--location-pick", default="New York, New York, United States")
    ap.add_argument("--dob-day", type=int, default=16)
    ap.add_argument("--dob-month", default="March")
    ap.add_argument("--dob-year", type=int, default=2000)
    ap.add_argument("--cities", default="Boston,Las Vegas,Knebworth")
    ap.add_argument("--album", default="Morning Glory")
    a = ap.parse_args()
    a.cities = [c.strip() for c in a.cities.split(",") if c.strip()]

    cred = parse_cred(open(a.cred).read())
    a.email = cred["email"]
    log(f"account={a.email} submit={a.submit} headless={a.headless} "
        f"proxy={a.proxy_url or ('relay' if a.proxy else 'off')}")

    with sync_playwright() as p:
        ctx, page, fp = launch(p, a)

        if a.phase in ("email", "all") and not a.reuse_link:
            started = calendar.timegm(time.gmtime())
            phase_email(page, a)
            log("polling mailbox for the verification link ...")
            url = find_verify_link(cred, since=started)
            if not url:
                log("!! no verification link arrived"); ctx.close(); return
            save_state({"verify_url": url})
            log(f"  link acquired ({len(url)} chars)")
        else:
            url = load_state().get("verify_url")
            if not url:
                log("!! no saved verification link"); ctx.close(); return

        log("opening verification link")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("#soundcheckConfirmFirstName", timeout=60000)
        page.wait_for_timeout(3500)

        fill_details(page, a)
        click_continue(page)

        if a.phase == "details":
            dump(page, "polls-step"); ctx.close(); return

        phase_polls(page, a)

        if a.phase == "polls" or not a.submit:
            log("dry run: stopping before the terms Submit")
            dump(page, "terms-step")
            page.wait_for_timeout(3000)
            ctx.close(); return

        phase_terms(page)
        log("done — browser left open 20s for inspection")
        page.wait_for_timeout(20000)
        ctx.close()


if __name__ == "__main__":
    main()
