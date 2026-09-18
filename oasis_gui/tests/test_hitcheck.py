#!/usr/bin/env python3
"""判定链路回归：拿假收件箱跑真实的 monitor，逐条断言结论。

离线，不碰网络，也不需要凭据。改判定规则后先跑它。

    python3 tests/test_hitcheck.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import hitcheck                     # noqa: E402
from core import monitor as monitor_mod       # noqa: E402
from core.mailbox import Mail                 # noqa: E402
from core.store import Store                  # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="oasis-hit-"), "probe.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass

NOW = time.time()


def mail(subject, body, sender="Oasis <oasis_at_openstageit_com_9f2@icloud.com>",
         age_days=0.0):
    return Mail(subject=subject, sender=sender, body=body,
                stamp=NOW - age_days * 86400, folder="inbox")


SUCCESS = mail("=?UTF-8?Q?Oasis_Live_=E2=80=9927_Registration_Complete?=",
               "<p>You have successfully registered for Oasis Live '27.</p>")
VERIFY = mail("=?UTF-8?Q?Verify_Your_Email?=",
              '<a href="https://oasis.hq.fan/registration?token=abc.def-123">'
              "click the button</a>")
OLD_VERIFY = mail("Verify Your Email",
                  'https://oasis.hq.fan/registration?token=old.1', age_days=120)
FOREIGN = mail("Your LA28 draw result", "You were not selected.",
               sender="LA28 <no-reply@la28.org>")
RESULT = mail("Oasis Live '27 — your ballot result",
              "<p>Thanks for taking part in the Oasis Live '27 ballot. "
              "Details about your entry are below.</p>")


class FakeMailbox:
    """Reader stand-in: hands back whatever the case says is in the inbox.

    按 self.email 现查现取，而不是构造时固定 —— 真实的 GmailAliasMailbox 就是
    每次用 self.email 去 SEARCH TO <alias>，共享连接的别名靠换 email 复用。
    """

    def __init__(self, email, mails, boom=False):
        self.email = email
        self._boom = boom
        self.closed = False
        self.calls = 0
        # 每次读都记下「有没有要求服务端只回 Oasis 的信」，用来断言粗筛的下推。
        self.filters = []

    def messages(self, limit=12, not_before=None, only_oasis=False):
        self.calls += 1
        self.filters.append(only_oasis)
        if self.email == "g-broken@outlook.com":
            raise OSError("imap connect refused")
        out = list(INBOX.get(self.email, []))
        if not_before:
            out = [m for m in out if (m.stamp or 0) >= not_before]
        out.sort(key=lambda m: m.stamp or 0, reverse=True)
        return out[:limit]

    def close(self):
        self.closed = True


store = Store(DB)
lines = [
    "a-success@outlook.com----p----cid----rt",          # A 成功邮件
    "b-oasis@outlook.com----p----cid----rt",            # B Oasis 新来信
    "c-old@outlook.com----p----cid----rt",              # C 只有基线前的旧信
    "d-foreign@outlook.com----p----cid----rt",          # D 只有别家邮件
    "g-broken@outlook.com----p----cid----rt",           # G 读信失败
    "h-result@outlook.com----p----cid----rt",           # H 结果信（无成功标记）
]
added, dup = store.add_mailboxes(lines, "graph")
assert added == 6, (added, dup)

# E: 那条被旧程序写成失败的记录 —— 用户点名要 merge 的一类
store.add_mailboxes(["e-nomail@outlook.com----p----cid----rt"], "graph")
e_id = [r for r in store.accounts() if r["email"] == "e-nomail@outlook.com"][0]["id"]
store._write(lambda c: c.execute(
    "UPDATE accounts SET status='failed', error=? WHERE id=?",
    ("confirm answered OK but the success mail never arrived within 180s",
     e_id)))

# F: 浏览器模式留下的「页面确认」记录
store.add_mailboxes(["f-page@outlook.com----p----cid----rt"], "graph")
f_id = [r for r in store.accounts() if r["email"] == "f-page@outlook.com"][0]["id"]
store._write(lambda c: c.execute(
    "UPDATE accounts SET status='submitted', error='页面确认注册成功' WHERE id=?",
    (f_id,)))

# 同收件箱的三条 iCloud 别名：必须共用一个 reader（分组复用）
alias_lines = [f"alias{i}@icloud.com----owner@gmail.com----app-pass----gmail-imap"
               for i in range(3)]
store.add_mailboxes(alias_lines, "auto")
first_alias = [r for r in store.accounts() if r["email"] == "alias0@icloud.com"][0]
store._write(lambda c: c.execute(
    "UPDATE accounts SET baseline_at=? WHERE email LIKE '%@icloud.com'",
    (0.0,)))

INBOX = {
    "a-success@outlook.com": [SUCCESS],
    "b-oasis@outlook.com": [VERIFY],
    "c-old@outlook.com": [OLD_VERIFY],
    "d-foreign@outlook.com": [FOREIGN],
    "g-broken@outlook.com": [],
    "e-nomail@outlook.com": [],
    "f-page@outlook.com": [],
    "alias0@icloud.com": [SUCCESS],
    "alias1@icloud.com": [FOREIGN],
    "alias2@icloud.com": [VERIFY],
    "h-result@outlook.com": [RESULT],
}
built = []


def fake_make_mailbox(cred, protocol="graph", proxy_url=""):
    mb = FakeMailbox(cred["email"], INBOX.get(cred["email"], []),
                     boom=cred["email"] == "g-broken@outlook.com")
    built.append(mb)
    return mb


monitor_mod.make_mailbox = fake_make_mailbox
logs = []
mon = monitor_mod.HitMonitor(store, lambda lvl, msg: logs.append((lvl, msg)),
                             lambda kind, payload=None: None, {})
mon.sweep()

results = {r["email"]: r for r in store.accounts()}
checks = [
    ("a-success@outlook.com", hitcheck.SOURCE_SUCCESS_MAIL, "成功邮件"),
    ("b-oasis@outlook.com", None, "注册期验证信不算中签"),
    ("c-old@outlook.com", None, "基线前的旧信不算"),
    ("d-foreign@outlook.com", None, "别家邮件不算"),
    ("e-nomail@outlook.com", hitcheck.SOURCE_SITE_OK, "confirm OK 无邮件也判成功"),
    ("f-page@outlook.com", hitcheck.SOURCE_SITE_OK, "页面确认也判成功"),
    ("alias0@icloud.com", hitcheck.SOURCE_SUCCESS_MAIL, "别名成功邮件"),
    ("alias2@icloud.com", None, "别名验证信不算中签"),
    ("h-result@outlook.com", hitcheck.SOURCE_OASIS_MAIL, "无成功标记的结果信仍算中签"),
]
ok = True
for email, want, why in checks:
    got = results[email].get("hit_source")
    mark = "OK " if got == want else "FAIL"
    if got != want:
        ok = False
    print(f"[{mark}] {email:26} want={str(want):14} got={str(got):14} ({why})")

broken = results["g-broken@outlook.com"]
print(f"[{'OK ' if broken['check_error'] and not broken['hit_at'] else 'FAIL'}] "
      f"读信失败只记错误、不判中签：{broken['check_error']}")
if not (broken["check_error"] and not broken["hit_at"]):
    ok = False

# 分组复用：3 条别名共用一个收件箱 -> 只该建 1 个 reader
alias_readers = [m for m in built if m.email in
                 ("alias0@icloud.com", "alias1@icloud.com", "alias2@icloud.com")]
print(f"[{'OK ' if len(alias_readers) == 1 else 'FAIL'}] "
      f"同收件箱别名复用连接：建了 {len(alias_readers)} 个 reader（应为 1）")
if len(alias_readers) != 1:
    ok = False

# 粗筛下推：首次读一个账号要全量（保证不漏掉已经发过的结果信），
# 之后才让服务端只回 Oasis 的信。
probe_email = "d-foreign@outlook.com"
reads = [m.filters for m in built if m.email == probe_email]
first_pass = reads[0] if reads else []

# 幂等：再扫一轮，中签结论不变、不会被抹掉
mon2 = monitor_mod.HitMonitor(store, lambda lvl, msg: None,
                              lambda kind, payload=None: None, {})
mon2.sweep()
reads = [m.filters for m in built if m.email == probe_email]
later = reads[1] if len(reads) > 1 else []
filter_ok = first_pass == [False] and later == [True]
print(f"[{'OK ' if filter_ok else 'FAIL'}] 首次全量、其后服务端粗筛"
      f"（首轮 {first_pass}，次轮 {later}）")
if not filter_ok:
    ok = False

again = {r["email"]: r.get("hit_source") for r in store.accounts()}
stable = all(again[e] == w for e, w, _ in checks)
print(f"[{'OK ' if stable else 'FAIL'}] 第二轮不改变已有结论")
if not stable:
    ok = False

stats = store.stats()
print(f"\nstats: {stats}")
print(f"hits (ordered): {[(h['email'], h['hit_source']) for h in store.hits()]}")
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
