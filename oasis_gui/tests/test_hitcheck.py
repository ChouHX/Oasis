#!/usr/bin/env python3
"""判定链路回归：拿假收件箱跑真实的 monitor，逐条断言结论。

重点在「预约」与「中签」的分界：注册截止（2026-09-17 16:00 BST）之前收到的每一
封信只说明预约成功，之后收到的 Oasis 来信才是结果。离线，不碰网络。

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

CUTOFF = hitcheck.parse_cutoff(hitcheck.DEFAULT_CUTOFF)
assert CUTOFF, "截止时间必须解析得出来"


def before(days=1.0):
    return CUTOFF - days * 86400


def after(days=1.0):
    return CUTOFF + days * 86400


def mail(subject, body, stamp,
         sender="Oasis <oasis@openstageit.com>"):
    return Mail(subject=subject, sender=sender, body=body, stamp=stamp,
                folder="inbox")


# --- 注册期的信：三封都在截止之前 -------------------------------------------
SUCCESS = mail("=?UTF-8?Q?Oasis_Live_=E2=80=9927_Registration_Complete?=",
               "<p>You have successfully registered for Oasis Live '27.</p>",
               before(1))
VERIFY = mail("=?UTF-8?Q?Verify_Your_Email?=",
              '<a href="https://oasis.hq.fan/registration?token=abc.def-123">'
              "click the button</a>", before(1))
OLD_VERIFY = mail("Verify Your Email",
                  "https://oasis.hq.fan/registration?token=old.1", before(120))
FOREIGN = mail("Your LA28 draw result", "You were not selected.", before(0.5),
               sender="LA28 <no-reply@la28.org>")
# 一封「结果信」造型的信，但时间戳在截止之前 —— 它仍然不算中签。
EARLY_RESULT = mail("Oasis Live '27 — your ballot result",
                    "<p>Details about your entry are below.</p>", before(0.5))
# 真正的结果信：截止之后到达。
RESULT = mail("Oasis Live '27 — your ballot result",
              "<p>You have been successful in the Oasis Live '27 ballot.</p>",
              after(1))

store = Store(DB)
lines = [
    "a-registered@outlook.com----p----cid----rt",     # 只有注册成功邮件
    "b-verify@outlook.com----p----cid----rt",         # 只有验证信
    "c-old@outlook.com----p----cid----rt",            # 只有很久以前的验证信
    "d-foreign@outlook.com----p----cid----rt",        # 只有别家邮件
    "e-nomail@outlook.com----p----cid----rt",         # confirm OK 无邮件
    "f-page@outlook.com----p----cid----rt",           # 页面确认
    "g-broken@outlook.com----p----cid----rt",         # 读信失败
    "h-result@outlook.com----p----cid----rt",         # 截止后的结果信
    "i-early@outlook.com----p----cid----rt",          # 截止前的「结果信」
]
added, dup = store.add_mailboxes(lines, "graph", opted_in=True)
assert added == 9, (added, dup)

by_email = {r["email"]: r for r in store.accounts()}
store._write(lambda c: c.execute(
    "UPDATE accounts SET status='failed', error=? WHERE id=?",
    ("confirm answered OK but the success mail never arrived within 180s",
     by_email["e-nomail@outlook.com"]["id"])))
store._write(lambda c: c.execute(
    "UPDATE accounts SET status='submitted', error='页面确认注册成功' WHERE id=?",
    (by_email["f-page@outlook.com"]["id"],)))

INBOX = {
    "a-registered@outlook.com": [SUCCESS],
    "b-verify@outlook.com": [VERIFY],
    "c-old@outlook.com": [OLD_VERIFY],
    "d-foreign@outlook.com": [FOREIGN],
    "e-nomail@outlook.com": [],
    "f-page@outlook.com": [],
    "g-broken@outlook.com": [],
    "h-result@outlook.com": [RESULT],
    "i-early@outlook.com": [EARLY_RESULT],
}
built = []


class FakeMailbox:
    """Reader stand-in：按当前 self.email 现查现取（别名共享连接时靠换 email 复用）。

    `not_before` 照服务端 SINCE 的语义过滤 —— 真实 reader 会把它下推给服务端，
    所以这里也必须这么做，否则测不出「截止前的信根本不会被拉回来」。
    """

    def __init__(self, email):
        self.email = email
        self.filters = []
        self.calls = 0

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
        pass


def fake_make_mailbox(cred, protocol="graph", proxy_url=""):
    mb = FakeMailbox(cred["email"])
    built.append(mb)
    return mb


monitor_mod.make_mailbox = fake_make_mailbox
LOGS = []
mon = monitor_mod.HitMonitor(store, lambda lvl, msg: LOGS.append((lvl, msg)),
                             lambda kind, payload=None: None,
                             {"threads": 2, "per_page": 20})
mon.cutoff = CUTOFF
mon.sweep()

results = {r["email"]: r for r in store.accounts()}
checks = [
    ("h-result@outlook.com", hitcheck.SOURCE_OASIS_MAIL, "截止后收到结果信"),
    ("i-early@outlook.com", None, "截止前收到的「结果信」不算"),
    ("a-registered@outlook.com", None, "Registration Complete 只是预约成功"),
    ("b-verify@outlook.com", None, "验证信不算"),
    ("c-old@outlook.com", None, "很久以前的验证信不算"),
    ("d-foreign@outlook.com", None, "别家邮件不算"),
    ("e-nomail@outlook.com", None, "confirm OK 无邮件只是预约成功"),
    ("f-page@outlook.com", None, "页面确认只是预约成功"),
]
ok = True
for email, want, why in checks:
    got = results[email].get("hit_source")
    good = got == want
    ok = ok and good
    print(f"[{'OK ' if good else 'FAIL'}] {email:24} want={str(want):14} "
          f"got={str(got):14} ({why})")

broken = results["g-broken@outlook.com"]
err_ok = bool(broken["check_error"]) and not broken["hit_at"]
print(f"[{'OK ' if err_ok else 'FAIL'}] 读信失败只记错误、不判中签："
      f"{broken['check_error']}")
ok = ok and err_ok

# 「预约过」的推断：三类旧痕迹都要落成 opted_in，且都不是中签
legacy = store.mark_unmarked(on=False)          # 先把导入时的标记撤掉
after_off = {r["email"]: bool(r.get("opted_in")) for r in store.accounts()}
store.migrate_legacy()
inferred = {r["email"]: bool(r.get("opted_in")) for r in store.accounts()}
want_inferred = {"e-nomail@outlook.com": True, "f-page@outlook.com": True}
inf_ok = all(inferred[e] == v for e, v in want_inferred.items())
print(f"[{'OK ' if inf_ok else 'FAIL'}] 旧痕迹推断为已预约"
      f"（confirm OK → {inferred['e-nomail@outlook.com']}，页面确认 → "
      f"{inferred['f-page@outlook.com']}）")
ok = ok and inf_ok
hits_now = store.stats()["hits"]
hit_ok = hits_now == 1          # 只有那封截止后的结果信
print(f"[{'OK ' if hit_ok else 'FAIL'}] 全库中签数 {hits_now}（应只有结果信那一封）")
ok = ok and hit_ok

# 别名分组：同一收件箱合一，缺 client_id 的各自成组
mixed = monitor_mod.HitMonitor._group([
    {"id": 1, "email": "a@icloud.com", "protocol": "alias-imap",
     "client_id": "owner@gmail.com"},
    {"id": 2, "email": "b@icloud.com", "protocol": "alias-imap",
     "client_id": "OWNER@gmail.com"},
    {"id": 3, "email": "c@icloud.com", "protocol": "alias-imap",
     "client_id": None},
    {"id": 4, "email": "d@icloud.com", "protocol": "alias-imap",
     "client_id": ""},
])
sizes = [len(g) for g in mixed]
group_ok = sizes == [2, 1, 1]
print(f"[{'OK ' if group_ok else 'FAIL'}] 别名分组：同一收件箱合一，缺 client_id 的"
      f"各自成组（{sizes}）")
ok = ok and group_ok

# 幂等：再扫一轮，结论不变
before_hits = {r["email"]: r.get("hit_source") for r in store.accounts()}
mon2 = monitor_mod.HitMonitor(store, lambda lvl, msg: None,
                              lambda kind, payload=None: None, {})
mon2.cutoff = CUTOFF
mon2.sweep()
again = {r["email"]: r.get("hit_source") for r in store.accounts()}
stable = all(again[e] == before_hits[e] for e in before_hits)
print(f"[{'OK ' if stable else 'FAIL'}] 第二轮不改变已有结论")
ok = ok and stable

# 粗筛下推：首次读一个账号要全量，之后才让服务端只回 Oasis 的信
probe = [m.filters for m in built if m.email == "d-foreign@outlook.com"]
filter_ok = len(probe) >= 2 and probe[0] == [False] and probe[1] == [True]
print(f"[{'OK ' if filter_ok else 'FAIL'}] 首次全量、其后服务端粗筛（{probe[:2]}）")
ok = ok and filter_ok

stats = store.stats()
print(f"\nstats: {stats}")
print(f"hits: {[(h['email'], h['hit_source']) for h in store.hits()]}")
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
