#!/usr/bin/env python3
"""上一版记录的翻译回归。

上一版把「预约成功」写成了中签，这一条测那个纠正：旧的 registered / submitted /
`confirm answered OK but the success mail never arrived` / 有预约行的记录，现在都
只翻译成「**已预约**」（纳入检测范围），而 `hit_at` 被清空 —— 它们一张票都没拿到。

同时测幂等：第二次打开库不再重复清理。

    python3 tests/test_merge.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import hitcheck                       # noqa: E402
from core.store import Store                    # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="oasis-legacy-"), "probe.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass

LEGACY = "confirm answered OK but the success mail never arrived within 180s"

# 一个「上一版迁移过之后」的库形状：有 hit_at（而且里面躺着误判），但没有
# registered_at、没有 opted_in、也没有 meta 表 —— 所以纠正那一步必须跑。
c = sqlite3.connect(DB)
c.executescript("""
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
    password TEXT, client_id TEXT, refresh_token TEXT,
    first_name TEXT, last_name TEXT, phone TEXT, date_of_birth TEXT,
    location_json TEXT, protocol TEXT NOT NULL DEFAULT 'graph',
    status TEXT NOT NULL DEFAULT 'pending', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    hit_at REAL, hit_source TEXT, hit_note TEXT,
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
    ("mailed@outlook.com", "registered", None, False),        # 成功邮件到过
    ("paged@outlook.com", "submitted", "页面确认注册成功", False),
    ("nomail@outlook.com", "failed", LEGACY, False),
    ("regged@outlook.com", "failed", "the form rejected this", True),
    ("never1@outlook.com", "pending", None, False),           # 从没跑过
    ("never2@outlook.com", "failed", "proxy died", False),    # 跑过但没提交
]
for email, status, error, has_reg in rows:
    c.execute("INSERT INTO accounts (email,protocol,status,error,created_at,"
              "updated_at) VALUES (?,'graph',?,?,?,?)", (email, status, error, now, now))
    if has_reg:
        aid = c.execute("SELECT id FROM accounts WHERE email=?", (email,)).fetchone()[0]
        c.execute("INSERT INTO registrations (account_id,artist_id,status,"
                  "created_at) VALUES (?,'artist','submitted',?)", (aid, now))
# 还有一条「上一版误判的中签」：hit_at 已经被写成非空。它记录的其实是预约时间。
c.execute("INSERT INTO accounts (email,protocol,status,created_at,updated_at,"
          "hit_at,hit_source,hit_note) VALUES ('badhit@outlook.com','graph',"
          "'registered',?,?,?,'success-mail','旧记录：成功邮件已到')",
          (now, now, time.time() - 3 * 86400))
c.commit()
c.close()

store = Store(DB)
rows_by_email = {r["email"]: r for r in store.accounts()}

checks = [
    ("mailed@outlook.com", True, "成功邮件到过（status=registered）"),
    ("paged@outlook.com", True, "页面确认（status=submitted）"),
    ("nomail@outlook.com", True, "confirm OK 无邮件那条错误"),
    ("regged@outlook.com", True, "有 registrations 行"),
    ("badhit@outlook.com", True, "上一版标过中签的（它其实只预约成功）"),
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

# 关键：误判的中签必须被撤销，而它的时间要落到 registered_at
bad = rows_by_email["badhit@outlook.com"]
cleared = bad.get("hit_at") is None and bad.get("hit_source") is None
kept_time = bool(bad.get("registered_at"))
print(f"[{'OK ' if cleared else 'FAIL'}] 误判的中签已撤销（hit_at="
      f"{bad.get('hit_at')}）")
ok = ok and cleared
print(f"[{'OK ' if kept_time else 'FAIL'}] 那个时间搬到了 registered_at="
      f"{bad.get('registered_at')}")
ok = ok and kept_time

# 全库不该有任何中签 —— 预约成功不是中签
hits = store.stats()["hits"]
none_hit = hits == 0
print(f"[{'OK ' if none_hit else 'FAIL'}] 全库中签数 {hits}（预约成功不算中签）")
ok = ok and none_hit

# 检测范围只看已预约
targets = {t["email"] for t in store.check_targets(skip_hits=False)}
want_scope = {"mailed@outlook.com", "paged@outlook.com", "nomail@outlook.com",
              "regged@outlook.com", "badhit@outlook.com"}
scope_ok = targets == want_scope
print(f"[{'OK ' if scope_ok else 'FAIL'}] 检测范围 = 已预约的那几个"
      f"（{len(targets)} 个）")
ok = ok and scope_ok

# 幂等：再开一次，结论不变，也不重复清理
again_rows = {r["email"]: r for r in Store(DB).accounts()}
same = all(bool(again_rows[e].get("opted_in")) == bool(rows_by_email[e].get("opted_in"))
           for e in rows_by_email)
print(f"[{'OK ' if same else 'FAIL'}] 重复打开不会改动已有结论")
ok = ok and same

# 截止时间本身要解析得出来，且是官方那个时刻（16:00 BST == 15:00 UTC）
cut = hitcheck.parse_cutoff(hitcheck.DEFAULT_CUTOFF)
want_cut = hitcheck.parse_cutoff("2026-09-17T15:00:00Z")
cut_ok = cut == want_cut
print(f"[{'OK ' if cut_ok else 'FAIL'}] 默认截止 = 2026-09-17 15:00 UTC"
      f"（{hitcheck.format_cutoff(cut)} 本地）")
ok = ok and cut_ok

print("\nRESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
