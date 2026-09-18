#!/usr/bin/env python3
"""检测范围（已预约）回归。

池子里绝大多数地址没预约过，而没预约过的邮箱不可能收到中签信 —— 检测范围因此
默认只含 opted_in=1 的账号。这一条测四件事：旧库打开时能不能把「预约过」的证据
全翻出来、纯粹的待办/失败账号不会被误标、手工标记能不能进出自如、以及统计口径
（进度类的数只在已预约里算）。

    python3 tests/test_opted.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import Store                    # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="oasis-opted-"), "probe.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass

LEGACY = "confirm answered OK but the success mail never arrived within 180s"

# 一个上一版形状的库：没有中签列，也没有 opted_in。
c = sqlite3.connect(DB)
c.executescript("""
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
    password TEXT, client_id TEXT, refresh_token TEXT,
    first_name TEXT, last_name TEXT, phone TEXT, date_of_birth TEXT,
    location_json TEXT, protocol TEXT NOT NULL DEFAULT 'graph',
    status TEXT NOT NULL DEFAULT 'pending', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (first_name, last_name, phone));
CREATE TABLE registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL
      REFERENCES accounts(id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL, page_id TEXT, session_id TEXT, poll_id TEXT,
    poll_answer_ids TEXT, journey_json TEXT, token TEXT, response TEXT,
    status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL,
    UNIQUE (account_id, artist_id));
""")
now = time.strftime("%Y-%m-%d %H:%M:%S")
rows = [
    ("mailed@outlook.com", "registered", None, False),      # 成功邮件到过
    ("paged@outlook.com", "submitted", "页面确认注册成功", False),   # 页面确认
    ("nomail@outlook.com", "failed", LEGACY, False),        # confirm OK 无邮件
    ("regged@outlook.com", "failed", "the form rejected this", True),  # 有预约行
    ("never1@outlook.com", "pending", None, False),         # 从没跑过
    ("never2@outlook.com", "failed", "proxy died", False),  # 跑过但没提交
]
for email, status, error, has_reg in rows:
    c.execute("INSERT INTO accounts (email,protocol,status,error,created_at,"
              "updated_at) VALUES (?,'graph',?,?,?,?)", (email, status, error, now, now))
    if has_reg:
        aid = c.execute("SELECT id FROM accounts WHERE email=?", (email,)).fetchone()[0]
        c.execute("INSERT INTO registrations (account_id,artist_id,status,"
                  "created_at) VALUES (?,'artist','submitted',?)", (aid, now))
c.commit()
c.close()

store = Store(DB)
rows_by_email = {r["email"]: r for r in store.accounts()}
checks = [
    ("mailed@outlook.com", True, "成功邮件到过（status=registered）"),
    ("paged@outlook.com", True, "页面确认（status=submitted）"),
    ("nomail@outlook.com", True, "confirm OK 无邮件那条错误"),
    ("regged@outlook.com", True, "有 registrations 行"),
    ("never1@outlook.com", False, "从没跑过的"),
    ("never2@outlook.com", False, "跑过但没提交的"),
]
ok = True
for email, want, why in checks:
    got = bool(rows_by_email[email].get("opted_in"))
    good = got == want
    ok = ok and good
    print(f"[{'OK ' if good else 'FAIL'}] 推断 {email:22} opted_in={int(got)} "
          f"（{why}）")

# 检测范围只看已预约
targets = store.check_targets(skip_hits=False)
emails = {t["email"] for t in targets}
want_scope = {"mailed@outlook.com", "paged@outlook.com", "nomail@outlook.com",
              "regged@outlook.com"}
scope_ok = emails == want_scope
print(f"[{'OK ' if scope_ok else 'FAIL'}] check_targets 只含已预约 "
      f"（{len(emails)} 个，期望 {len(want_scope)}）")
ok = ok and scope_ok

everything = {t["email"] for t in store.check_targets(skip_hits=False,
                                                      only_opted=False)}
all_ok = len(everything) == len(rows)
print(f"[{'OK ' if all_ok else 'FAIL'}] only_opted=False 时整池都在 "
      f"（{len(everything)}）")
ok = ok and all_ok

# 手工标记：整份名单一次标完
n = store.mark_unmarked(on=True, source="manual")
after = {r["email"]: bool(r.get("opted_in")) for r in store.accounts()}
mark_all_ok = n == 2 and all(after.values())
print(f"[{'OK ' if mark_all_ok else 'FAIL'}] 未标记的一键标进来（改了 {n} 个，"
      f"现在全池 {sum(after.values())}/{len(after)}）")
ok = ok and mark_all_ok

# 单条取消再标回
nevers = [r["id"] for r in store.accounts()
          if r["email"] in ("never1@outlook.com", "never2@outlook.com")]
off = store.mark_opted(nevers, on=False)
back = {r["email"]: bool(r.get("opted_in")) for r in store.accounts()}
toggle_ok = (off == 2 and not back["never1@outlook.com"]
             and store.mark_opted(nevers, on=True) == 2)
print(f"[{'OK ' if toggle_ok else 'FAIL'}] 单条取消 / 重新标记（{off} 条取消）")
ok = ok and toggle_ok

# 导入即标记
store.add_mailboxes(["fresh@outlook.com----pw----cid----rt"], "graph",
                    opted_in=True)
fresh = [r for r in store.accounts() if r["email"] == "fresh@outlook.com"][0]
imp_ok = bool(fresh.get("opted_in")) and fresh.get("opted_in_source") == "import"
print(f"[{'OK ' if imp_ok else 'FAIL'}] 导入即标记 "
      f"（source={fresh.get('opted_in_source')}）")
ok = ok and imp_ok

# 统计口径
store.record_check(rows_by_email["mailed@outlook.com"]["id"])
s = store.stats()
stats_ok = (s["total"] == 7 and s["opted"] == 7 and s["unmarked"] == 0
            and s["checked"] + s["unchecked"] == s["opted"] - s["hits"])
print(f"[{'OK ' if stats_ok else 'FAIL'}] 口径：total={s['total']} opted={s['opted']} "
      f"unmarked={s['unmarked']} hits={s['hits']} checked+unchecked="
      f"{s['checked'] + s['unchecked']}（应 = opted - hits）")
ok = ok and stats_ok

# 导入接口的行解析：数组与文本都要给出干净的邮箱。
from core.webui import split_lines            # noqa: E402

parsed = {
    "数组": split_lines(["a@x.com", "b@x.com"]),
    "文本": split_lines("a@x.com\nb@x.com"),
    "逗号": split_lines("a@x.com,b@x.com"),
    "带注释": split_lines(["# 注释", "a@x.com"]),
}
dirty = [line for lines in parsed.values() for line in lines
         if "'" in line or "[" in line or "]" in line]
good = (parsed["数组"] == ["a@x.com", "b@x.com"] and parsed["文本"] == parsed["数组"]
        and parsed["逗号"] == parsed["数组"] and parsed["带注释"] == ["a@x.com"]
        and not dirty)
print(f"[{'OK ' if good else 'FAIL'}] 导入行解析：数组/文本/逗号/注释都给干净邮箱"
      f"（脏值 {dirty or '无'}）")
ok = ok and good

print("\nRESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
