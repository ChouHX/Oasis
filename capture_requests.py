#!/usr/bin/env python3
"""Capture the two write requests of the Oasis Live '27 registration flow.

  A) email gate  ->  POST that triggers the verification mail   (sent for real,
     it is idempotent and only mails a link)
  B) final Submit on the terms step  ->  captured but ABORTED via route
     interception, so no duplicate registration is ever created.

Everything is dumped to /tmp/capture_out.json for offline analysis.
"""
import json, re, sys, time, types, calendar
from playwright.sync_api import sync_playwright
sys.path.insert(0, ".")
import register as R

OUT = "/tmp/capture_out.json"
ARMED = {"on": False}
hits = []          # aborted submit requests
api_log = []       # every api.openstage.live exchange


def note(request, response=None):
    try:
        post = request.post_data
    except Exception:
        post = None
    rec = {"method": request.method, "url": request.url,
           "req_headers": dict(request.headers), "post_data": post,
           "resource_type": request.resource_type}
    if response is not None:
        try:
            rec["status"] = response.status
            rec["resp_headers"] = dict(response.headers)
            if "json" in (response.headers.get("content-type") or ""):
                rec["resp_body"] = response.text()[:4000]
        except Exception:
            pass
    api_log.append(rec)


def main():
    a = types.SimpleNamespace(
        first_name="Martha", last_name="Chapman", phone="6606453436",
        location="New York, NY",
        location_pick="New York, New York, United States",
        dob_day=16, dob_month="March", dob_year=2000)
    cred = R.parse_cred(open("/tmp/oasis_cred.txt").read())
    a.email = cred["email"]

    with sync_playwright() as p:
        ctx = R.launch(p, "/tmp/oasis_capture", False, False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.on("response", lambda r: note(r.request, r)
                if "api.openstage.live" in r.url else None)
        page.on("request", lambda q: note(q)
                if "api.openstage.live" in q.url else None)

        def intercept(route):
            req = route.request
            if ARMED["on"]:
                try:
                    post = req.post_data
                except Exception:
                    post = None
                hits.append({"method": req.method, "url": req.url,
                             "req_headers": dict(req.headers), "post_data": post,
                             "aborted": True})
                print(f"[intercept] ABORTED {req.method} {req.url}", flush=True)
                return route.abort("aborted")
            return route.continue_()

        # ---- A) email gate ------------------------------------------------ #
        page.goto(R.SITE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("#soundcheckEmail", timeout=60000)
        t = page.locator("button[aria-label*='receive updates from Oasis']")
        if t.count():
            t.first.click(); page.wait_for_timeout(500)
        page.fill("#soundcheckEmail", a.email)
        started = calendar.timegm(time.gmtime())
        page.locator("button:has-text('Get started')").first.click()
        page.wait_for_timeout(6000)
        print("[A] email gate submitted", flush=True)

        # ---- verification link ------------------------------------------- #
        url = R.find_verify_link(cred, since=started)
        if not url:
            print("!! no verification link"); ctx.close(); return
        print("[link] acquired", flush=True)

        # ---- details ------------------------------------------------------ #
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("#soundcheckConfirmFirstName", timeout=60000)
        page.wait_for_timeout(3000)
        R.fill_details(page, a)
        page.locator("button:has-text('Continue')").first.click()
        page.wait_for_timeout(8000)

        # ---- polls + album ------------------------------------------------ #
        for city in ("Boston", "Las Vegas", "Knebworth"):
            R.pick_dropdown(page, "Select show", re.escape(city), f"city {city}")
        R.pick_dropdown(page, "Select an option", r"^No$", "travel")
        page.locator("button:has-text('Continue')").first.click()
        page.wait_for_timeout(8000)
        R.pick_dropdown(page, "Select an option", "Morning Glory", "album")
        page.locator("button:has-text('Continue')").first.click()
        page.wait_for_timeout(9000)
        print("[B] reached terms step — arming interceptor", flush=True)

        # ---- B) final Submit: arm interception, then abort ---------------- #
        page.route("**/*", intercept)
        ARMED["on"] = True
        sc = page.locator("button:has-text('Scroll to bottom')")
        if sc.count():
            sc.first.click(); page.wait_for_timeout(1500)
        page.locator("button:has-text('Submit')").first.click()
        page.wait_for_timeout(6000)
        print("[B] submit request captured (aborted, nothing was sent)", flush=True)

        with open(OUT, "w") as f:
            json.dump({"submit_aborted": hits, "api_exchanges": api_log},
                      f, indent=1, ensure_ascii=False)
        print(f"saved -> {OUT}  ({len(api_log)} api exchanges, "
              f"{len(hits)} aborted submit)", flush=True)
        ctx.close()


main()
