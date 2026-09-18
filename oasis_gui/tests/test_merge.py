#!/usr/bin/env python3
"""merge 回归：那条「confirm 回 OK 但成功邮件没到」的记录必须进中签名单。

自己造一个旧版形状的库（没有中签列），走真实的 Store() 打开流程与迁移，再断言
名单。离线。

    python3 tests/test_merge.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import hitcheck                        # noqa: E402
from core.monitor import HitMonitor              # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="oasis-merge-"), "probe.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass

LEGACY_ERROR = ("confirm answered OK but the success mail never arrived "
                "within 180s")

# 直接建一个旧版形状的库：没有中签列，全靠迁移补齐。
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
    # 1) confirm 回 OK、成功邮件没到 -> 旧程序记 failed
    ("a-nomail@outlook.com", "failed", LEGACY_ERROR),
    # 2) 页面确认（浏览器模式，从不发成功邮件）
    ("b-page@outlook.com", "submitted", "页面确认注册成功（浏览器模式不发成功邮件）"),
    # 3) 成功邮件到过
    ("c-mail@outlook.com", "registered", None),
    # 4) 真正失败：站点拒绝，跟成功邮件没关系，不该被并进来
    ("d-rejected@outlook.com", "failed", "the form rejected this address"),
    # 5) 还没跑过的账号
    ("e-pending@outlook.com", "pending", None),
]
for email, status, error in rows:
    c.execute("INSERT INTO accounts (email,protocol,status,error,created_at,"
              "updated_at) VALUES (?,'graph',?,?,?,?)", (email, status, error, now, now))
# 6) 同一条错误只落在 registrations.response 上
c.execute("INSERT INTO accounts (email,protocol,status,created_at,updated_at) "
          "VALUES ('f-resp@outlook.com','graph','failed',?,?)", (now, now))
aid = c.execute("SELECT id FROM accounts WHERE email='f-resp@outlook.com'").fetchone()[0]
c.execute("INSERT INTO registrations (account_id,artist_id,response,status,"
          "created_at) VALUES (?,'artist',?,'submitted',?)",
          (aid, LEGACY_ERROR, now))
c.commit()
c.close()

from core.store import Store                     # noqa: E402
store = Store(DB)                                # 打开即迁移
hits = {h["email"]: h["hit_source"] for h in store.hits()}
print("迁移后名单:", hits)

checks = [
    ("a-nomail@outlook.com", hitcheck.SOURCE_SITE_OK, "confirm OK 无邮件"),
    ("b-page@outlook.com", hitcheck.SOURCE_SITE_OK, "页面确认"),
    ("c-mail@outlook.com", hitcheck.SOURCE_SUCCESS_MAIL, "成功邮件已到"),
    ("f-resp@outlook.com", hitcheck.SOURCE_SITE_OK, "错误只落在 registrations.response"),
    ("d-rejected@outlook.com", None, "真正的失败不并入"),
    ("e-pending@outlook.com", None, "没跑过的不并入"),
]
ok = True
for email, want, why in checks:
    got = hits.get(email)
    good = got == want
    ok = ok and good
    print(f"[{'OK ' if good else 'FAIL'}] {email:26} want={str(want):14} "
          f"got={str(got):14} ({why})")

# 幂等：再开一次库，结论不变
again = {h["email"]: h["hit_source"] for h in Store(DB).hits()}
same = again == hits
print(f"[{'OK ' if same else 'FAIL'}] 重复打开不会改动已有结论")
ok = ok and same

# 中签是单向的：一轮读信失败不能把它抹掉
HitMonitor(store, lambda *a: None, lambda *a: None,
           {"per_page": 5}).sweep()
after = {h["email"]: h["hit_source"] for h in store.hits()}
kept = after == hits
print(f"[{'OK ' if kept else 'FAIL'}] 读信失败不抹掉已成立的中签（{len(after)} 条仍在）")
ok = ok and kept
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
