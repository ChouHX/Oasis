#!/usr/bin/env python3
"""Can the whole registration run on curl_cffi alone, with no browser at all?

The one documented blocker is the reCAPTCHA Enterprise token that
/fan2/verify/confirm is supposed to require. This script drives both API calls
end to end against a fresh mailbox and lets the captcha value be chosen, so the
server's real validation behaviour gets measured instead of assumed.

  python3 curl_cffi_test.py --cred <mailbox> --captcha ""      # empty
  python3 curl_cffi_test.py --cred <mailbox> --captcha omit    # field absent
"""
import argparse
import json
import re
import sys
import time

from curl_cffi import requests as creq

HERE = "/home/beacon/dev/ai/tickets/oasis"
sys.path.insert(0, HERE)
import identity  # noqa: E402
from graph_mail import parse_cred, refresh, graph_get  # noqa: E402

PROXY = "http://127.0.0.1:8899"
API = "https://api.openstage.live"
ARTIST = "28400196-01ba-4920-810b-9592f9f1045d"
PAGE = "b4356f65-bdc5-4dd6-84a8-36369f15b9a8"
RETURN_URL = "https://oasis.hq.fan/registration"

EVENTS_POLL = "e0f2f14d-daed-4afb-978b-dc38f2164e01"
PREF_POLL_2 = "f3f3fdeb-20d7-4bd1-b84d-c7545886a904"
PREF_POLL_3 = "03890a33-7440-470e-b5a4-d12133d01a99"
TRAVEL_POLL = "d9ec070f-2bb0-4e83-a64d-0b2451ac2252"
TRAVEL_NO = "62777823-211b-4436-803e-2ba57f735fa5"
ALBUM_POLL = "47ea91f1-5f0f-4556-8d32-db7b22f43ade"
ALBUM_1995 = "c107232e-b35c-4f40-bf45-ba74018a43fe"

# venue -> (own pollAnswerId, [preferencePollAnswers for 2nd, 3rd slot])
SHOWS = {
    "knebworth": ("e9f6e585-adaf-44e3-9233-ed5eeb67db3c",
                  ["c4c3fcdd-9d22-4257-a7d3-acdb8e5e1a43",
                   "7ec1fb94-2219-4ed5-9b6a-dd2784ae4570"]),
    "slane": ("a7425d69-872e-4451-9d45-5b307f1220e6",
              ["8049cf4c-5f07-446e-8ff1-d7282a5ad7fa",
               "6d88dea0-2b06-4ff9-ba46-53f00f63bc8d"]),
    "glasgow": ("5202df3d-3bd5-44e2-a9da-66c254beab68",
                ["83923b31-1da3-4df0-a6c5-d787a09a820c",
                 "734c06e9-4659-488b-9375-081566ca531e"]),
}
# preference order requested: Knebworth first, Slane second, Glasgow third
PREFERENCE_ORDER = ["knebworth", "slane", "glasgow"]

TOKEN_RE = re.compile(r"https://oasis\.hq\.fan/registration\?token=[A-Za-z0-9._\-]+")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def with_retry(fn, label="", attempts=5, base_delay=1.5):
    """The residential upstream drops tunnels under load; retry the call."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            log(f"  {label} attempt {i + 1}/{attempts} failed: "
                f"{type(e).__name__} {str(e)[:90]}")
            time.sleep(base_delay * (i + 1))
    raise last


def session():
    return creq.Session(impersonate="chrome",
                        proxies={"http": PROXY, "https": PROXY})


def api_headers():
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US",
        "content-type": "application/json",
        "origin": "https://oasis.hq.fan",
        "referer": "https://oasis.hq.fan/",
    }


def request_verification(s, email):
    body = {
        "returnUrl": RETURN_URL,
        "artistId": ARTIST,
        "pageId": PAGE,
        "locale": "en",
        "type": "email",
        "email": email,
        "data": {
            "tags": [],
            "pollAnswerIds": [],
            "consentEmail": True,
            "acquisition": {
                "channelName": "Register for Oasis Live '27",
                "channelType": "Registration Page",
                "referrer": None,
                "pageId": PAGE,
            },
        },
    }
    r = with_retry(lambda: s.post(f"{API}/fan2/verify/verify", json=body,
                                  headers=api_headers(), timeout=60),
                   label="verify/verify")
    log(f"verify/verify -> {r.status_code} {r.text[:200]}")
    return r


def wait_for_link(cred, since, timeout=600, interval=8):
    at = with_retry(lambda: refresh(cred)["access_token"], label="graph token")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = graph_get(
                "/me/messages?$top=15&$select=subject,receivedDateTime,body", at)
        except Exception as e:
            log(f"  graph error {type(e).__name__}: {str(e)[:90]}")
            time.sleep(interval)
            continue
        for m in data.get("value", []):
            body = (m.get("body") or {}).get("content", "") or ""
            flat = re.sub(r"=\r?\n", "", body).replace("&amp;", "&")
            hit = TOKEN_RE.search(flat)
            if hit:
                log(f"  mail: {m.get('subject')} @ {m.get('receivedDateTime')}")
                return hit.group(0)
        time.sleep(interval)
    return None


def check_verification(s, token):
    """The mail link's own step: marks the token emailValid and re-issues it.

    confirm rejects the raw mail token with "email not validated", so this call
    has to happen first - it is what the SPA fires when the link is opened.
    """
    hdr = {k: v for k, v in api_headers().items() if k != "content-type"}
    r = with_retry(lambda: s.get(
        f"{API}/fan2/verify/check-verification",
        params={"token": token, "artistId": ARTIST, "pageId": PAGE},
        headers=hdr, timeout=60), label="check-verification")
    log(f"check-verification -> {r.status_code}")
    try:
        j = r.json()
    except Exception:
        log(f"  (non-json body: {r.text[:200]})")
        return None
    claims = j.get("claims") or {}
    log(f"  claims: emailValid={claims.get('emailValid')} "
        f"closed={claims.get('closed')} session={claims.get('sessionId')}")
    return j.get("token")


def build_confirm(ident, token, url, captcha):
    kneb, slane, glasgow = (SHOWS[k] for k in PREFERENCE_ORDER)
    body = {
        "location": ident["location"],
        "consentEmail": True,
        "countryCallingCode": "1",
        "nationalPhoneNumber": ident["phone"],
        "firstName": ident["first_name"],
        "lastName": ident["last_name"],
        "dateOfBirth": ident["date_of_birth"],
        "journeyPollAnswers": [
            {"pollId": PREF_POLL_2, "pollAnswerIds": [slane[1][0]]},
            {"pollId": PREF_POLL_3, "pollAnswerIds": [glasgow[1][1]]},
            {"pollId": TRAVEL_POLL, "pollAnswerIds": [TRAVEL_NO]},
            {"pollId": ALBUM_POLL, "pollAnswerIds": [ALBUM_1995]},
        ],
        "pollAnswerIds": [kneb[0]],
        "artistId": ARTIST,
        "ip": None,
        "locale": "en-US",
        "pageId": PAGE,
        "pollId": EVENTS_POLL,
        "tags": ["welcome", "signup-live-27-registration"],
        "token": token,
        "url": url,
    }
    if captcha != "omit":
        body["captcha"] = captcha
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True)
    ap.add_argument("--captcha", default="",
                    help='captcha value, or "omit" to drop the field entirely')
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reuse-token", default=None,
                    help="skip the mail round-trip and reuse a token/url")
    ap.add_argument("--repeat", type=int, default=1,
                    help="post confirm N times with the same token")
    a = ap.parse_args()

    cred = parse_cred(open(a.cred).read())
    ident = identity.random_identity(
        None if a.seed is None else __import__("random").Random(a.seed))
    log(f"mailbox={cred['email']}")
    log(f"identity={ident['first_name']} {ident['last_name']} "
        f"{ident['phone']} {ident['date_of_birth']} {ident['location']['city']}")
    log(f"captcha={a.captcha!r}")

    s = session()
    if a.reuse_token:
        token = a.reuse_token
        url = f"{RETURN_URL}?token={token}"
    else:
        started = time.time()
        request_verification(s, cred["email"])
        url = wait_for_link(cred, started)
        if not url:
            log("!! no verification mail arrived")
            return
        token = url.split("token=", 1)[1]
    log(f"token acquired ({len(token)} chars)")

    fresh = check_verification(s, token)
    if fresh:
        token = fresh
        url = f"{RETURN_URL}?token={token}"
        log(f"token refreshed ({len(token)} chars)")

    body = build_confirm(ident, token, url, a.captcha)
    for i in range(a.repeat):
        log(f"posting confirm ({i + 1}/{a.repeat}) ...")
        r = with_retry(lambda: s.post(f"{API}/fan2/verify/confirm", json=body,
                                      headers=api_headers(), timeout=90),
                       label="confirm")
        log(f"confirm -> {r.status_code}")
        print(f"--- response {i + 1} ---")
        print(r.text[:800])
        if i + 1 < a.repeat:
            time.sleep(3)


if __name__ == "__main__":
    main()
