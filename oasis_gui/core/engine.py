#!/usr/bin/env python3
"""Worker pool that drives the registration over a mailbox queue.

One worker = one mailbox at a time = one random identity = one proxy lease.
Everything the operator asked to be random is drawn per claim, and everything
that must be unique is enforced by the store's constraints rather than by a
check-then-act race between threads.
"""
import random
import threading
import time
import traceback

from . import identity as ident_mod
from . import registrar
from .browser_registrar import BrowserRegistrar
from .mailbox import make_mailbox
from .relay import RelayPool


class Engine:
    def __init__(self, store, proxy_pool, log_cb, event_cb, config=None):
        self.store = store
        self.pool = proxy_pool
        self.log_cb = log_cb            # (level, message)
        self.event_cb = event_cb        # (kind, payload)
        self.config = config or {}
        self._threads = []
        self._stop = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self._done = 0
        # browser mode needs the relay's Google split routing
        self.relays = RelayPool(log=lambda m: self._log("debug", m))
        self.browser = BrowserRegistrar(
            self.relays, headless=True, log=lambda m: self._log("info", m.strip()))
        # proxy url -> exit country code, so the identity's address matches the
        # IP the site will see. One lookup per distinct exit, not per account.
        self._geo_cache = {}
        self._geo_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _proxy_country(self, proxy, dial=None):
        """ISO code of the proxy's exit, cached per proxy.

        `dial` is what curl_cffi should actually talk to (the local relay when
        the upstream is chained); the lookup has to go the same way or it would
        geolocate the wrong hop.
        """
        with self._geo_lock:
            if proxy in self._geo_cache:
                return self._geo_cache[proxy]
        cc, ip = registrar.exit_geo(dial or proxy)
        with self._geo_lock:
            self._geo_cache[proxy] = cc
        if cc:
            self._log("info", f"exit {ip or '?'} -> {cc} "
                              f"(identities for this proxy will use {cc} addresses)")
        else:
            # A random country here produces identities the registration form
            # rejects (its phone field is fixed at +1), so fall back to US.
            cc = "US"
            self._log("warn", f"could not geolocate exit of "
                              f"{proxy.split('@')[-1]}; assuming US")
        return cc

    # ------------------------------------------------------------------ helpers
    def _log(self, level, msg):
        try:
            self.log_cb(level, msg)
        except Exception:
            pass

    def _emit(self, kind, payload=None):
        try:
            self.event_cb(kind, payload)
        except Exception:
            pass

    def _fresh_identity(self, rnd, country=None):
        """A person not already present in the store.

        `country` comes from the proxy's exit; when it is not one of the
        supported regions the identity is drawn from the pool at random.
        """
        for _ in range(60):
            cand = ident_mod.random_identity(rnd, country=country)
            if not self.store.identity_taken(cand):
                return cand
        return None

    @property
    def running(self):
        return self._running

    @property
    def busy(self):
        """True while any worker thread is still alive.

        `running` stays True until finish() is called, so a caller waiting for
        the queue to drain has to watch the threads instead (service.py does).
        """
        return any(t.is_alive() for t in self._threads)

    def stats(self):
        return self.store.stats()

    # ------------------------------------------------------------------- control
    def start(self, threads=4, order=None, link_timeout=300,
              delay_between=0.0, mode="hybrid"):
        if self._running:
            self._log("warn", "engine already running")
            return
        order = order or registrar.DEFAULT_ORDER
        if not self.pool.urls():
            self._log("error", "no proxies configured - aborting start")
            return
        pending = self.store.stats().get("pending", 0)
        if not pending:
            self._log("warn", "no pending mailboxes in the queue")
            return

        self._stop.clear()
        self._running = True
        self._done = 0
        # Relays are per-upstream and cached; drop them so a changed
        # google_proxy setting takes effect on this run.
        self.relays.stop_all()
        self.relays.google_fallback = self.config.get("google_proxy", "") or ""
        self.relays.front = self.config.get("front_proxy", "") or ""
        self.browser.strict_egress = bool(self.config.get("strict_egress", False))
        label = {"hybrid": "混合 (浏览器取 captcha + curl_cffi 提交)",
                 "browser": "浏览器 (Playwright 全流程)"}.get(mode, mode)
        gp = self.relays.google_fallback or "不分流（全部走上游）"
        self._log("info", f"engine start: {threads} threads, {pending} pending, "
                          f"mode={label}, google={gp}, shows {' > '.join(order)}")
        for i in range(threads):
            t = threading.Thread(target=self._worker, name=f"worker-{i + 1}",
                                 args=(i + 1, order, link_timeout,
                                       delay_between, mode), daemon=True)
            t.start()
            self._threads.append(t)
        self._emit("started", {"threads": threads, "mode": mode})

    def stop(self):
        self._stop.set()
        self._log("warn", "stop requested - workers finish their current account")

    def join(self):
        for t in self._threads:
            t.join()
        self._threads = []
        self.relays.stop_all()

    # -------------------------------------------------------------------- worker
    def _worker(self, wid, order, link_timeout, delay_between, mode):
        rnd = random.Random()
        tag = f"w{wid}"
        try:
            self._loop(wid, tag, rnd, order, link_timeout,
                       delay_between, mode)
        except Exception:
            self._log("error", f"{tag}: worker crashed\n{traceback.format_exc()[-800:]}")
        self._log("info", f"{tag}: worker stopped ({self._done} ok so far)")

    def _loop(self, wid, tag, rnd, order, link_timeout, delay_between,
              mode):
        while not self._stop.is_set():
            rows = self.store.claim_pending(1)
            if not rows:
                self._log("info", f"{tag}: queue empty, worker exits")
                break
            acct = rows[0]
            account_id = acct["id"]

            # one registration per account, ever
            if self.store.registration_exists(account_id, registrar.ARTIST_ID):
                self._log("warn", f"{tag}: {acct['email']} already registered, skip")
                self.store.mark_registered(account_id)
                continue

            # proxy first: the identity's country is derived from its exit, so
            # the home address matches the IP the site will actually see
            proxy = self.pool.acquire()
            # A short-lived upstream is usually unreachable directly and has to
            # be dialled through the front proxy. curl_cffi cannot chain, so
            # when a chain is in play it talks to the local relay instead.
            dial = (self.relays.get(proxy).url if self.relays.is_chained(proxy)
                    else proxy)
            if dial != proxy:
                self._log("info", f"chained via {self.relays.front}; "
                                  f"curl_cffi -> {dial}")
            country = self._proxy_country(proxy, dial)
            if mode == "browser" and country != "US":
                # The registration form's phone field is pinned to +1 and does
                # not follow the location, so a DE/FR number is rejected as
                # invalid. Browser mode therefore always uses a US identity.
                self._log("info", f"{tag}: exit is {country}; using a US identity "
                                  f"(the form's phone field is +1 only)")
                country = "US"

            ident = self._fresh_identity(rnd, country)
            if not ident:
                self.store.release(account_id, "no unique identity available")
                self._log("error", f"{tag}: identity space exhausted, stopping")
                break

            try:
                self.store.bind_identity(account_id, ident)
            except Exception as e:
                # lost the uniqueness race against another worker; requeue
                self.store.release(account_id, f"identity clash: {e}")
                self._log("warn", f"{tag}: identity clash, requeueing {acct['email']}")
                continue

            protocol = acct.get("protocol") or "graph"
            mail_proxy = self.config.get("mail_proxy", "") or ""
            mailbox = make_mailbox({
                "email": acct["email"], "password": acct["password"],
                "client_id": acct["client_id"],
                "client_secret": acct.get("client_secret") or "",
                "refresh_token": acct["refresh_token"],
                # ignored by the Microsoft/Google readers, used by HmeMailbox
                "hme_base": self.config.get("hme_base", "") or "",
                "hme_password": self.config.get("hme_password", "") or "",
                "hme_account": acct.get("client_id") or ""}, protocol, mail_proxy)

            session = None
            t0 = time.time()
            think = float(self.config.get("think_time", 45) or 0)
            vok = bool(self.config.get("verify_success", False))
            vto = int(self.config.get("success_timeout", 180) or 180)
            scap = bool(self.config.get("send_captcha", False))
            try:
                if mode == "browser":
                    self._log("info", f"{tag}: {acct['email']} | mode=browser | "
                                      f"mail={protocol} | proxy={proxy.split('@')[-1]}")
                    result = self.browser.register(
                        mailbox, ident, order, proxy_url=proxy,
                        link_timeout=link_timeout,
                        verify_success=vok, success_timeout=vto,
                        send_captcha=scap,
                        log=lambda m: self._log("info", f"{tag} {m.strip()}"))
                elif mode == "hybrid":
                    session = registrar.make_session(
                        dial, timeout=self.config.get("http_timeout", 60))
                    self._log("info", f"{tag}: {acct['email']} | mode=hybrid | "
                                      f"mail={protocol} | proxy={proxy.split('@')[-1]}")
                    result = self.browser.register_hybrid(
                        session, mailbox, ident, order, proxy_url=proxy,
                        link_timeout=link_timeout, think_time=think,
                        verify_success=vok, success_timeout=vto,
                        send_captcha=scap,
                        log=lambda m: self._log("info", f"{tag} {m.strip()}"))
                else:
                    raise RegistrationError(
                        f"unknown mode {mode!r}: only hybrid and browser carry a "
                        f"captcha token")

                self.store.record_registration(
                    account_id, registrar.ARTIST_ID, registrar.PAGE_ID,
                    result["session_id"], registrar.EVENTS_POLL,
                    result["poll_answer_ids"], result["journey"],
                    result["token"], result["response"], mode)
                if (result or {}).get("evidence") == "page":
                    # page-confirmed only: distinguishable from mail-verified
                    self.store.mark_submitted(
                        account_id, "页面确认注册成功（浏览器模式不发成功邮件）")
                else:
                    self.store.mark_registered(account_id)
                self.store.update_refresh_token(account_id,
                                                mailbox.refresh_token)
                self.pool.report(proxy, True)
                with self._lock:
                    self._done += 1
                page_only = (result or {}).get("evidence") == "page"
                self._log("ok", f"{tag}: {'SUBMITTED' if page_only else 'REGISTERED'} "
                                f"{acct['email']} ({result['elapsed']}s, {mode}) - "
                                f"{registrar.SHOW_LABEL[order[0]]}"
                                + ("  [页面确认，无成功邮件]"
                                   if page_only else "  [邮件确认]"))
                self._emit("registered", {
                    "email": acct["email"],
                    "name": f"{ident['first_name']} {ident['last_name']}",
                    "phone": ident["phone"],
                    "city": ident["location"]["city"],
                    "show": registrar.SHOW_LABEL[order[0]],
                    "mode": mode,
                    "evidence": (result or {}).get("evidence") or "mail",
                    "elapsed": result["elapsed"]})
            except Exception as e:
                err = str(e)[:300]
                self.store.fail(account_id, err)
                self.pool.report(proxy, False, err)
                self._log("error", f"{tag}: FAILED {acct['email']} - {err}")
                self._emit("failed", {"email": acct["email"], "error": err})
                if self.config.get("debug"):
                    self._log("debug", traceback.format_exc()[-600:])
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                self._emit("stats", self.store.stats())
                if delay_between:
                    time.sleep(delay_between)

    def finish(self):
        self._running = False
        self._emit("stopped", {"stats": self.store.stats()})
