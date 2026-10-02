from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import sqlite3
import time
from pathlib import Path


LOGGER = logging.getLogger("easymesh.remote")


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt:{salt}:{digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, digest = encoded.split(":")
        return algorithm == "scrypt" and secrets.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    def __init__(self, path, idle_seconds=600, maximum_seconds=3600, handoff_seconds=125, clock=time.time):
        self.path = Path(path)
        self.idle_seconds = idle_seconds
        self.maximum_seconds = maximum_seconds
        self.handoff_seconds = handoff_seconds
        self.clock = clock
        with self.transaction() as database:
            database.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT, expires REAL)")
            database.execute("""CREATE TABLE IF NOT EXISTS reservation (
                slot INTEGER PRIMARY KEY CHECK(slot=1), token TEXT, username TEXT,
                idle_until REAL NOT NULL, hard_until REAL NOT NULL, available_at REAL NOT NULL)""")
            database.execute("INSERT OR IGNORE INTO reservation VALUES (1, NULL, NULL, 0, 0, 0)")
            database.execute("CREATE TABLE IF NOT EXISTS maintenance (slot INTEGER PRIMARY KEY CHECK(slot=1), enabled INTEGER)")
            database.execute("INSERT OR IGNORE INTO maintenance VALUES (1, 0)")

    @contextlib.contextmanager
    def transaction(self):
        database = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        database.row_factory = sqlite3.Row
        try:
            database.execute("BEGIN IMMEDIATE")
            yield database
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()

    def expire(self, database):
        now = self.clock()
        database.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        lease = database.execute("SELECT * FROM reservation WHERE slot=1").fetchone()
        if lease["token"]:
            valid_session = database.execute("SELECT 1 FROM sessions WHERE token=?", (lease["token"],)).fetchone()
            if not valid_session or now >= min(lease["idle_until"], lease["hard_until"]):
                self.clear(database, now, "expired")

    def clear(self, database, now, reason):
        lease = database.execute("SELECT username FROM reservation WHERE slot=1").fetchone()
        LOGGER.info("reservation ended user=%s reason=%s", lease["username"], reason)
        database.execute("""UPDATE reservation SET token=NULL, username=NULL,
            idle_until=0, hard_until=0, available_at=? WHERE slot=1""", (now + self.handoff_seconds,))

    def login(self, username):
        token = secrets.token_urlsafe(32)
        with self.transaction() as database:
            self.expire(database)
            if database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 256:
                raise ValueError("Too many login sessions; wait for a session to expire.")
            database.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                             (token_hash(token), username, self.clock() + 8 * 3600))
        return token

    def status(self, token=""):
        with self.transaction() as database:
            self.expire(database)
            return self.describe(database, token)

    def describe(self, database, token):
        now = self.clock()
        session = database.execute("SELECT * FROM sessions WHERE token=?", (token_hash(token),)).fetchone()
        lease = database.execute("SELECT * FROM reservation WHERE slot=1").fetchone()
        return {
            "authenticated": bool(session),
            "username": session["username"] if session else None,
            "owner": lease["username"],
            "mine": bool(session and lease["token"] == token_hash(token)),
            "busy": bool(lease["token"]),
            "idle_remaining": max(0, lease["idle_until"] - now),
            "maximum_remaining": max(0, lease["hard_until"] - now),
            "handoff_remaining": max(0, lease["available_at"] - now),
            "maintenance": bool(database.execute("SELECT enabled FROM maintenance WHERE slot=1").fetchone()[0]),
        }

    def acquire(self, token):
        with self.transaction() as database:
            self.expire(database)
            status = self.describe(database, token)
            if not status["authenticated"]:
                raise PermissionError("Sign in first.")
            if status["maintenance"]:
                raise BlockingIOError("The host administrator has reserved the lab for maintenance or local testing.")
            if status["mine"]:
                return status
            if status["busy"] or status["handoff_remaining"]:
                raise BlockingIOError("The lab is reserved or completing its previous session.")
            now = self.clock()
            database.execute("""UPDATE reservation SET token=?, username=?, idle_until=?,
                hard_until=?, available_at=0 WHERE slot=1""",
                             (token_hash(token), status["username"], now + self.idle_seconds,
                              now + self.maximum_seconds))
            return self.describe(database, token)

    def activity(self, token):
        with self.transaction() as database:
            self.expire(database)
            status = self.describe(database, token)
            if not status["mine"]:
                raise PermissionError("This browser no longer owns the lab.")
            database.execute("UPDATE reservation SET idle_until=MIN(?, hard_until) WHERE slot=1",
                             (self.clock() + self.idle_seconds,))
            return self.describe(database, token)

    def release(self, token="", force=False):
        with self.transaction() as database:
            self.expire(database)
            status = self.describe(database, token)
            if not force and not status["mine"]:
                raise PermissionError("Only the owning browser can release the lab.")
            if status["busy"]:
                self.clear(database, self.clock(), "administrator" if force else "released")

    def logout(self, token):
        with self.transaction() as database:
            self.expire(database)
            if self.describe(database, token)["mine"]:
                self.clear(database, self.clock(), "logout")
            database.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))

    def revoke_user(self, username):
        with self.transaction() as database:
            database.execute("DELETE FROM sessions WHERE username=?", (username,))
            self.expire(database)

    def set_maintenance(self, enabled):
        with self.transaction() as database:
            self.expire(database)
            if enabled and self.describe(database, "")["busy"]:
                self.clear(database, self.clock(), "maintenance")
            database.execute("UPDATE maintenance SET enabled=? WHERE slot=1", (int(enabled),))
