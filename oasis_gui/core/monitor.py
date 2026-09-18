#!/usr/bin/env python3
"""中签检测：定时把每个已导入账号的收件箱翻一遍，看 Oasis 有没有来信。

和上一版程序的关系
------------------
程序不再向站点提交任何东西。实测重复请求只会作废会话，而活动已经结束，注册
这件事没有意义了；这里唯一的动作是**读** —— 每个账号一次收件箱搜索，把结果
交给 `core.hitcheck` 判成中签或未中签。

节奏由 interval 控制，一轮一轮跑：每轮把所有还没中签的账号按「最久没查过」
的顺序过一遍，然后睡到下一轮。账号读不到时只记 `check_error`，既不清已有的
中签结论，也不把账号踢出队列 —— 读不到信不等于没中签。

成本上唯一值得设计的地方是分组：iCloud 别名不是独立邮箱，几十个别名的信都
落在同一个 Gmail 收件箱里，每个别名登一次 IMAP 是纯粹的浪费。所以同一收件箱
的别名共用一条连接串行扫，不同的收件箱之间才并行 —— 并发上限因此是「同时打开
几条 IMAP 连接」，不是「同时读几封信」。
"""
import threading
import time
import traceback

from . import hitcheck
from .mailbox import (ALIAS_PROTOCOL, MailAuthError,
                      MailSearchUnsupported, make_mailbox)

# 默认值。都可由 config 覆盖，改一处就能在 Web UI 上调。
DEFAULT_INTERVAL = 300          # 秒；一轮跑完到下一轮开始之间的等待
DEFAULT_THREADS = 2             # 同时打开的 IMAP/收件箱分组数
DEFAULT_LOOKBACK_DAYS = 30      # 首次检测回看多少天（0 = 不设基线，看全部历史）
DEFAULT_PER_PAGE = 20           # 每个邮箱取最近多少封信来判
# 一条线程超过这么久还没读完一个账号，就当作它已经不在了（TTL 兜底）。
INFLIGHT_TTL = 900
# 一次扫描里单封邮件处理的停顿：这些邮箱是别人的真实收件箱，检测程序不该在
# 收件箱里打出连续不断的请求尖峰。
PAGE_GAP = 0.15


class HitMonitor:
    """一轮一轮扫收件箱的调度器。线程模型：一个调度线程 + 每轮一个线程池。"""

    def __init__(self, store, log_cb, event_cb, config=None):
        self.store = store
        self.log_cb = log_cb                    # (level, message)
        self.event_cb = event_cb                # (kind, payload)
        self.config = config or {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._round = 0
        self._hits = 0
        self._checked = 0
        self._failures = 0
        self._last_sweep = 0.0
        # 生效中的参数。在 __init__ 里就填好，sweep() 才能被单独调用 —— 测试、
        # 脚本和 oneshot 都直接跑一轮，不经过 start() 的调度线程。
        self.threads = max(1, int(self.setting("threads", None, DEFAULT_THREADS)))
        self.interval = max(5, int(self.setting("interval", None, DEFAULT_INTERVAL)))
        self.lookback_days = float(self.setting("lookback_days", None,
                                                DEFAULT_LOOKBACK_DAYS) or 0)
        self.per_page = max(1, int(self.setting("per_page", None,
                                                DEFAULT_PER_PAGE)))
        self.skip_hits = bool(self.setting("skip_hits", None, True))
        # 一轮最多查几个账号，0 = 全部。留给「先拿一两个试」和排查单个邮箱。
        self.limit = max(0, int(self.setting("limit", None, 0) or 0))
        # 服务端只拉 Oasis 的来信（见 core.mailbox 的 OASIS_SENDER_HINT）。
        # 首次检测一个账号时始终走全量，之后才用粗筛 —— 理由在 _read_and_judge 里。
        self.only_opted = bool(self.setting("only_opted", None, True))
        self.mail_filter = bool(self.setting("mail_filter", None, True))
        # 某个服务端明确拒绝粗筛之后置位：本轮剩下的账号直接走全量，不浪费一次
        # 必然失败的搜索。
        self.mail_filter_broken = False
        # 本轮被放弃等待的账号（见 _mark_overdue）。
        self._abandoned = set()
        self._rounds = 0
        # 一轮的时间预算。一轮要给几十上百个邮箱轮一遍，而单个坏邮箱（出口不通、
        # 被墙、账号被限）能让一次读信花掉好几分钟 —— 实测过一个直连不通的 Gmail
        # 把整轮拖住六分钟以上。到点就停，剩下的下一轮优先重试；预算而不是
        # 「更长的超时」，是因为预算能给出一个可预期的轮次时长。
        self.sweep_budget = max(600.0, self.interval * 2.0)
        # 正在读的账号 -> 开始时刻。超时放弃的线程还在收尾，下一轮不能对同一个
        # 账号再开一条连接，否则连接数会一轮一轮地堆上去。
        self._reading = {}

    # ------------------------------------------------------------------ fixtures
    def setting(self, key, explicit, fallback):
        """优先级：显式传入 > 配置文件 > 内置默认。

        三层都要有，因为三个调用方各有各的真相来源：service 每轮把 Web UI 的
        值读出来显式传进来，桌面版调 start() 时带参数，而脚本/测试直接调
        sweep() 时只能靠 config 或默认值。
        """
        if explicit is not None:
            return explicit
        value = self.config.get(key)
        return fallback if value in (None, "") else value

    @property
    def running(self):
        return self._running

    def stats(self):
        with self._lock:
            return {"round": self._round, "hits": self._hits,
                    "checked": self._checked, "failed": self._failures,
                    "last_sweep": self._last_sweep,
                    "next_in": max(0, int(self.interval -
                                          (time.time() - self._last_sweep)))
                    if self._last_sweep else 0}

    def _log(self, level, msg):
        try:
            self.log_cb(level, msg)
        except Exception:
            pass

    def _emit(self, kind, payload=None):
        try:
            self.event_cb(kind, payload or {})
        except Exception:
            pass

    # ------------------------------------------------------------------- control
    def start(self, threads=None, interval=None, lookback_days=None,
              per_page=None, skip_hits=None, limit=None, mail_filter=None,
              only_opted=None, rounds=0):
        """开始巡检。

        `rounds=0` 是常驻（一直跑，每轮之间等 interval）；`rounds=1` 只跑一轮
        就退出，用于 OASIS_ONESHOT 那种一次性检查。
        """
        if self._running:
            self._log("warn", "检测已在运行")
            return False
        self.threads = max(1, int(self.setting("threads", threads, DEFAULT_THREADS)))
        self.interval = max(5, int(self.setting("interval", interval,
                                                DEFAULT_INTERVAL)))
        self.lookback_days = float(self.setting(
            "lookback_days", lookback_days, DEFAULT_LOOKBACK_DAYS) or 0)
        self.per_page = max(1, int(self.setting("per_page", per_page,
                                                DEFAULT_PER_PAGE)))
        self.skip_hits = bool(self.setting("skip_hits",
                                           True if skip_hits is None
                                           else skip_hits, True))
        self.limit = max(0, int(self.setting("limit", limit, 0) or 0))
        self.only_opted = bool(self.setting("only_opted", only_opted, True))
        self.mail_filter = bool(self.setting("mail_filter", mail_filter, True))
        self.mail_filter_broken = False
        self._abandoned = set()
        self._rounds = int(rounds or 0)
        self._stop.clear()
        self._wake.clear()
        self._running = True
        stats = self.store.stats()
        scope = (f"{stats.get('opted', 0)}/{stats.get('total', 0)} 个已预约账号"
                 if self.only_opted else f"{stats.get('total', 0)} 个账号（含未标记）")
        self._log("info", f"中签检测启动：{scope} · {self.threads} 并发 · "
                          f"每 {self.interval}s 一轮 · 回看 "
                          f"{int(self.lookback_days)} 天 · "
                          f"{'跳过已中签' if self.skip_hits else '重复检查已中签'}")
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="hit-monitor")
        self._thread.start()
        self._emit("started", {"threads": self.threads, "interval": self.interval})
        return True

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """让当前轮结束的等待立刻结束 —— 「立即再查一轮」按钮用。"""
        self._wake.set()

    def join(self):
        if self._thread:
            self._thread.join()
            self._thread = None

    def finish(self):
        self._running = False
        self._emit("stopped", {"stats": self.store.stats(), "monitor": self.stats()})

    # ---------------------------------------------------------------------- loop
    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    self.sweep()
                except Exception:
                    self._log("error", "检测轮次异常：\n"
                                       + traceback.format_exc()[-600:])
                if self._rounds and self._round >= self._rounds:
                    self._log("info", f"已完成 {self._round} 轮（单轮模式），退出")
                    break
                # 等待期间收到 stop 或 wake 就立刻醒；不用 sleep 是为了让
                # 「立即检查」按钮和进程退出都不用等满一个 interval。
                self._wake.wait(self.interval)
                self._wake.clear()
        finally:
            self._running = False
            self._emit("stopped", {"stats": self.store.stats(),
                                   "monitor": self.stats()})

    def sweep(self):
        """跑完一轮：把还没中签的账号全部读一遍。返回本轮统计。"""
        started = time.time()
        targets = [a for a in self.store.check_targets(
                       skip_hits=self.skip_hits, limit=self.limit or None,
                       only_opted=self.only_opted)
                   if not self._busy(a["id"])]
        # 上限是「本轮读几个」，所以 busy 过滤掉的那几个要从后面补回来，
        # 否则设了 3 却只读 1 个 —— 上限就成了掷骰子。
        if self.limit:
            short = self.limit - len(targets)
            if short > 0:
                more = [a for a in self.store.check_targets(
                            skip_hits=self.skip_hits, limit=self.limit + short,
                            only_opted=self.only_opted)
                        if a not in targets and not self._busy(a["id"])]
                targets += more[:short]
        self._round += 1
        self._last_sweep = started
        if not targets:
            stats = self.store.stats()
            why = ("全部已中签" if self.skip_hits else "没有待检测的账号")
            if self.only_opted and not stats.get("opted"):
                why = "池子里没有已预约的账号（用界面上的标记功能把要查的加进来）"
            self._log("info", f"第 {self._round} 轮：没有需要检测的账号（{why}）")
            return {"targets": 0, "checked": 0, "hits": 0, "failed": 0}
        groups = self._group(targets)
        before_hits = self.store.stats().get("hits", 0)
        budget = self.sweep_budget
        self._log("info", f"第 {self._round} 轮：{len(targets)} 个账号 · "
                          f"{len(groups)} 条收件箱连接 · {self.threads} 并发 · "
                          f"本轮预算 {int(budget)}s")
        deadline = started + budget
        # 自己开线程，不用 ThreadPoolExecutor：它的线程不是 daemon 的，解释器退出
        # 时会 join 它们（concurrent.futures.thread._python_exit）。一个卡在读信上
        # 的线程于是能把 `docker stop` 拖到超时被硬杀 —— 实测过脚本打印完结果却
        # 迟迟不退出。daemon 线程随进程结束，退出干净。
        slots = threading.BoundedSemaphore(self.threads)
        running = []                       # [(thread, group)]
        try:
            for group in groups:
                left = deadline - time.time()
                if left <= 0 or not slots.acquire(timeout=max(1.0, left)):
                    skipped = len(groups) - len(running)
                    self._log("warn", f"本轮预算用尽：剩余 {skipped} 条连接留到"
                                      f"下一轮（它们仍排在队首）")
                    break
                t = threading.Thread(target=self._run_group, args=(group, slots),
                                     daemon=True, name="hit-scan")
                t.start()
                running.append((t, group))
            for t, _group in running:
                t.join(timeout=max(0.0, deadline - time.time()))
            # 到点还没回来的，本轮不再等：线程会自己撞上 mailbox 的超时并结束，
            # 而账号不推进 checked_at，所以下一轮仍然优先重试。
            for t, group in running:
                if t.is_alive():
                    for acct in group:
                        self._mark_overdue(acct)
        finally:
            pass
        after = self.store.stats()
        gained = after.get("hits", 0) - before_hits
        self._log("info", f"第 {self._round} 轮完成：{len(targets)} 个账号 · "
                          f"新中签 {gained} · 累计中签 "
                          f"{after.get('hits', 0)}/{after.get('total', 0)}")
        self._emit("sweep", {"round": self._round, "targets": len(targets),
                             "hits": gained, "stats": after,
                             "monitor": self.stats()})
        self._emit("stats", after)
        return {"targets": len(targets), "hits": gained}

    def _run_group(self, group, slots):
        try:
            self._scan_group(group)
        except Exception:
            self._log("debug", "分组扫描异常：\n"
                               + traceback.format_exc()[-400:])
        finally:
            slots.release()

    def _busy(self, account_id):
        """这个账号是不是还有一条连接在读？

        放弃等待不等于线程结束了。加一层 TTL 兜底：一个账号超过 15 分钟还没回来，
        说明那条线程已经不在了（进程重启、句柄泄漏），再挡着它就等于永久漏检。
        """
        started = self._reading.get(account_id)
        if started is None:
            return False
        if time.time() - started > INFLIGHT_TTL:
            self._reading.pop(account_id, None)
            return False
        return True

    def _mark_overdue(self, acct):
        """本轮放弃等它 —— 并且记住放弃了，免得慢线程回来把话说反。

        放弃等待不等于线程结束了：它还会跑完，还会写一次 record_check。那次写入
        默认会推进 checked_at，于是「下一轮优先重试」这行日志在几秒后就成了假话
        —— 账号已经排到队尾去了。所以把 id 记进 _abandoned，由 _scan_one 决定
        这一笔要不要算进进度：结论照写（中签证据不能丢），进度不推进。
        """
        self._abandoned.add(acct["id"])
        self.store.record_check(
            acct["id"], error="本轮读信超时，未计入进度，下一轮优先重试", touch=False)
        self._log("warn", f"读信超时 {acct['email']}：本轮不再等它")
        self._emit("check_failed", {"email": acct["email"],
                                    "error": "读信超时（下一轮重试）"})

    # ----------------------------------------------------------------- grouping
    @staticmethod
    def _group(targets):
        """把账号按「要打开的收件箱」分组。

        iCloud 别名共用同一个 Gmail 收件箱，组里串行扫、共用一条 IMAP 连接；
        自己的邮箱（Outlook / Gmail）各自一组，彼此并行。列表顺序保持不变，
        所以一轮里的顺序仍是「最久没查的优先」。

        分组键必须指向一条**真的能读它的凭据**。别名行里承载收件箱的是
        client_id（见 store.parse_line），所以按它分组；而 client_id 为空的别名
        行（老格式、手工粘贴缺字段）**不能**被归到一起 —— 那会让其中一条的
        收件箱密码去登另一条，读到的要么是别人的邮箱要么是登录失败。这种行
        各自一组，走自己的凭据、坏了也只坏它自己。
        """
        groups, index = [], {}
        for acct in targets:
            if (acct.get("protocol") or "") == ALIAS_PROTOCOL:
                inbox = (acct.get("client_id") or "").strip().lower()
                # 没有收件箱可复用：当成独立邮箱，别和人拼组。
                key = ("alias", inbox) if inbox else ("box", acct["email"].lower())
            else:
                key = ("box", acct["email"].lower())
            slot = index.get(key)
            if slot is None:
                slot = len(groups)
                index[key] = slot
                groups.append([])
            groups[slot].append(acct)
        return groups

    # ------------------------------------------------------------------ scanning
    def _cred(self, acct):
        return {
            "email": acct["email"], "password": acct.get("password"),
            "client_id": acct.get("client_id"),
            "client_secret": acct.get("client_secret") or "",
            "refresh_token": acct.get("refresh_token") or "",
            "hme_base": self.config.get("hme_base", "") or "",
            "hme_password": self.config.get("hme_password", "") or "",
            "hme_account": acct.get("client_id") or "",
        }

    def _scan_group(self, accts):
        first = accts[0]
        protocol = first.get("protocol") or "graph"
        mail_proxy = self.config.get("mail_proxy", "") or ""
        try:
            mailbox = make_mailbox(self._cred(first), protocol, mail_proxy)
        except Exception as e:
            for acct in accts:
                self._record_error(acct, f"无法建立取件通道：{type(e).__name__}: "
                                         f"{str(e)[:160]}")
            return
        # 同一收件箱的多个别名共用连接：GmailAliasMailbox 每次 messages() 都用
        # self.email 去 SEARCH TO <alias>，所以换个别名接着读就行，不必重登。
        share = protocol == ALIAS_PROTOCOL and len(accts) > 1
        try:
            for acct in accts:
                # 停止请求意味着停：不再开新的收件箱，未读的账号留给下一轮。
                if self._stop.is_set():
                    break
                if share:
                    mailbox.email = acct["email"]
                self._scan_one(acct, mailbox)
        finally:
            try:
                mailbox.close()
            except Exception:
                pass

    def _scan_one(self, acct, mailbox):
        account_id = acct["id"]
        self._reading[account_id] = time.time()
        try:
            self._read_and_judge(acct, mailbox)
        finally:
            self._reading.pop(account_id, None)

    def _read_and_judge(self, acct, mailbox):
        account_id = acct["id"]
        baseline = acct.get("baseline_at")
        if baseline is None:
            # 首次检测：基线 = 回看窗口的起点。活动已经结束，回看窗口内的
            # Oasis 来信必须算数 —— 中签结果很可能早就发出去了，若把基线设成
            # 「现在」，那些信会被当成历史而白白漏掉。
            baseline = (time.time() - self.lookback_days * 86400
                        if self.lookback_days > 0 else 0.0)
        # 粗筛还是全量，取决于这个账号查过几次。
        #
        # 首次检测走全量：那一次要把邮箱里**已经存在**的信看全 —— 活动结束、结果
        # 可能早就发过了，任何粗筛都有漏掉它的风险，而这正是这一版程序最要紧的
        # 一次读。之后的轮次只做服务端粗筛（发件人 openstage 或标题含 Oasis），
        # 因为此后要找的都是「新到的信」，而 Oasis 的信实测来自同一个发件人。
        #
        # 代价与收益都清楚：全量 = 拉 N 封正文本地挑；粗筛 = 服务端只回那几封。
        # 邮箱里塞着几百封无关邮件的账号，差别是几十倍的下载量。若哪天站点换了
        # 发件人，把 mail_filter 关掉即可 —— 那等于每轮都按首次的标准读。
        first_look = not acct.get("check_count")
        only_oasis = (self.mail_filter and not first_look
                      and not self.mail_filter_broken)
        try:
            mails = mailbox.messages(limit=self.per_page,
                                     not_before=baseline or None,
                                     only_oasis=only_oasis)
        except MailSearchUnsupported as e:
            # 粗筛这条路走不通（服务端不认这套搜索语法）。退回全量 —— 把
            # 「筛不出来」当成「读不到」，会让这个账号每轮都被记一次失败，
            # 而它其实完全可以读。
            self._log("warn", f"{acct['email']}：{e} —— 改为全量读取")
            self.mail_filter_broken = True
            try:
                mails = mailbox.messages(limit=self.per_page,
                                         not_before=baseline or None)
            except MailAuthError as e2:
                self._record_error(acct, f"取件被拒：{str(e2)[:200]}")
                return
            except Exception as e2:
                self._record_error(acct, f"{type(e2).__name__}: {str(e2)[:200]}")
                return
        except MailAuthError as e:
            self._record_error(acct, f"取件被拒：{str(e)[:200]}")
            return
        except Exception as e:
            self._record_error(acct, f"{type(e).__name__}: {str(e)[:200]}")
            return
        if first_look and self.mail_filter:
            self._log("debug", f"{acct['email']}：首次检测，取全量 "
                               f"{len(mails)} 封做基准")

        verdict = hitcheck.pick_oasis(mails, baseline_at=baseline)
        latest_at, latest_subject = verdict["latest"] or (None, "")

        source, note = None, ""
        if verdict["success"]:
            source = hitcheck.SOURCE_SUCCESS_MAIL
            note = f"成功邮件：{verdict['success'][1]}"
        elif verdict["first_new"]:
            source = hitcheck.SOURCE_OASIS_MAIL
            note = f"Oasis 来信：{verdict['subject']}"
        elif hitcheck.legacy_site_ok(acct.get("status"), acct.get("error") or ""):
            # merge 点：站点已经确认过的旧记录。运行中兜底再判一次，因为
            # 这个结论只依赖库里已有的字段，不花任何网络代价。
            source = hitcheck.SOURCE_SITE_OK
            note = "站点已确认（无成功邮件）"

        # 本轮被放弃的账号：结论照写，但不推进进度（见 _mark_overdue）。
        overdue = account_id in self._abandoned
        self._abandoned.discard(account_id)
        self.store.record_check(
            account_id, mail_at=latest_at, mail_subject=latest_subject,
            hit_source=source, hit_note=note, baseline_at=baseline or None,
            touch=not overdue)
        if overdue:
            self._log("info", f"{acct['email']}：上一轮放弃等待的那次读信回来了"
                              f"（{hitcheck.SOURCE_LABEL.get(source, '未中签')}），"
                              f"进度仍留给下一轮")
        with self._lock:
            self._checked += 1
            if source:
                self._hits += 1
        time.sleep(PAGE_GAP)
        if source:
            self._log("ok", f"中签 {acct['email']} —— "
                            f"{hitcheck.SOURCE_LABEL.get(source, source)} · {note}")
            self._emit("hit", {"email": acct["email"], "source": source,
                               "note": note, "mail_at": latest_at,
                               "subject": latest_subject,
                               "protocol": acct.get("protocol")})
        else:
            seen = (f"{len(mails)} 封信，Oasis {verdict['n_oasis']} 封"
                    + (f"（其中验证信 {verdict['n_verify']} 封）"
                       if verdict["n_verify"] else ""))
            self._log("info", f"{acct['email']}：未中签（{seen}）")
            self._emit("checked", {"email": acct["email"],
                                   "n_oasis": verdict["n_oasis"],
                                   "n_verify": verdict["n_verify"],
                                   "latest_at": latest_at,
                                   "subject": latest_subject})

    def _record_error(self, acct, message):
        self.store.record_check(acct["id"], error=message)
        with self._lock:
            self._failures += 1
        self._log("warn", f"检测失败 {acct['email']} —— {message}")
        self._emit("check_failed", {"email": acct["email"], "error": message})
