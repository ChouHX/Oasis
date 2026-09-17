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
"""
import json
import os
import sqlite3
import threading
import time

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

    def _migrate(self, c):
        """Additive migrations for databases created by an earlier build."""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
        if "protocol" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN protocol TEXT "
                      "NOT NULL DEFAULT 'graph'")
        if "client_secret" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN client_secret TEXT")
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
        import time. Gmail lines carry an extra field and are recognised by
        shape, so a mixed batch imports cleanly.
        """
        added, dup = 0, 0
        for raw in lines:
            cred = self.parse_line(raw)
            if not cred:
                continue
            _, created = self.add_mailbox(cred, protocol)
            added += 1 if created else 0
            dup += 0 if created else 1
        return added, dup

    def identity_taken(self, ident):
        row = self.conn().execute(
            "SELECT 1 FROM accounts WHERE first_name=? AND last_name=? AND phone=?",
            (ident["first_name"], ident["last_name"], ident["phone"])).fetchone()
        return row is not None

    def bind_identity(self, account_id, ident):
        """Attach a fresh identity and move the account to 'running'."""
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET first_name=?,last_name=?,phone=?,"
            "date_of_birth=?,location_json=?,status='running',error=NULL,"
            "updated_at=? WHERE id=?",
            (ident["first_name"], ident["last_name"], ident["phone"],
             ident["date_of_birth"], json.dumps(ident["location"]),
             now(), account_id)))

    def claim_pending(self, limit=1):
        """Reserve up to `limit` pending mailboxes for this worker."""
        def op(c):
            rows = c.execute(
                "SELECT * FROM accounts WHERE status='pending' "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
            claimed = []
            for r in rows:
                cur = c.execute(
                    "UPDATE accounts SET status='running', updated_at=? "
                    "WHERE id=? AND status='pending'", (now(), r["id"]))
                if cur.rowcount:
                    claimed.append(dict(r))
            return claimed
        return self._write(op)

    def release(self, account_id, error=None):
        """Put a claimed mailbox back in the queue (transient failure)."""
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET status='pending', error=?, updated_at=? "
            "WHERE id=? AND status='running'", (error, now(), account_id)))

    def fail(self, account_id, error):
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET status='failed', error=?, updated_at=? WHERE id=?",
            (str(error)[:500], now(), account_id)))

    def mark_registered(self, account_id):
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET status='registered', error=NULL, updated_at=? "
            "WHERE id=?", (now(), account_id)))

    def mark_submitted(self, account_id, note=""):
        """Registered as far as the page's own confirmation goes.

        Browser mode is known not to send the "Registration Complete" mail, so
        the page showing "Thanks for registering" is the only evidence there
        is. Kept distinct from 'registered' (which means the mail arrived) so a
        run can tell the two apart afterwards.
        """
        return self._write(lambda c: c.execute(
            "UPDATE accounts SET status='submitted', error=?, updated_at=? "
            "WHERE id=?", (note or None, now(), account_id)))

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
        reclaim them automatically, so they are swept up here too.
        """
        def op(c):
            cur = c.execute("UPDATE accounts SET status='pending', error=NULL, "
                            "updated_at=? WHERE status IN ('failed','running')",
                            (now(),))
            return cur.rowcount
        return self._write(op)

    # -------------------------------------------------------------- registrations
    def registration_exists(self, account_id, artist_id):
        row = self.conn().execute(
            "SELECT 1 FROM registrations WHERE account_id=? AND artist_id=?",
            (account_id, artist_id)).fetchone()
        return row is not None

    def record_registration(self, account_id, artist_id, page_id, session_id,
                            poll_id, poll_answer_ids, journey, token, response,
                            mode="http"):
        """Returns True when a new row landed, False when it was a duplicate."""
        def op(c):
            c.execute(
                "INSERT INTO registrations (account_id,artist_id,page_id,"
                "session_id,poll_id,poll_answer_ids,journey_json,token,"
                "response,mode,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, artist_id, page_id, session_id, poll_id,
                 json.dumps(poll_answer_ids), json.dumps(journey), token,
                 response, mode or "http", "submitted", now()))
            return True
        try:
            return self._write(op)
        except sqlite3.IntegrityError:
            return False

    # --------------------------------------------------------------------- queries
    def stats(self):
        c = self.conn()
        out = {}
        for row in c.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status"):
            out[row["status"]] = row["n"]
        out["registrations"] = c.execute(
            "SELECT COUNT(*) n FROM registrations").fetchone()["n"]
        out["total"] = c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
        return out

    def accounts(self, limit=None):
        """Newest-first accounts. limit=None returns everything (used by exports).

        The table views pass a limit because rendering 8000+ rows is pointless;
        anything that writes a file must not, or the file silently truncates.
        """
        sql = ("SELECT id,email,first_name,last_name,phone,date_of_birth,status,"
               "protocol,error,updated_at FROM accounts ORDER BY id DESC")
        c = self.conn()
        if limit:
            return [dict(r) for r in c.execute(sql + " LIMIT ?", (limit,))]
        return [dict(r) for r in c.execute(sql)]

    def pending_emails(self, limit=20):
        """Oldest-first pending addresses, for the mail prefetcher.

        Deliberately minimal: the prefetcher only needs to know which addresses
        will be worked on next, and pulling whole rows (credentials included)
        on every poll would be wasteful.
        """
        c = self.conn()
        return [r["email"] for r in c.execute(
            "SELECT email FROM accounts WHERE status='pending' "
            "ORDER BY id LIMIT ?", (limit,))]

    def protocol_for(self, account_id):
        row = self.conn().execute(
            "SELECT protocol FROM accounts WHERE id=?", (account_id,)).fetchone()
        return (row["protocol"] if row and row["protocol"] else "graph")

    def registrations(self, limit=None):
        sql = ("SELECT r.id,a.email,r.artist_id,r.poll_answer_ids,r.mode,r.status,"
               "r.created_at FROM registrations r "
               "JOIN accounts a ON a.id=r.account_id ORDER BY r.id DESC")
        c = self.conn()
        if limit:
            return [dict(r) for r in c.execute(sql + " LIMIT ?", (limit,))]
        return [dict(r) for r in c.execute(sql)]

    # ------------------------------------------------------------------- cleanup
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
        """Current credential lines, for exporting the working set."""
        return [self._cred_line(r) for r in self.conn().execute(
            "SELECT email,password,client_id,client_secret,refresh_token "
            "FROM accounts WHERE refresh_token <> '' ORDER BY id")]

    def cred_line_for(self, account_id):
        row = self.conn().execute(
            "SELECT email,password,client_id,client_secret,refresh_token "
            "FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row or not row["refresh_token"]:
            return None
        return self._cred_line(row)


if __name__ == "__main__":
    import sys
    s = Store(sys.argv[1] if len(sys.argv) > 1 else "/tmp/oasis_test.db")
    print("stats:", s.stats())
