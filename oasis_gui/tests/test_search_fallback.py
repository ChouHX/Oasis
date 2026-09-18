#!/usr/bin/env python3
"""粗筛失败时的降级回归。

粗筛（服务端只回 Oasis 的来信）是一层优化，不能成为正确性的一部分。它有两处
可能被服务端拒绝：IMAP 的前缀 OR、Graph 的 $filter。若拒绝被静默当成「搜索
结果为空」，一个完全可以读的账号会每轮都被判成未中签。

这里用一台「不认 OR 语法」的假 IMAP 服务端，断言三件事：抛出的是
MailSearchUnsupported（而不是空列表）；不加筛选时照常读到信；monitor 收到它
之后退回全量，账号照样被判中签。

    python3 tests/test_search_fallback.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import hitcheck                       # noqa: E402
from core import monitor as monitor_mod         # noqa: E402
from core.mailbox import (ImapMailbox, Mail, MailSearchUnsupported,  # noqa: E402
                          parse_cred)
from core.store import Store                    # noqa: E402

RAW = (b"From: Oasis <oasis@openstageit.com>\r\n"
       b"To: someone@outlook.com\r\n"
       b"Subject: =?UTF-8?Q?Oasis_Live_=E2=80=9927_Registration_Complete?=\r\n"
       b"Date: Wed, 16 Sep 2026 10:00:00 +0000\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n"
       b"\r\n"
       b"You have successfully registered for Oasis Live '27.\r\n")


class FakeIMAP:
    """一台只会说「我不认 OR」的 IMAP 服务端。"""

    def __init__(self, refuse_or=True):
        self.refuse_or = refuse_or
        self.searches = []

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.searches.append(list(criteria))
        if self.refuse_or and "OR" in criteria:
            return "BAD", [b"Error in IMAP command SEARCH: Invalid arguments"]
        return "OK", [b"1"]

    def fetch(self, num, spec):
        return "OK", [(b"1 (RFC822 {%d}" % len(RAW), RAW), b")"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" INBOX']

    def logout(self):
        pass

    def noop(self):
        return "OK", [b""]


CRED = parse_cred("box@outlook.com----pw----cid----refresh-token")

print("== 1. 服务端拒绝 OR 时，IMAP reader 抛的是明确的失败，而不是一个空邮箱 ==")
mb = ImapMailbox(CRED)
fake = FakeIMAP(refuse_or=True)
mb.connect = lambda attempts=2: fake
try:
    mb.messages(limit=5, only_oasis=True)
    raised = False
    detail = "没有抛出"
except MailSearchUnsupported as e:
    raised = True
    detail = str(e)[:70]
except Exception as e:                                  # noqa: BLE001
    raised = False
    detail = f"抛的是 {type(e).__name__}"
print(f"[{'OK ' if raised else 'FAIL'}] only_oasis=True -> MailSearchUnsupported（{detail}）")
ok1 = raised

print("\n== 2. 同一条连接不加筛选照常读到信，标题照样被解码 ==")
mails = mb.messages(limit=5, only_oasis=False)
kinds = [hitcheck.classify(m) for m in mails]
decoded = mails[0].subject if mails else ""
ok2 = (len(mails) == 1 and kinds == ["success"]
       and decoded == "Oasis Live \u201927 Registration Complete")
print(f"[{'OK ' if ok2 else 'FAIL'}] 读到 {len(mails)} 封，判定 {kinds}，"
      f"标题 {decoded!r}")

print("\n== 3. monitor 收到拒绝之后退回全量，账号照样判中签 ==")
DB = os.path.join(tempfile.mkdtemp(prefix="oasis-fallback-"), "probe.db")
store = Store(DB)
store.add_mailboxes(["fallback@outlook.com----pw----cid----rt"], "imap")
# 先让它有 check_count，从而走粗筛那条路（首次是全量，测不出降级）。
store.record_check(store.check_targets()[0]["id"])

LOGS = []


class RefusingReader:
    """读信永远拒绝粗筛，但全量读得到那封成功邮件。"""

    def __init__(self, email):
        self.email = email
        self.asked = []

    def messages(self, limit=12, not_before=None, only_oasis=False):
        self.asked.append(only_oasis)
        if only_oasis:
            raise MailSearchUnsupported("服务端不接受这次 SEARCH（BAD）")
        return [Mail(subject="Oasis Live '27 Registration Complete",
                     sender="Oasis <oasis@openstageit.com>",
                     body="You have successfully registered.", stamp=0.0,
                     folder="inbox")]

    def close(self):
        pass


readers = []
monitor_mod.make_mailbox = lambda cred, protocol="graph", proxy_url="": (
    readers.append(RefusingReader(cred["email"])) or readers[-1])
mon = monitor_mod.HitMonitor(store, lambda lvl, msg: LOGS.append((lvl, msg)),
                             lambda kind, payload=None: None,
                             {"threads": 1, "per_page": 5})
mon.sweep()

row = [a for a in store.accounts() if a["email"] == "fallback@outlook.com"][0]
asked = readers[0].asked if readers else []
ok3 = (row["hit_source"] == hitcheck.SOURCE_SUCCESS_MAIL
       and asked == [True, False])
print(f"[{'OK ' if ok3 else 'FAIL'}] 中签来源 {row['hit_source']!r}，"
      f"读取顺序 {asked}（先粗筛、被拒后全量）")
for lvl, msg in LOGS:
    if lvl == "warn":
        print(f"       日志：[{lvl}] {msg}")

print("\nRESULT:", "PASS" if (ok1 and ok2 and ok3) else "FAIL")
sys.exit(0 if (ok1 and ok2 and ok3) else 1)
