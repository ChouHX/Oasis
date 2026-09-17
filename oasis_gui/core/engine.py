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
from .browser_registrar import BrowserRegistrar, TransientError
from .mailbox import TOKEN_RE, make_mailbox
from .relay import RelayPool


# Retries for failures that never reached the site (dead proxy, dropped
# connection). Failures the site itself decided are not retried.
TRANSIENT_RETRIES = 1
# A requeued account goes straight back to 'pending', so the same worker claims
# it again immediately - with a dead proxy that is an endless loop inside one
# round. Stop the round once this many transport failures pile up and let the
# caller back off.
TRANSIENT_PER_ROUND = 4

# How long a prefetched verification link is assumed good for. The mail link
# carries a short-lived token, so a request fired long before the account is
# picked up is worse than useless - it would make the worker wait on a link
# that has already expired.
PREFETCH_MAX_AGE = 600
# Seconds between prefetch requests, so a burst does not look like abuse.
PREFETCH_GAP = 1.5


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
        # Per-round cap on how many accounts to touch; 0 means no cap.
        self._limit = 0
        self._claimed = 0
        # How many verification mails this round may still ask for, when the
        # round is capped. None means "no cap, keep the queue warm".
        self._prefetch_budget = None
        # browser mode needs the relay's Google split routing
        self.relays = RelayPool(log=lambda m: self._log("debug", m))
        self.browser = BrowserRegistrar(
            self.relays, headless=True, log=lambda m: self._log("info", m.strip()))
        # proxy url -> exit country code, so the identity's address matches the
        # IP the site will see. One lookup per distinct exit, not per account.
        self._geo_cache = {}
        self._geo_lock = threading.Lock()
        # email -> epoch the verification mail was requested at. Filled by the
        # prefetcher so a worker usually finds its mail already on the way.
        self._mail_since = {}
        self._prefetch_stop = threading.Event()
        self._prefetch_thread = None

    # -------------------------------------------------------------- prefetching
    def _start_prefetcher(self, threads):
        """Request the verification mail for upcoming accounts ahead of time.

        The mail takes ~10s to arrive, and that was dead time inside the worker:
        it had nothing to do but poll. Asking for the next few accounts' mails
        up front, from curl, moves that wait off the critical path entirely -
        the worker opens the mail link that is already sitting in the inbox
        instead of waiting for it to show up.

        Depth is kept small on purpose. Requests are cheap but not free, and a
        verification link goes stale, so asking for twenty accounts at once
        would mostly produce expired links.
        """
        self._prefetch_stop.clear()
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_loop, args=(max(1, threads),),
            daemon=True, name="mail-prefetch")
        self._prefetch_thread.start()

    def _prefetch_loop(self, depth):
        while not self._prefetch_stop.is_set() and not self._stop.is_set():
            try:
                self._prefetch_once(depth)
            except Exception as e:
                self._log("debug", f"prefetch: {type(e).__name__}: {e}")
            self._prefetch_stop.wait(2)

    def _prefetch_once(self, depth):
        now = time.time()
        # A round with a cap only has that many accounts to fetch mail for, so
        # the prefetcher gets the same cap. Without this it keeps working down
        # the queue: "上限 1" still fired verification requests at the next
        # fifty addresses, which is useless (they are not going to be touched)
        # and is exactly the pattern that gets an address rate-limited.
        with self._lock:
            if self._prefetch_budget is not None:
                room = self._prefetch_budget
                if room <= 0:
                    return
                depth = min(depth, room)
        # one request per pending account that is not already covered
        wanted = []
        fresh = {e: t for e, t in self._mail_since.items()
                 if now - t < PREFETCH_MAX_AGE}
        with self._lock:
            self._mail_since.clear()
            self._mail_since.update(fresh)
        for acct in self.store.pending_emails(max(depth * 3, 6)):
            if len(wanted) >= depth:
                break
            email = acct["email"]
            if email in fresh:
                continue
            # The prefetcher runs before any worker, so it is the first thing to
            # touch a fresh address - and if it asks for a mail without looking,
            # it manufactures the dead session the worker then fails on. Same
            # rule as the worker: if the mail is already in the inbox, use it.
            if self._mail_in_inbox(acct):
                continue
            wanted.append(email)
        if not wanted:
            return
        proxy = self.pool.acquire()
        relay = self.relays.get(proxy)
        session = registrar.make_session(relay.url, timeout=45)
        try:
            for email in wanted:
                if self._stop.is_set():
                    return
                # Claim the slot before the request, not after. Recording it
                # afterwards leaves a window where a worker has already claimed
                # the account but cannot see the prefetch, so it sends its own
                # verify - and its later mail_since then filters out the mail
                # the prefetch already triggered, making the account slower.
                with self._lock:
                    if self._prefetch_budget is not None:
                        if self._prefetch_budget <= 0:
                            return
                        self._prefetch_budget -= 1
                    self._mail_since[email] = time.time()
                # Persisted as well: an in-memory record dies with the process,
                # and the next boot would ask the site again - which costs the
                # address its usable session rather than refreshing anything.
                self.store.mark_mail_requested(email, self._mail_since[email])
                try:
                    registrar.request_verification(
                        session, email,
                        lambda m: self._log("debug", f"prefetch{m}"))
                    self._log("info", f"prefetch: 已为 {email} 触发验证邮件")
                except Exception as e:
                    # give the slot back so a worker will retry it
                    with self._lock:
                        self._mail_since.pop(email, None)
                        if self._prefetch_budget is not None:
                            self._prefetch_budget += 1
                    self._log("debug", f"prefetch {email} failed: "
                                       f"{type(e).__name__}")
                self._prefetch_stop.wait(PREFETCH_GAP)
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _ensure_mail_requested(self, mailbox, email, relay_url):
        """Ask the site for this address's verification mail, once and once only.

        Measured: the site answers a repeat request with a brand new session
        whose token reads `closed: true`, while the original session stays
        usable. So a second request does not refresh anything - it manufactures
        a session the worker will pick up and fail on, which is how a pool ends
        up with a dozen dead sessions per address.

        The timestamp lives in the store so this promise survives a restart.
        Returns the epoch to use as `mail_since`, so the caller never has to
        request it itself.
        """
        sent = self.store.mail_requested(email)
        if sent:
            return sent

        # Look before asking. An address imported today may already have been
        # asked for a mail by an earlier build or by hand, and that mail is
        # still sitting in the inbox with the only usable session attached to
        # it. Asking again would not refresh it - it would add a dead session
        # and leave the worker picking the newest mail, which is the dead one.
        existing = self._existing_mail_time(mailbox, email)
        if existing:
            self.store.mark_mail_requested(email, existing)
            self._log("info", f"{email} 的收件箱里已有验证邮件，直接用它"
                              f"（不再向站点请求，重复请求会作废会话）")
            return existing

        session = registrar.make_session(relay_url, timeout=45)
        try:
            registrar.request_verification(
                session, email, lambda m: self._log("debug", f"    {m.strip()}"))
        except Exception as e:
            # Nothing recorded, so the next attempt is free to try again.
            self._log("debug", f"mail request for {email} failed: "
                               f"{type(e).__name__}")
            return None
        finally:
            try:
                session.close()
            except Exception:
                pass
        sent = time.time()
        self.store.mark_mail_requested(email, sent)
        self._log("info", f"已为 {email} 请求验证邮件（只请求一次，重复请求会"
                          f"让站点把新会话标记为已关闭）")
        return sent

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
              delay_between=0.0, mode="browser", limit=0):
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
        # One browser per worker, not one per proxy: only a worker can be using
        # one at a time, and a pool of ten upstreams was turning into ten
        # chromium processes at ~210MB each.
        self.browser.cap_browsers(threads)
        self._limit = max(0, int(limit or 0))
        self._claimed = 0
        # One mail request per account this round will actually touch.
        self._prefetch_budget = self._limit if self._limit else None
        self._transient_left = max(TRANSIENT_PER_ROUND, threads)
        with self._lock:
            self._mail_since.clear()
        if self._limit:
            self._log("info", f"本轮最多处理 {self._limit} 个账号"
                              f"（队列里有 {pending} 个）")
        self._start_prefetcher(min(threads, self._limit) if self._limit
                               else threads)
        self._running = True
        self._done = 0
        # Relays are per-upstream and cached; drop them so a changed
        # google_proxy setting takes effect on this run.
        self.relays.stop_all()
        self.relays.google_fallback = self.config.get("google_proxy", "") or ""
        self.relays.front = self.config.get("front_proxy", "") or ""
        # Resolve artistId / pageId from the site once per run. Pinned copies
        # would silently fail every request the day a new registration round
        # opens, so ask; if the lookup fails the captured values stay in place.
        try:
            proxy = self.pool.acquire()
            session = registrar.make_session(self.relays.get(proxy).url, timeout=30)
            try:
                # resolve_page takes a one-argument logger, not _log(level, msg)
                registrar.resolve_page(
                    session, lambda m: self._log("info", m.strip()))
            finally:
                session.close()
        except Exception as e:
            self._log("warn", f"page resolve 跳过（{type(e).__name__}: "
                              f"{str(e)[:70]}），沿用内置的 artist/page id")
        label = ("纯 HTTP (curl_cffi，captcha 留空)" if mode == "http"
                 else "浏览器 (驱动官方 SPA 全流程)")
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
    def _take_slot(self):
        """Take one of this round's account slots, when a limit is set.

        A limit of 0 means "no limit" and costs nothing. The slot is taken
        before the account is claimed, so the number is what the round started
        with rather than what it managed to finish: a run whose accounts keep
        failing would otherwise never reach the count the operator asked to try
        with, which defeats the point of trying with a count.
        """
        if not self._limit:
            return True
        with self._lock:
            if self._claimed >= self._limit:
                return False
            self._claimed += 1
            return True

    def _worker(self, wid, order, link_timeout, delay_between, mode):
        rnd = random.Random()
        tag = f"w{wid}"
        try:
            self._loop(wid, tag, rnd, order, link_timeout,
                       delay_between, mode)
        except Exception:
            self._log("error", f"{tag}: worker crashed\n{traceback.format_exc()[-800:]}")
        self._log("info", f"{tag}: worker stopped ({self._done} ok so far)")

    def _mail_in_inbox(self, acct):
        """Is a verification mail for this account already sitting in its inbox?

        Returns the arrival time and records it, or None. Costs one search, and
        it is what keeps the prefetcher from re-asking for addresses that were
        already asked for by an earlier run or by hand.
        """
        try:
            mailbox = make_mailbox(self._mail_cred(acct),
                                   acct.get("protocol") or "graph",
                                   self.config.get("mail_proxy", "") or "")
        except Exception:
            return None
        stamp = self._existing_mail_time(mailbox, acct["email"])
        try:
            mailbox.close()
        except Exception:
            pass
        if stamp:
            self.store.mark_mail_requested(acct["email"], stamp)
            self._log("info", f"{acct['email']} 收件箱里已有验证邮件，跳过请求")
        return stamp

    def _mail_cred(self, acct):
        return {
            "email": acct["email"], "password": acct["password"],
            "client_id": acct["client_id"],
            "client_secret": acct.get("client_secret") or "",
            "refresh_token": acct["refresh_token"],
            "hme_base": self.config.get("hme_base", "") or "",
            "hme_password": self.config.get("hme_password", "") or "",
            "hme_account": acct.get("client_id") or "",
        }

    @staticmethod
    def _existing_mail_time(mailbox, email):
        """When the earliest verification mail for this address arrived, if any.

        Earliest on purpose. Measured on one address: the token from the first
        mail read closed=false, a freshly requested second mail read closed=true,
        and the first one was still closed=false afterwards. The older mail is
        the one attached to the usable session.
        """
        try:
            for body, stamp in reversed(mailbox.messages(limit=10)):
                if TOKEN_RE.search(body):
                    return stamp
        except Exception:
            return None
        return None

    def _loop(self, wid, tag, rnd, order, link_timeout, delay_between,
              mode):
        while not self._stop.is_set():
            if not self._take_slot():
                self._log("info", f"{tag}: 本轮已跑满 {self._limit} 个，worker 退出")
                break
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
            mailbox = make_mailbox(self._mail_cred(acct), protocol, mail_proxy)

            t0 = time.time()
            vok = bool(self.config.get("verify_success", False))
            vto = int(self.config.get("success_timeout", 180) or 180)
            try:
                self._log("info", f"{tag}: {acct['email']} | mode={mode} | "
                                  f"mail={protocol} | proxy={proxy.split('@')[-1]}")
                # Transport failures say nothing about the account, so a retry
                # here is cheap insurance against one dead connection costing a
                # queue pass. Anything the site itself decides is not retried.
                for attempt in range(TRANSIENT_RETRIES + 1):
                    try:
                        mail_since = self._ensure_mail_requested(
                            mailbox, acct["email"], self.relays.get(proxy).url)
                        if mode == "http":
                            result = self._register_http(
                                mailbox, ident, order, proxy, link_timeout, vto,
                                mail_since,
                                log=lambda m: self._log("info", f"{tag} {m.strip()}"))
                        else:
                            result = self.browser.register(
                                mailbox, ident, order, proxy_url=proxy,
                                link_timeout=link_timeout,
                                verify_success=vok, success_timeout=vto,
                                mail_since=mail_since,
                                log=lambda m: self._log("info", f"{tag} {m.strip()}"))
                        break
                    except TransientError as e:
                        self.pool.report(proxy, False, str(e))
                        if attempt < TRANSIENT_RETRIES:
                            self._log("warn", f"{tag}: {acct['email']} transport "
                                              f"failure ({str(e)[:80]}), attempt "
                                              f"{attempt + 2}/{TRANSIENT_RETRIES + 1}")
                            time.sleep(2)
                            continue
                        raise

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
            except TransientError as e:
                # Never reached the site, so do not write the account off. Mark
                # the exit unhealthy and hand the account back to the queue; an
                # iCloud quota makes burning an address expensive.
                err = str(e)[:300]
                self.pool.report(proxy, False, err)
                self.store.release(account_id,
                                   f"传输失败，未提交，已退回队列：{err}")
                self._log("warn", f"{tag}: 退回队列 {acct['email']} - "
                                  f"传输失败，账号未消耗：{err}")
                self._emit("requeued", {"email": acct["email"], "error": err})
                with self._lock:
                    self._transient_left -= 1
                    left = self._transient_left
                if left <= 0:
                    self._log("error", "本轮传输失败过多（代理池可能整体不可用），"
                                       "提前结束本轮，交由外层退避")
                    self.stop()
            except registrar.RegistrationClosed as e:
                # The site has already closed this session, so the form renders
                # without accepting anything. Measured: every address that kept
                # failing came back closed=true from check-verification, while
                # the one that registered came back false. Nothing to retry and
                # nothing wrong with the exit - record it once and move on. Left
                # as 'submitted' rather than 'failed' because the address really
                # has been dealt with (here or by hand); because it is no longer
                # 'pending' the queue will not pick it up again.
                err = str(e)[:300]
                self.store.mark_submitted(account_id, err)
                self._log("warn", f"{tag}: {acct['email']} 站点已关闭该会话，"
                                  f"不再重试 - {err[:120]}")
                self._emit("closed", {"email": acct["email"], "reason": err})
            except Exception as e:
                err = str(e)[:300]
                self.store.fail(account_id, err)
                self.pool.report(proxy, False, err)
                self._log("error", f"{tag}: FAILED {acct['email']} - {err}")
                self._emit("failed", {"email": acct["email"], "error": err})
                if self.config.get("debug"):
                    self._log("debug", traceback.format_exc()[-600:])
            finally:
                # The Gmail alias reader holds an IMAP session open between
                # polls (logging in per poll cost more than the search itself),
                # so it has to be told when this account is done with it. A
                # no-op for every reader that reconnects each time.
                try:
                    mailbox.close()
                except Exception:
                    pass
                self._emit("stats", self.store.stats())
                if delay_between:
                    time.sleep(delay_between)

    def _register_http(self, mailbox, ident, order, proxy, link_timeout,
                       success_timeout, mail_since, log=print):
        """The whole registration over curl_cffi, with no browser in it.

        Dials the same local relay the browser flow uses, so the traffic leaves
        from the same place and only the client differs. The success mail is
        always waited for here: nothing renders in this mode, so unlike the
        browser flow there is no page to confirm the registration first, and
        `verify_success` (which exists to decide whether a page-confirmed
        registration is worth waiting on) has nothing to say about it.
        """
        relay = self.relays.get(proxy)
        session = registrar.make_session(relay.url, timeout=90)
        try:
            return registrar.register_over_http(
                session, mailbox, ident, order,
                link_timeout=link_timeout, success_timeout=success_timeout,
                mail_since=mail_since, log=log)
        except registrar.RegistrationError:
            raise
        except Exception as e:
            # The same split browser_registrar makes: a transport failure says
            # nothing about the account and belongs back in the queue, while
            # anything else is a real error worth seeing.
            if registrar._is_transient(e):
                raise TransientError(str(e)[:200]) from e
            raise
        finally:
            try:
                session.close()
            except Exception:
                pass

    def finish(self):
        self._running = False
        self._prefetch_stop.set()
        self._emit("stopped", {"stats": self.store.stats()})
