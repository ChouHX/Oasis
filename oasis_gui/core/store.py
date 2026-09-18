#!/usr/bin/env python3
"""SQLite persistence with the de-duplication rules enforced by the schema.

Guarantees, enforced in SQL rather than by hopeful checks in Python:

  * one row per mailbox          -> accounts.email UNIQUE
  * one row per real person      -> accounts(first_name,last_name,phone) UNIQUE
  * one registration per account -> registrations(account_id,artist_id) UNIQUE

Two details matter for correctness under concurrency:

  * Identity columns start as NULL, not ''. SQLite treats NULLs as distinct in a
    UNIQUE index, so unclaimed mailboxes never collide with each other; only
    real identities do.
  * Every write goes through _write(), which rolls back on failure. A caught
    IntegrityError that is never rolled back leaves the write transaction open
    and locks the database for every other thread.

Since the program became a hit monitor, this file also owns the one piece of
history that cannot be re-derived: `migrate_legacy_hits()` folds the previous
build's outcome columns into the hit columns, so the accounts it already got
through - including the ones whose success mail never arrived, which the old
build recorded as failures - are on the hit list from the first sweep.
"""
import json
import os
import sqlite3
import threading
import time

try:
    from . import hitcheck
except ImportError:                      # 直接跑这个文件时没有包上下文
    import hitcheck

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    password       TEXT,
    client_id      TEXT,
    client_secret  TEXT,
    refresh_token  TEXT,
    first_name     TEXT,
    last_name      TEXT,
    phone          TEXT,
    date_of_birth  TEXT,
    location_json  TEXT,
    protocol       TEXT    NOT NULL DEFAULT 'graph',
    status         TEXT    NOT NULL DEFAULT 'pending',
    error          TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    -- 中签检测（见 core/hitcheck.py）。hit_at 是判定成立的时刻，hit_source 说
    -- 清是哪一路证据（success-mail / oasis-mail / site-ok），便于事后复核。
    hit_at          REAL,
    hit_source      TEXT,
    hit_note        TEXT,
    checked_at      REAL,
    check_count     INTEGER NOT NULL DEFAULT 0,
    check_error     TEXT,
    last_mail_at    REAL,
    last_mail_subject TEXT,
    baseline_at     REAL,
    UNIQUE (first_name, last_name, phone)
);

CREATE TABLE IF NOT EXISTS registrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    artist_id       TEXT    NOT NULL,
    page_id         TEXT,
    session_id      TEXT,
    poll_id         TEXT,
    poll_answer_ids TEXT,
    journey_json    TEXT,
    token           TEXT,
    response        TEXT,
    mode            TEXT    NOT NULL DEFAULT 'http',
    status          TEXT    NOT NULL DEFAULT 'submitted',
    created_at      TEXT    NOT NULL,
    UNIQUE (account_id, artist_id)
);

CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_reg_account     ON registrations(account_id);
"""


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """Thread-safe store: one connection per thread, WAL for concurrent access."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        c = self.conn()
        c.executescript(SCHEMA)
        self._migrate(c)
        c.commit()
        n = self.migrate_legacy_hits()
        if n:
            print(f"store: 旧记录并入中签名单 {n} 条", flush=True)

    def _migrate(self, c):
        """Additive migrations for databases created by an earlier build."""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
        if "protocol" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN protocol TEXT "
                      "NOT NULL DEFAULT 'graph'")
        if "client_secret" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN client_secret TEXT")
        if "mail_requested_at" not in cols:
            # When the site was last asked for a verification mail for this
            # address. Kept although nothing writes it any more: the rows that
            # carry it are the record of what the site was already asked.
            c.execute("ALTER TABLE accounts ADD COLUMN mail_requested_at REAL")
        # 中签检测列。检测程序把它们当作唯一的状态来源，所以旧库必须先补齐。
        for name, decl in (
                ("hit_at", "REAL"), ("hit_source", "TEXT"), ("hit_note", "TEXT"),
                ("checked_at", "REAL"),
                ("check_count", "INTEGER NOT NULL DEFAULT 0"),
                ("check_error", "TEXT"), ("last_mail_at", "REAL"),
                ("last_mail_subject", "TEXT"), ("baseline_at", "REAL")):
            if name not in cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")
        rcols = {r["name"] for r in c.execute("PRAGMA table_info(registrations)")}
        if "mode" not in rcols:
            c.execute("ALTER TABLE registrations ADD COLUMN mode TEXT "
                      "NOT NULL DEFAULT 'http'")

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def _write(self, fn):
        """Run fn(conn) inside the process-wide write lock, committing or
        rolling back. Never leaves a transaction dangling."""
        with self._write_lock:
            c = self.conn()
            try:
                out = fn(c)
                c.commit()
                return out
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------------ mailboxes
    def add_mailbox(self, cred, protocol="graph"):
        """Insert one mailbox credential; returns (id, created?)."""
        email = cred["email"].strip().lower()
        ts = now()

        def op(c):
            cur = c.execute(
                "INSERT INTO accounts (email,password,client_id,client_secret,"
                "refresh_token,protocol,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (email, cred.get("password", ""), cred.get("client_id", ""),
                 cred.get("client_secret", ""), cred.get("refresh_token", ""),
                 protocol or "graph", ts, ts))
            return cur.lastrowid

        try:
            return self._write(op), True
        except sqlite3.IntegrityError:
            row = self.conn().execute(
                "SELECT id FROM accounts WHERE email=?", (email,)).fetchone()
            return (row["id"] if row else None), False

    @staticmethod
    def parse_line(raw):
        """email----password----client_id----[client_secret----]refresh_token

        Four fields is the Microsoft form; five is Gmail (Google will not refresh
        without the client secret). Returns None for blank/comment lines.
        """
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            return None
        parts = [p.strip() for p in raw.split("----")]
        # iCloud Hide-My-Email read through Gmail:
        #   alias@icloud.com----<the gmail that owns the Apple ID>----<app password>----gmail-imap
        # The gmail address rides in client_id and the app password in password -
        # both columns exist, and the reader needs nothing else.
        if len(parts) >= 3 and parts[-1].lower() in ("gmail-imap", "alias-imap"):
            return {"email": parts[0], "client_id": parts[1],
                    "password": parts[2], "refresh_token": "",
                    "_protocol": "alias-imap"}
        # iCloud Hide-My-Email: alias@icloud.com----acc_xxxxxxxx----hme
        if len(parts) >= 2 and parts[-1].lower() == "hme":
            # the HME account id goes in client_id: add_mailbox persists that
            # column already, so nothing else has to change to carry it.
            return {"email": parts[0],
                    "hme_account": parts[1] if len(parts) > 2 else "",
                    "password": "", "client_id": parts[1] if len(parts) > 2 else "",
                    "refresh_token": ""}
        if len(parts) >= 5:
            return {"email": parts[0], "password": parts[1], "client_id": parts[2],
                    "client_secret": parts[3], "refresh_token": parts[4]}
        if len(parts) >= 4:
            return {"email": parts[0], "password": parts[1],
                    "client_id": parts[2], "refresh_token": parts[3]}
        return None

    def add_mailboxes(self, lines, protocol="graph"):
        """Bulk import credential lines.

        The whole batch shares one fetch protocol (graph or imap), chosen at
        import time - except for the lines that carry their own. Gmail lines are
        recognised by shape, and an iCloud alias read through Gmail names its
        reader in the line itself, so a mixed batch imports cleanly and each row
        ends up with the protocol that can actually read it.
        """
        added, dup = 0, 0
        for raw in lines:
            cred = self.parse_line(raw)
            if not cred:
                continue
            _, created = self.add_mailbox(cred, cred.get("_protocol") or protocol)
            added += 1 if created else 0
            dup += 0 if created else 1
        return added, dup

    def check_targets(self, limit=None, skip_hits=True, only=None):
        """要检测的账号，最久没查过的优先（凭据全带）。

        顺序是刻意的：`checked_at IS NULL` 排最前（新导入的账号先看一遍），
        然后是等得最久的。这样一轮扫描的时间不会因为某个邮箱卡住而永远轮不到
        别的账号 —— 每轮都从最欠检查的那一头开始。

        skip_hits 默认只跳过已中签的账号，省掉对同一个答案的重复搜索；中签后
        仍想继续盯（比如后续还会发付款链接）就把它关掉。
        """
        sql = ("SELECT * FROM accounts WHERE "
               + ("hit_at IS NULL" if skip_hits else "1=1")
               + (" AND status=?" if only else "")
               + " ORDER BY checked_at IS NOT NULL, checked_at ASC, id ASC")
        args = [only] if only else []
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self.conn().execute(sql, args)]

    def account(self, account_id):
        row = self.conn().execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- 检测结果
    def record_check(self, account_id, mail_at=None, mail_subject="",
                     hit_source=None, hit_note="", error=None,
                     baseline_at=None, touch=True):
        """写入一次检测的结果。

        中签是单向的：`hit_at` 一旦写上就不再被后续结果覆盖。一封已经被确认
        过的信不会因为下一次扫描没再看见它而失效（邮箱会清理、Gmail 的 SINCE
        窗口会滑走），所以这里只补不撤。

        `error` 只记录本轮检测为什么没拿到邮件 —— 读不到信 ≠ 没中签，所以它
        既不清 `hit_at`，也不把账号移出检测队列。

        `touch=False` 用于「本轮没来得及读」：只记原因，不推进 checked_at，
        这样账号下一轮仍然排在最前面被优先重试，而不是被排到队尾。
        """
        stamp = time.time()
        progress = ("checked_at=?, check_count=check_count+1,"
                    if touch else "")

        def op(c):
            if hit_source:
                c.execute(
                    "UPDATE accounts SET " + progress +
                    "check_error=?, last_mail_at=COALESCE(?,last_mail_at),"
                    "last_mail_subject=COALESCE(?,last_mail_subject),"
                    "baseline_at=COALESCE(?,baseline_at),"
                    "hit_at=COALESCE(hit_at,?), hit_source=COALESCE(hit_source,?),"
                    "hit_note=COALESCE(hit_note,?), status='hit', updated_at=?"
                    " WHERE id=?",
                    ((stamp,) if touch else ()) +
                    (None if error is None else str(error)[:300],
                     mail_at, mail_subject or None, baseline_at,
                     stamp, hit_source, (hit_note or "")[:300], now(), account_id))
                return
            c.execute(
                "UPDATE accounts SET " + progress +
                "check_error=?, last_mail_at=COALESCE(?,last_mail_at),"
                "last_mail_subject=COALESCE(?,last_mail_subject),"
                "baseline_at=COALESCE(?,baseline_at), updated_at=? WHERE id=?",
                ((stamp,) if touch else ()) +
                (None if error is None else str(error)[:300],
                 mail_at, mail_subject or None, baseline_at, now(), account_id))
        return self._write(op)

    def mark_hit(self, account_id, source, note="", when=None, mail_at=None,
                 mail_subject=""):
        """人工或程序直接标中签（幂等）。"""
        stamp = when or time.time()
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET hit_at=COALESCE(hit_at,?), "
            "hit_source=COALESCE(hit_source,?), hit_note=COALESCE(hit_note,?), "
            "last_mail_at=COALESCE(?,last_mail_at), "
            "last_mail_subject=COALESCE(?,last_mail_subject), "
            "status='hit', updated_at=? WHERE id=?",
            (stamp, source, (note or "")[:300], mail_at, mail_subject or None,
             now(), account_id)))

    def hits(self, limit=None):
        """中签名单，最新中签在前。"""
        sql = ("SELECT id,email,protocol,hit_at,hit_source,hit_note,last_mail_at,"
               "last_mail_subject,checked_at,check_count,status,first_name,"
               "last_name,phone FROM accounts WHERE hit_at IS NOT NULL "
               "ORDER BY hit_at DESC, id DESC")
        c = self.conn()
        if limit:
            return [dict(r) for r in c.execute(sql + " LIMIT ?", (limit,))]
        return [dict(r) for r in c.execute(sql)]

    def migrate_legacy_hits(self):
        """把上一版程序的结果并入中签名单 —— merge 点。

        两种旧记录都代表「站点已经收下」：

          * status='registered' —— 成功邮件到过（HTTP 模式实测会发）
          * status='submitted'  —— 页面确认注册成功，浏览器模式一条成功邮件都
            不发
          * 错误里含 `success mail never arrived` —— confirm 回 OK 但成功邮件
            180s 没到。旧程序把这句写进 error 并按失败处理，它字面上是错误、
            语义上是成功，这是必须合并的一类，否则它们会被「没有成功邮件」
            整批吞掉。

        幂等，而且是**可观测的**幂等：三条语句都带 `hit_at IS NULL`，所以返回的
        是「这次真正新并入了几条」。少了这个条件，SQLite 的 rowcount 会把「匹配到
        但值没变」的行也算进去 —— 每次打开库都报一句「并入 2 条」，而实际一条都
        没变，看日志的人会以为名单在涨。

        时间一律写 epoch 秒（不是 `now()` 那种文本时间）：这一列要和检测流程
        写进去的值同类型，否则同一列里混着 TEXT 和 REAL，`_stamp()` 转不出
        人类时间、`ORDER BY hit_at` 也会把文本排在数字后面。
        """
        stamp = time.time()

        def op(c):
            changed = 0
            # 1) 成功邮件到过
            cur = c.execute(
                "UPDATE accounts SET hit_at=COALESCE(hit_at,?), "
                "hit_source='success-mail', "
                "hit_note=COALESCE(hit_note,'旧记录：成功邮件已到'), "
                "status='hit', updated_at=? "
                "WHERE status='registered' AND hit_at IS NULL", (stamp, now()))
            changed += cur.rowcount
            # 2) 站点确认、页面确认，或那条「confirm OK 但成功邮件没来」
            cur = c.execute(
                "UPDATE accounts SET hit_at=COALESCE(hit_at,?), "
                "hit_source='site-ok', "
                "hit_note=COALESCE(hit_note,"
                "'旧记录：站点已确认，活动结束后不会再有成功邮件'), "
                "status='hit', updated_at=? "
                "WHERE (status='submitted' OR error LIKE ?) "
                "AND hit_at IS NULL",
                (stamp, now(), f"%{hitcheck.NO_MAIL_OK_MARK}%"))
            changed += cur.rowcount
            # 3) 同一条错误可能只落在 registrations.response 上
            cur = c.execute(
                "UPDATE accounts SET hit_at=COALESCE(hit_at,?), "
                "hit_source='site-ok', "
                "hit_note=COALESCE(hit_note,"
                "'旧记录：站点已确认，活动结束后不会再有成功邮件'), "
                "status='hit', updated_at=? WHERE hit_at IS NULL AND id IN ("
                "SELECT account_id FROM registrations WHERE response LIKE ?)",
                (stamp, now(), f"%{hitcheck.NO_MAIL_OK_MARK}%"))
            changed += cur.rowcount
            return changed
        return self._write(op)

    def reset_checks(self):
        """清掉检测错误，让每个账号重新排队（不触碰已成立的中签）。"""
        def op(c):
            cur = c.execute(
                "UPDATE accounts SET checked_at=NULL, check_count=0, "
                "check_error=NULL, updated_at=? WHERE hit_at IS NULL", (now(),))
            return cur.rowcount
        return self._write(op)

    def update_refresh_token(self, account_id, refresh_token):
        """Microsoft rotates the refresh token on every redemption; keep it."""
        if not refresh_token:
            return
        self._write(lambda c: c.execute(
            "UPDATE accounts SET refresh_token=?, updated_at=? WHERE id=?",
            (refresh_token, now(), account_id)))

    def reset_failed(self):
        """Put failed and stuck accounts back in the queue.

        'running' rows are leftovers from an interrupted run: nothing will ever
        reclaim them automatically, so they are swept up here too. 检测程序里
        「放回队列」= 清掉检测记录重来，所以直接走 reset_checks。
        """
        return self.reset_checks()

    # --------------------------------------------------------------------- queries
    def stats(self):
        """计数。`hits` 是唯一的结论性数字，其余是检测进度。

        `registered` / `submitted` / `failed` 是上一版程序留下的 status 值，
        登录到中签名单后它们仍会被统计出来，所以老库的第一眼不会是一片空白。
        """
        c = self.conn()
        out = {}
        for row in c.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status"):
            out[row["status"]] = row["n"]
        out["total"] = c.execute(
            "SELECT COUNT(*) n FROM accounts").fetchone()["n"]
        out["hits"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE hit_at IS NOT NULL").fetchone()["n"]
        out["checked"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE checked_at IS NOT NULL").fetchone()["n"]
        out["unchecked"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE checked_at IS NULL AND "
            "hit_at IS NULL").fetchone()["n"]
        out["check_errors"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE check_error IS NOT NULL AND "
            "hit_at IS NULL").fetchone()["n"]
        out["registrations"] = c.execute(
            "SELECT COUNT(*) n FROM registrations").fetchone()["n"]
        return out

    def accounts(self, limit=None):
        """Newest-first accounts. limit=None returns everything (used by exports).

        The table views pass a limit because rendering 8000+ rows is pointless;
        anything that writes a file must not, or the file silently truncates.
        """
        sql = ("SELECT id,email,first_name,last_name,phone,date_of_birth,status,"
               "protocol,error,updated_at,hit_at,hit_source,hit_note,checked_at,"
               "check_count,check_error,last_mail_at,last_mail_subject "
               "FROM accounts ORDER BY id DESC")
        c = self.conn()
        if limit:
            return [dict(r) for r in c.execute(sql + " LIMIT ?", (limit,))]
        return [dict(r) for r in c.execute(sql)]

    def mail_requested(self, email):
        """Epoch the site was last asked for a verification mail, or None.

        Nothing writes this any more - the monitor never asks the site for
        anything. It is kept readable because on an old database it is the
        record of what the previous build already asked for, which is the first
        thing to check when an address turns out to have a dead session.
        """
        row = self.conn().execute(
            "SELECT mail_requested_at FROM accounts WHERE email=?",
            (email,)).fetchone()
        return row["mail_requested_at"] if row else None

    def mark_mail_requested(self, email, when):
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET mail_requested_at=? WHERE email=?",
            (when, email)))

    def protocol_for(self, account_id):
        row = self.conn().execute(
            "SELECT protocol FROM accounts WHERE id=?", (account_id,)).fetchone()
        return (row["protocol"] if row and row["protocol"] else "graph")

    def registrations(self, limit=None):
        """上一版程序留下的预约记录，只读。

        检测程序不再写这张表，但它是「这个账号当时预约了哪一场」的唯一记录，
        而中签本身是场次级的事 —— 没有它，中签名单就只剩邮箱地址。
        """
        sql = ("SELECT r.id,a.email,r.artist_id,r.poll_answer_ids,r.mode,r.status,"
               "r.created_at FROM registrations r "
               "JOIN accounts a ON a.id=r.account_id ORDER BY r.id DESC")
        c = self.conn()
        if limit:
            return [dict(r) for r in c.execute(sql + " LIMIT ?", (limit,))]
        return [dict(r) for r in c.execute(sql)]

    # ------------------------------------------------------------------- cleanup
    def delete_hits(self):
        """删除中签名单（预约记录随账号级联删除）。"""
        return self._write(lambda c: c.execute(
            "DELETE FROM accounts WHERE hit_at IS NOT NULL").rowcount)

    def delete_accounts(self, status=None):
        """Delete accounts (registrations follow via ON DELETE CASCADE).

        status=None wipes the whole pool; a status clears just that bucket.
        """
        def op(c):
            if status:
                return c.execute("DELETE FROM accounts WHERE status=?",
                                 (status,)).rowcount
            return c.execute("DELETE FROM accounts").rowcount
        n = self._write(op)
        return n

    def vacuum(self):
        """Reclaim the file space after a large delete."""
        with self._write_lock:
            c = self.conn()
            c.execute("VACUUM")
            c.commit()

    @staticmethod
    def _cred_line(row):
        """Rebuild the import line, keeping the 5-field shape for Gmail."""
        base = f"{row['email']}----{row['password']}----{row['client_id']}----"
        secret = row["client_secret"] if "client_secret" in row.keys() else ""
        if secret:
            return f"{base}{secret}----{row['refresh_token']}"
        return f"{base}{row['refresh_token']}"

    def cred_lines(self):
        """Current credential lines, for exporting the working set.

        Written in the shape that imports back in. An iCloud alias read through
        Gmail has no refresh token at all - its credential is the inbox address
        and the app password - so it gets its own form instead of being skipped
        by the refresh_token test or written as a Microsoft line.
        """
        out = []
        for r in self.conn().execute(
                "SELECT email,password,client_id,client_secret,refresh_token,"
                "protocol FROM accounts ORDER BY id"):
            if r["protocol"] == "alias-imap":
                out.append(f"{r['email']}----{r['client_id'] or ''}----"
                           f"{r['password'] or ''}----gmail-imap")
            elif r["refresh_token"]:
                out.append(self._cred_line(r))
        return out

    def cred_line_for(self, account_id):
        """One account's credential line, in a shape that imports back in."""
        row = self.conn().execute(
            "SELECT email,password,client_id,client_secret,refresh_token,protocol "
            "FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            return None
        if row["protocol"] == "alias-imap":
            # 别名的凭据是「收件箱地址 + 应用专用密码」，它本来就没有 refresh
            # token，用 _cred_line 的判据会被整批丢掉。
            return (f"{row['email']}----{row['client_id'] or ''}----"
                    f"{row['password'] or ''}----gmail-imap")
        if not row["refresh_token"]:
            return None
        return self._cred_line(row)


if __name__ == "__main__":
    import sys
    s = Store(sys.argv[1] if len(sys.argv) > 1 else "/tmp/oasis_test.db")
    print("stats:", s.stats())
