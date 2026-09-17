#!/usr/bin/env python3
"""The registration itself, over plain HTTP.

Verified end to end: the whole flow runs on curl_cffi with no browser at all.
Two findings drove this design:

  * /fan2/verify/confirm does NOT enforce the reCAPTCHA Enterprise token. An
    empty captcha string is accepted, so the browser and its reCAPTCHA round
    trip are unnecessary.
  * confirm rejects the raw mail token with "email not validated". The SPA's
    own step, GET /fan2/verify/check-verification?token&artistId&pageId, must
    run first: it returns a re-issued token carrying emailValid=true, and that
    token is what confirm accepts.

Three calls, one mailbox, a few seconds:

    POST /fan2/verify/verify                       -> mails the link
    GET  /fan2/verify/check-verification           -> re-issues a valid token
    POST /fan2/verify/confirm                      -> {"status":"OK"}
"""
import random
import time

from curl_cffi import requests as creq

API = "https://api.openstage.live"
# Funnel telemetry lives on a different host from the API. The SPA fires it as
# the visitor moves through the form.
TELEMETRY_URL = "https://queue.openstage.live/fan1/telemetry"
RETURN_URL = "https://oasis.hq.fan/registration"

# Where the page's identity comes from. The SPA resolves it the same way on
# load: artist.json carries the artist id, and /fan2/page/{artistId}/registration
# carries the page id and title.
ARTIST_JSON_URL = ("https://openstage-pages.s3.eu-west-2.amazonaws.com"
                   "/oasis/artist.json")
PAGE_CONFIG_PATH = "/fan2/page/{artist}/registration"

# Values captured 2026-09-16. They are fallbacks only - resolve_page() replaces
# them from the site, because a new registration round changes both ids and a
# pinned copy would then fail every request with no obvious symptom.
ARTIST_ID = "28400196-01ba-4920-810b-9592f9f1045d"
PAGE_ID = "b4356f65-bdc5-4dd6-84a8-36369f15b9a8"
CHANNEL_NAME = "Register for Oasis Live '27"
CHANNEL_TYPE = "Registration Page"

# reCAPTCHA Enterprise, invisible, evaluated by the page on submit.
# The site's reCAPTCHA parameters, kept as reference: they are what a captcha
# service (or any manual debugging) needs, and they identify which reCAPTCHA
# this endpoint uses. The browser flow does not read them - the SPA fetches its
# own configuration and mints the token itself.
SITEKEY = "6LcMjSsqAAAAANqPn-O5M5wUDtJm-Zjx2d3NtWTp"
CAPTCHA_ACTION = "fan_verification"

EVENTS_POLL = "e0f2f14d-daed-4afb-978b-dc38f2164e01"

# The rest of the form's poll ids, needed only by the plain-HTTP flow: with no
# page to build the confirm body, this flow has to assemble the journey itself.
# The browser flow never reads these - its SPA fills its own.
PREF_POLL_2 = "f3f3fdeb-20d7-4bd1-b84d-c7545886a904"
PREF_POLL_3 = "03890a33-7440-470e-b5a4-d12133d01a99"
TRAVEL_POLL = "d9ec070f-2bb0-4e83-a64d-0b2451ac2252"
TRAVEL_NO = "62777823-211b-4436-803e-2ba57f735fa5"
ALBUM_POLL = "47ea91f1-5f0f-4556-8d32-db7b22f43ade"
ALBUM_1995 = "c107232e-b35c-4f40-bf45-ba74018a43fe"

# Cloudflare's trace endpoint. The SPA reads `ip=` from it before submitting
# and puts the value in the confirm body.
CF_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# venue -> (own pollAnswerId, [answer for the 2nd slot, answer for the 3rd slot])
# All eleven cities from the registration form, in the order the page lists them.
SHOWS = {
    "glasgow": ("5202df3d-3bd5-44e2-a9da-66c254beab68",
                ["83923b31-1da3-4df0-a6c5-d787a09a820c",
                 "734c06e9-4659-488b-9375-081566ca531e"]),
    "manchester": ("ecfb4675-0a0b-4733-b3c3-83fc13e0d058",
                   ["30c3fc48-4797-49dd-8eeb-61f9bfd333c3",
                    "7dc7f340-f7fa-447d-8707-c02b26198613"]),
    "munich": ("3c323b27-d021-457b-b6e7-1aee96d217f5",
               ["3633ae5b-3186-46cb-9d47-ce7e05fff83b",
                "1b72369c-d0f6-4d6a-9ef0-d2428897b3dc"]),
    "barcelona": ("78749329-a3a0-41b3-98db-fa92a280e84c",
                  ["9152834d-bc18-47d9-aa70-3cd1aea8f5d8",
                   "e337a95d-5613-4144-ad6e-0863f801ebe2"]),
    "amsterdam": ("31b298b8-13be-4b9d-b125-88bc4351a28d",
                  ["73d57077-1e37-4f8a-a479-ff20b935779f",
                   "d0471262-6c13-483f-99aa-b2c368b20a61"]),
    "paris": ("c448ca76-bd1d-459a-8452-f4e02c56385a",
              ["e0220078-8499-47cf-b363-d2dfa1728767",
               "cb9c2872-9174-4c61-9d62-2f385a613317"]),
    "rome": ("0d901162-1406-44c7-af3f-872d3a247ab0",
             ["3eb10d46-b7fc-49af-a8f2-950d2eeacd88",
              "e0d0b576-5e3e-4a3e-ae50-fbbdebc4b173"]),
    "boston": ("82e76cdd-7b13-4ecb-9df3-fd4fcb247475",
               ["5313e3d3-8b03-4190-a7f2-9d36b4b20882",
                "5ace1a43-dbeb-464e-a382-dccfce121bd3"]),
    "lasvegas": ("9b46b5ec-72c1-4ac7-883b-10f64ad2b4cd",
                 ["4570d616-9c56-4cf0-957b-e520194d8ea6",
                  "39082884-963e-4dd1-8f42-39f1c5f0716f"]),
    "slane": ("a7425d69-872e-4451-9d45-5b307f1220e6",
              ["8049cf4c-5f07-446e-8ff1-d7282a5ad7fa",
               "6d88dea0-2b06-4ff9-ba46-53f00f63bc8d"]),
    "knebworth": ("e9f6e585-adaf-44e3-9233-ed5eeb67db3c",
                  ["c4c3fcdd-9d22-4257-a7d3-acdb8e5e1a43",
                   "7ec1fb94-2219-4ed5-9b6a-dd2784ae4570"]),
}

# display order = the order the registration page lists them in
SHOW_ORDER = ["glasgow", "manchester", "munich", "barcelona", "amsterdam",
              "paris", "rome", "boston", "lasvegas", "slane", "knebworth"]

SHOW_META = {
    "glasgow": ("2027-05", "Glasgow", "UK"),
    "manchester": ("2027-06", "Manchester", "UK"),
    "munich": ("2027-07", "Munich", "DE"),
    "barcelona": ("2027-07", "Barcelona", "ES"),
    "amsterdam": ("2027-07", "Amsterdam", "NL"),
    "paris": ("2027-07", "Paris", "FR"),
    "rome": ("2027-07", "Rome", "IT"),
    "boston": ("2027-08", "Boston", "US"),
    "lasvegas": ("2027-08", "Las Vegas", "US"),
    "slane": ("2027-09", "Slane", "IE"),
    "knebworth": ("2027-09", "Knebworth", "UK"),
}

SHOW_LABEL = {k: f"{d} | {v} | {c}" for k, (d, v, c) in SHOW_META.items()}

# Preference ranking requested: Knebworth first, Slane second, Glasgow third.
DEFAULT_ORDER = ["knebworth", "slane", "glasgow"]

# Desktop TLS/HTTP fingerprints curl_cffi can wear. One is drawn per run so the
# transport profile differs between registrations, matching the random-identity
# requirement. Deliberately no Linux/Android/iOS entries.
IMPERSONATE_POOL = [
    "chrome131", "chrome136", "chrome142", "chrome145", "chrome146", "chrome150",
    "edge101", "firefox135", "firefox144", "safari180", "safari184", "safari260",
]


class RegistrationError(Exception):
    """The site refused this registration. Retrying the same account is pointless."""


class RegistrationClosed(RegistrationError):
    """The site has already closed this registration session.

    Read off the token: check-verification answers with claims carrying `closed`.
    Measured across six addresses - the four that kept failing all came back
    `closed: true`, while the one that registered and the one that was merely
    unfinished both came back `closed: false`.

    A closed session still renders the page, but without a usable form, so
    filling it in and pressing Continue neither advances nor complains. That is
    exactly the "details step did not advance; page says nothing" in the log,
    and none of it is retryable: the address has been dealt with (by us or by
    hand) or the site retired the session. Retrying only pushes the account
    round the queue for ever.
    """


class TransientError(Exception):
    """A failure that says nothing about the account.

    Dead proxy, dropped tunnel, relay out of upstreams - the account is
    untouched and belongs back in the queue rather than in the failed pile.
    Defined here, not in browser_registrar: both flows raise it and only one of
    them involves a browser, and the engine has to catch one type either way.
    """


def _headers(extra=None):
    h = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US",
        "origin": "https://oasis.hq.fan",
        "referer": "https://oasis.hq.fan/",
    }
    if extra:
        h.update(extra)
    return h


def _is_transient(e):
    """Only network-shaped failures are worth retrying.

    Retrying a programming error (AttributeError, KeyError, ...) just burns the
    backoff budget and hides the real bug behind a generic "failed after N
    attempts" message.
    """
    if isinstance(e, (OSError, TimeoutError, ConnectionError)):
        return True
    module = (type(e).__module__ or "").split(".")[0]
    return module in ("curl_cffi", "ssl", "urllib", "http", "socket")


def _retry(fn, label, log, attempts=5, base=1.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if not _is_transient(e):
                raise
            log(f"    {label} retry {i + 1}/{attempts}: {type(e).__name__} "
                f"{str(e)[:80]}")
            time.sleep(base * (i + 1))
    raise RegistrationError(f"{label} failed: {last}")


def make_session(proxy_url=None, timeout=60, impersonate=None):
    """One session = one transport fingerprint. Randomised unless pinned."""
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    target = impersonate or random.choice(IMPERSONATE_POOL)
    s = creq.Session(impersonate=target, proxies=proxies, timeout=timeout)
    s._fp_label = target          # surfaced in the log
    return s


def resolve_page(session=None, log=print):
    """Fetch artistId / pageId / channel name from the site, once per run.

    Returns True when the live values were picked up, False when the fallbacks
    are still in place. Never raises: a lookup failure must not stop a run that
    would otherwise work with the values captured earlier.
    """
    global ARTIST_ID, PAGE_ID, CHANNEL_NAME
    own = session is None
    if own:
        session = creq.Session(impersonate="chrome", timeout=30)
    try:
        # Retry once: the session picks a random impersonation and the relay
        # re-dials the upstream per request, so an occasional TLS handshake
        # failure is expected rather than a sign of anything wrong.
        r = _retry(lambda: session.get(ARTIST_JSON_URL, headers=_headers(),
                                       timeout=30), "artist.json", log)
        artist = (r.json() or {}).get("artist_id")
        if not artist:
            log(f"    page resolve: artist.json 没有 artist_id")
            return False

        r = _retry(lambda: session.get(
            f"{API}{PAGE_CONFIG_PATH.format(artist=artist)}",
            headers=_headers(), timeout=30), "page config", log)
        cfg = r.json() or {}
        page = cfg.get("id")
        title = ((cfg.get("metaData") or {}).get("content") or {}) \
            .get("page", {}).get("title")
        if not page:
            log("    page resolve: 页面配置里没有 id")
            return False

        changed = (artist != ARTIST_ID) or (page != PAGE_ID)
        ARTIST_ID = artist
        PAGE_ID = page
        if title:
            CHANNEL_NAME = title
        log(f"    page resolve: artist={artist[:8]} page={page[:8]} "
            f"channel={CHANNEL_NAME!r}"
            + ("  （与内置值不同，已更新）" if changed else ""))
        return True
    except Exception as e:
        log(f"    page resolve 失败（{type(e).__name__}: {str(e)[:90]}），"
            f"沿用内置值")
        return False
    finally:
        if own:
            try:
                session.close()
            except Exception:
                pass


def build_verify(email):
    """Body for POST /fan2/verify/verify (triggers the verification mail).

    Field for field what the SPA sends, checked against a fresh capture from a
    real browser: six top-level fields plus `data`, with `data` carrying tags,
    pollAnswerIds, consentEmail and acquisition. There is no captcha field on
    this endpoint.

    `artistId` and `pageId` are page-level constants rather than anything the
    request negotiates - they identify the Oasis Live '27 registration page and
    are the same for every account, which is why they are pinned here.
    """
    return {
        "returnUrl": RETURN_URL,
        "artistId": ARTIST_ID,
        "pageId": PAGE_ID,
        "locale": "en",
        "type": "email",
        "email": email,
        "data": {
            "tags": [],
            "pollAnswerIds": [],
            # Present in the real request. Verification still worked without it,
            # but there is no reason to look different from the SPA.
            "consentEmail": True,
            "acquisition": {
                "channelName": CHANNEL_NAME,
                "channelType": CHANNEL_TYPE,
                "referrer": None,
                "pageId": PAGE_ID,
            },
        },
    }


def report_telemetry(session, metric, log=print):
    """One funnel event, exactly as the page sends it.

    Captured from a live browser: `hit` and `uniquehit` go out when a page
    loads, `email-entered-hit` immediately after the verify request. They are
    the site's own telemetry, and a registration that never reports itself is a
    difference worth not having - it costs one small POST.

    Never raises. Telemetry failing must not cost an account.
    """
    body = {
        "metric": metric,
        "resource": RETURN_URL,
        "origin": RETURN_URL,
        "pageId": PAGE_ID,
        "artistId": ARTIST_ID,
    }
    try:
        r = session.post(TELEMETRY_URL, json=body,
                         headers=_headers(), timeout=20)
        log(f"    telemetry {metric} -> {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        log(f"    telemetry {metric} 失败（{type(e).__name__}），忽略")
        return False


def request_verification(session, email, log=print):
    # The order mirrors the real page: a page load reports itself, then the
    # submit does, then the verify request sits between the two.
    report_telemetry(session, "hit", log)
    report_telemetry(session, "uniquehit", log)

    body = build_verify(email)
    r = _retry(lambda: session.post(f"{API}/fan2/verify/verify", json=body,
                                    headers=_headers(
                                        {"content-type": "application/json"})),
               "verify/verify", log)
    if r.status_code != 200:
        raise RegistrationError(f"verify/verify HTTP {r.status_code}: {r.text[:200]}")
    log(f"    verify/verify -> {r.text[:80]}")
    report_telemetry(session, "email-entered-hit", log)
    return r








def check_verification(session, token, log=print):
    """The mail link's own step: re-issues the token with emailValid=true.

    confirm refuses the raw mail token with "email not validated", so this has
    to run first - it is what the SPA fires when the link is opened. Returns
    (token, claims).
    """
    r = _retry(lambda: session.get(
        f"{API}/fan2/verify/check-verification",
        params={"token": token, "artistId": ARTIST_ID, "pageId": PAGE_ID},
        headers=_headers()), "check-verification", log)
    if r.status_code != 200:
        raise RegistrationError(
            f"check-verification HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    claims = data.get("claims") or {}
    if not claims.get("emailValid"):
        raise RegistrationError(f"emailValid false: {claims}")
    if claims.get("closed"):
        raise RegistrationClosed(
            f"站点已关闭该会话（closed=true, "
            f"session={claims.get('sessionId')}）：该地址已处理过，"
            f"或站点因重复请求作废了它")
    return data.get("token") or token, claims


def client_ip(session):
    """Public IP as Cloudflare sees it, or None.

    The SPA reads this exact endpoint before submitting and puts the value in
    the confirm body's `ip` field. `null` was measured to work, but there is no
    reason to look different from the page.
    """
    try:
        txt = session.get(CF_TRACE_URL, timeout=25).text
    except Exception:
        return None
    for line in txt.splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    return None


def build_confirm(ident, token, url, order=None, captcha="", ip=None):
    """The /fan2/verify/confirm body for the plain-HTTP flow.

    Built here because no page exists to build it. The one deliberate
    difference from the SPA's own body is the captcha field, and confirm()
    below records what that difference is worth.
    """
    order = order or DEFAULT_ORDER
    primary, *_rest = order
    # journeyPollAnswers carries the site's own journey steps and nothing else.
    # Today that is two: the travel question and the album question, both read
    # straight out of the page config (journey.steps / .poll.id).
    #
    # The preference polls are NOT journey steps. The site declares them in
    # form.events.preferencePollIds and they exist to rank the three event picks
    # - sending them inside the journey posts two polls the site never asked
    # about, which is what this flow did until the config was read properly.
    journey = [
        {"pollId": TRAVEL_POLL, "pollAnswerIds": [TRAVEL_NO]},
        {"pollId": ALBUM_POLL, "pollAnswerIds": [ALBUM_1995]},
    ]

    # The site sends dateOfBirth as a full ISO instant rather than a bare date.
    dob = ident["date_of_birth"]
    if len(dob) == 10:
        dob = f"{dob}T00:00:00.000Z"

    return {
        "location": ident["location"],
        "captcha": captcha,
        "consentEmail": True,
        # The SPA posts this alongside consentEmail; omit it and the request
        # differs from every real submission.
        "consentMessaging": True,
        "countryCallingCode": ident.get("country_calling_code", "1"),
        "nationalPhoneNumber": ident["phone"],
        "firstName": ident["first_name"],
        "lastName": ident["last_name"],
        "dateOfBirth": dob,
        "journeyPollAnswers": journey,
        "pollAnswerIds": [SHOWS[primary][0]],
        "artistId": ARTIST_ID,
        "ip": ip,
        "locale": "en-US",
        "pageId": PAGE_ID,
        "pollId": EVENTS_POLL,
        "tags": ["welcome", "signup-live-27-registration"],
        "token": token,
        "url": url,
    }


def confirm(session, body, log=print):
    """POST /fan2/verify/confirm. Returns (json, raw text).

    Measured 2026-09-16 on this endpoint - same proxy, same mailbox, same
    minute, only the captcha differing:

        captcha empty         -> registration completed, success mail arrived
        captcha (2382 chars)  -> answers {"status":"OK"}, no success mail, ever

    A reCAPTCHA Enterprise token is bound to the client that minted it, so one
    minted in Chromium and replayed from curl scores as invalid. Sending the
    empty string is the point of this flow, not a shortcut around it.
    """
    r = _retry(lambda: session.post(
        f"{API}/fan2/verify/confirm", json=body,
        headers=_headers({"content-type": "application/json"})),
        "confirm", log)
    text = r.text.strip()
    if r.status_code != 200:
        raise RegistrationError(f"confirm HTTP {r.status_code}: {text[:200]}")
    try:
        data = r.json()
    except Exception:
        data = {"raw": text}
    if data.get("error"):
        raise RegistrationError(f"confirm rejected: {data['error']}")
    return data, text


def register_over_http(session, mailbox, ident, order=None, *,
                       link_timeout=300, success_timeout=180,
                       mail_since=None, captcha="", log=print):
    """A whole registration over curl, with no browser anywhere in it.

    The calls the SPA makes, issued directly: verify, then the mail link's
    check-verification, then confirm. Nothing renders, so nothing can be read
    back from a page - which makes the success mail the only evidence that a
    registration completed, and that is what this waits for. Returns the same
    shape browser_registrar.register() does, so the engine can treat both alike.
    """
    order = order or DEFAULT_ORDER
    t0 = time.time()

    if mail_since is None:
        request_verification(session, mailbox.email, log)
        mail_since = time.time()

    url, received = mailbox.find_verification_link(
        timeout=link_timeout, not_before=mail_since, log=log)
    if not url:
        raise RegistrationError("verification mail never arrived")
    log(f"  [{mailbox.email}] mail {received}")

    token, claims = check_verification(session, url.split("token=", 1)[1], log)
    url = f"{RETURN_URL}?token={token}"
    log(f"  [{mailbox.email}] emailValid=true session={claims.get('sessionId')}")

    body = build_confirm(ident, token, url, order, captcha=captcha,
                         ip=client_ip(session))
    _data, text = confirm(session, body, log)
    log(f"  [{mailbox.email}] confirm -> {text[:60]}")

    found, when = mailbox.find_success(success_timeout, not_before=t0, log=log)
    if not found:
        raise RegistrationError(
            "confirm answered OK but the success mail never arrived within "
            f"{success_timeout}s")

    return {
        "email": mailbox.email,
        "session_id": claims.get("sessionId"),
        "token": token,
        "poll_answer_ids": body["pollAnswerIds"],
        "journey": body["journeyPollAnswers"],
        "response": text,
        "elapsed": round(time.time() - t0, 2),
        "refresh_token": mailbox.refresh_token,
        "captcha_len": len(captcha),
        "mode": "http",
        "evidence": "mail",
    }


def probe_proxy(proxy_url, timeout=25):
    """Health check used by the proxy screen."""
    s = creq.Session(impersonate="chrome",
                     proxies={"http": proxy_url, "https": proxy_url},
                     timeout=timeout)
    t0 = time.time()
    try:
        r = s.get("https://api.ipify.org?format=json")
        return True, r.json().get("ip", "?"), round(time.time() - t0, 2)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}", round(time.time() - t0, 2)


# ip-api.com free tier: HTTP only, ~45 requests/minute. Results are cached per
# proxy by the caller, so one lookup per distinct exit is all we ever spend.
GEO_URL = "http://ip-api.com/json/?fields=status,countryCode,country,city,query"






def exit_geo(proxy_url, timeout=20):
    """Country of the proxy's exit IP, looked up *through* that proxy.

    Asking through the proxy means the service sees the exit address itself, so
    the answer cannot be confused by our own egress. Returns (cc, ip) or
    (None, None) when the lookup fails.
    """
    s = creq.Session(impersonate="chrome",
                     proxies={"http": proxy_url, "https": proxy_url},
                     timeout=timeout)
    try:
        r = s.get(GEO_URL)
        j = r.json()
        if j.get("status") == "success":
            return (j.get("countryCode") or "").upper() or None, j.get("query")
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    return None, None


if __name__ == "__main__":
    print("shows:", ", ".join(SHOW_LABEL[k] for k in DEFAULT_ORDER))
    print("order:", DEFAULT_ORDER)
