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
    -- 「这个地址预约成功过」。只有预约过的地址才可能中签，所以检测范围默认就是
    -- opted_in=1 的账号；池子里的其他地址（试过但没成功、或者压根只是存着）不查。
    --
    -- 注意这里**不是**中签：预约（registration）与中签（ballot result）是两件事，
    -- 前者在 2026-09-17 16:00 BST 截止，后者要等 Oasis 之后发结果信。把预约成功
    -- 当成中签，会让名单上出现几百个「已中签」而它们一张票都没有 —— 更糟的是，
    -- 中了签的账号会被当成已经查过、从此不再检测。
    opted_in        INTEGER NOT NULL DEFAULT 0,
    opted_in_source TEXT,
    opted_in_at     REAL,
    -- 预约成功的时间（能推断出来时）。来自上一版程序的成功记录或邮件的到达时间。
    registered_at   REAL,
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

-- 一次性的迁移标记。有些修正只该跑一次（比如把误判的中签清掉），而在数据里
-- 找不到可靠的判据 —— 只能自己记一笔。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
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
        opted = self.migrate_legacy()
        if opted:
            print(f"store: 推断出已预约 {opted} 个（纳入检测范围）", flush=True)

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
                ("last_mail_subject", "TEXT"), ("baseline_at", "REAL"),
                ("opted_in", "INTEGER NOT NULL DEFAULT 0"),
                ("opted_in_source", "TEXT"), ("opted_in_at", "REAL"),
                ("registered_at", "REAL")):
            if name not in cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")
        rcols = {r["name"] for r in c.execute("PRAGMA table_info(registrations)")}
        if "mode" not in rcols:
            c.execute("ALTER TABLE registrations ADD COLUMN mode TEXT "
                      "NOT NULL DEFAULT 'http'")

    def meta_get(self, key, default=None):
        row = self.conn().execute("SELECT value FROM meta WHERE key=?",
                                  (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key, value):
        return self._write(lambda c: c.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))))
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
    def add_mailbox(self, cred, protocol="graph", opted_in=False):
        """Insert one mailbox credential; returns (id, created?).

        `opted_in` 把新账号直接纳入检测范围。导入面板默认勾上它 —— 操作者把
        一批地址粘进来，图的本来就是「查这些」，再让他勾一遍是多余的。
        """
        email = cred["email"].strip().lower()
        ts = now()

        def op(c):
            cur = c.execute(
                "INSERT INTO accounts (email,password,client_id,client_secret,"
                "refresh_token,protocol,created_at,updated_at,opted_in,"
                "opted_in_source,opted_in_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (email, cred.get("password", ""), cred.get("client_id", ""),
                 cred.get("client_secret", ""), cred.get("refresh_token", ""),
                 protocol or "graph", ts, ts, 1 if opted_in else 0,
                 "import" if opted_in else None, time.time() if opted_in else None))
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

    def add_mailboxes(self, lines, protocol="graph", opted_in=False):
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
            account_id, created = self.add_mailbox(
                cred, cred.get("_protocol") or protocol, opted_in=opted_in)
            if not created and opted_in and account_id:
                # 重复导入的地址如果这次要求标记，就顺手标上：操作者粘贴一整份
                # 名单时，其中一部分早就在池子里了，不该只标记新来的那一半。
                self.mark_opted([account_id], on=True, source="import")
            added += 1 if created else 0
            dup += 0 if created else 1
        return added, dup

    def check_targets(self, limit=None, skip_hits=True, only=None,
                      only_opted=True):
        """要检测的账号，最久没查过的优先（凭据全带）。

        `only_opted` 默认只给「已预约」的地址：没预约过的邮箱不会收到中签信，
        扫它纯属浪费 —— 一个 800 人的池子里可能只有一半提交过注册，每一轮把这
        另一半也读一遍是实打实的无效请求。检测范围由 opted_in 决定，而它来自
        三处：上一版程序留下的成功记录（自动并入）、导入时的标记、以及手工标记。


        顺序是刻意的：`checked_at IS NULL` 排最前（新导入的账号先看一遍），
        然后是等得最久的。这样一轮扫描的时间不会因为某个邮箱卡住而永远轮不到
        别的账号 —— 每轮都从最欠检查的那一头开始。

        skip_hits 默认只跳过已中签的账号，省掉对同一个答案的重复搜索；中签后
        仍想继续盯（比如后续还会发付款链接）就把它关掉。
        """
        sql = ("SELECT * FROM accounts WHERE "
               + ("opted_in=1" if only_opted else "1=1")
               + (" AND hit_at IS NULL" if skip_hits else "")
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

    def migrate_legacy(self):
        """把上一版程序留下的记录翻译成现在这两件事，并纠正一次误判。

        预约（registration）与中签（结果）是两件事：前者 2026-09-17 16:00 BST
        截止，后者要等 Oasis 发结果信。上一版把「注册成功」直接写成了中签 ——
        于是名单上出现几百个「已中签」，而它们其实只是预约成功；更糟的是那些
        账号会被 `skip_hits` 当成已经查过，真正的中签通知来了反而不查。

        所以这里做两件事，都是幂等的：

          1. **撤销那次误判**（只做一次，靠 meta 表记着）：把每个 `hit_at` 搬到
             `registered_at` —— 它记录的其实是「预约成功的时间」—— 然后清空
             `hit_at` / `hit_source` / `hit_note`。此后 `hit_at` 只由检测写，
             而那意味着「截止时间之后收到了 Oasis 的新来信」。
          2. **推断「预约成功过」**：状态是 registered/submitted、错误里含那条
             confirm OK、有 registrations 行的，都算预约过，纳入检测范围。

        「用别的工具或手工预约」的地址推不出来，那部分交给界面的标记功能。
        """
        if not self.meta_get("migration.optin_split"):
            def split(c):
                # 旧的 hit_at 是「预约成功」的时刻，搬去它该在的列。
                c.execute("UPDATE accounts SET registered_at=COALESCE("
                          "registered_at, hit_at) WHERE hit_at IS NOT NULL")
                cur = c.execute("UPDATE accounts SET hit_at=NULL, hit_source=NULL,"
                                "hit_note=NULL WHERE hit_at IS NOT NULL")
                return cur.rowcount
            cleared = self._write(split)
            self.meta_set("migration.optin_split", "1")
            if cleared:
                print(f"store: 撤销上一版误判的「中签」{cleared} 条"
                      f"（它们是预约成功，不是中签）", flush=True)

        def infer(c):
            cur = c.execute(
                "UPDATE accounts SET opted_in=1, opted_in_source='legacy', "
                "opted_in_at=COALESCE(opted_in_at,?), "
                "registered_at=COALESCE(registered_at, "
                "  strftime('%s', updated_at)) "
                "WHERE opted_in=0 AND ("
                "status IN ('registered','submitted') "
                "OR error LIKE ? "
                "OR id IN (SELECT account_id FROM registrations))",
                (time.time(), f"%{hitcheck.NO_MAIL_OK_MARK}%"))
            return cur.rowcount
        return self._write(infer)

    def mark_opted(self, account_ids, on=True, source="manual"):
        """把一批账号标进/移出检测范围。

        这是给「用别的工具或手工预约的邮箱」准备的口子：程序推不出来它们预约过，
        但操作者知道。返回真正改变的行数。
        """
        ids = [int(i) for i in (account_ids or [])]
        if not ids:
            return 0
        flag = 1 if on else 0
        stamp = time.time() if on else None
        marks = ",".join("?" * len(ids))

        def op(c):
            cur = c.execute(
                f"UPDATE accounts SET opted_in=?, opted_in_source=?, "
                f"opted_in_at=?, updated_at=? WHERE id IN ({marks})",
                [flag, source if on else None, stamp, now()] + ids)
            return cur.rowcount
        return self._write(op)

    def mark_unmarked(self, on=True, source="manual"):
        """把所有还没标记的账号一次性标进来（或全部取消）。

        一整份名单一次标完，是这里的典型用法 —— 让操作者逐个勾八百个邮箱没有
        意义。
        """
        flag = 1 if on else 0
        stamp = time.time() if on else None
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET opted_in=?, opted_in_source=?, opted_in_at=?, "
            "updated_at=? WHERE opted_in=0",
            (flag, source if on else None, stamp, now())).rowcount)

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
        """计数。

        口径分成两层，别混：`total` 是池子里有多少地址，`opted` 是其中真的预约
        过、因此会被检测的那部分。进度类的三个数（unchecked / checked /
        check_errors）都只在 opted 里算 —— 以前它们按全体算，于是「还没查过 453」
        里混着一堆永远不会被查、查了也没有意义的地址，看起来像进度落后，实际
        是分母错了。
        """
        c = self.conn()
        out = {}
        for row in c.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status"):
            out[row["status"]] = row["n"]
        out["total"] = c.execute(
            "SELECT COUNT(*) n FROM accounts").fetchone()["n"]
        out["opted"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE opted_in=1").fetchone()["n"]
        out["unmarked"] = out["total"] - out["opted"]
        out["hits"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE hit_at IS NOT NULL").fetchone()["n"]
        out["unchecked"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE opted_in=1 AND hit_at IS NULL "
            "AND checked_at IS NULL").fetchone()["n"]
        out["checked"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE opted_in=1 AND hit_at IS NULL "
            "AND checked_at IS NOT NULL").fetchone()["n"]
        out["check_errors"] = c.execute(
            "SELECT COUNT(*) n FROM accounts WHERE opted_in=1 AND hit_at IS NULL "
            "AND check_error IS NOT NULL").fetchone()["n"]
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
               "check_count,check_error,last_mail_at,last_mail_subject,"
               "opted_in,opted_in_source,opted_in_at,registered_at "
               "FROM accounts ORDER BY id DESC")
        c = self.conn()
        if limit:
            return [dict(r) for r in c.execute(sql + " LIMIT ?", (limit,))]
        return [dict(r) for r in c.execute(sql)]

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
