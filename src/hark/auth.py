"""Accounts and sessions — split out of web.py (2026-07-13) as that module
grew past ~1900 lines mixing routing, templates, and auth. Auth state lives
in its own SQLite file (auth.db), NOT in hark.db — data snapshots pushed
from the pipeline replace hark.db wholesale and must never wipe accounts or
sessions. Passwords are stretched (iterated salted SHA-256) and compared in
constant time; changing a password revokes every session for that account.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PW_ITERS = 120_000
SESSION_DAYS = 30
INVITE_EXPIRES_DAYS = 7

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY,
    username          TEXT NOT NULL UNIQUE,
    salt              TEXT,
    password_hash     TEXT,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    invite_token      TEXT,
    invite_expires_at TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
"""

# Columns added after 0.13.0 — same bolt-on idiom as db.py's _MIGRATIONS,
# scoped to auth.db instead (it had no migration path at all before this;
# executescript()'s CREATE-IF-NOT-EXISTS never touched existing tables).
_AUTH_MIGRATIONS = (
    ("users", "is_admin", "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"),
    ("users", "invite_token", "ALTER TABLE users ADD COLUMN invite_token TEXT"),
    ("users", "invite_expires_at", "ALTER TABLE users ADD COLUMN invite_expires_at TEXT"),
)


def stretch(salt: str, password: str) -> str:
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    for _ in range(PW_ITERS):
        h = hashlib.sha256(h.encode()).hexdigest()
    return h


def constant_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Auth:
    """Accounts and sessions in their own database file."""

    def __init__(self, path: str | Path, admin_token: str | None, admin_user: str = "admin"):
        self.path = str(path)
        self.admin_token = admin_token or None
        with contextlib.closing(self._connect()) as conn:
            conn.executescript(AUTH_SCHEMA)
            for table, column, ddl in _AUTH_MIGRATIONS:
                cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(ddl)
            # After the migrations above, not inside AUTH_SCHEMA/inline on the
            # column: SQLite's ALTER TABLE ADD COLUMN can't add a UNIQUE
            # column to an existing table (confirmed against a real
            # pre-invite-links auth.db — it crashed here), and invite_token
            # doesn't exist yet on an upgrading database until the loop above
            # adds it, so this index can't be created any earlier either.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_invite_token ON users (invite_token)"
            )
            conn.execute("INSERT OR IGNORE INTO users (username, is_admin) VALUES (?, 1)", (admin_user,))
            # Covers the upgrade case too: an admin_user row created before
            # is_admin existed (INSERT OR IGNORE above is a no-op for it)
            # still needs to actually become an admin, not just default to 0.
            conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (admin_user,))
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # foreign_keys is a per-connection pragma, not a schema property — it
        # was never actually being turned on here, so sessions.user_id's own
        # ON DELETE CASCADE has never really been enforced. Matters now that
        # delete_user() relies on it to clean up sessions on account removal.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def create_user(self, username: str, is_admin: bool = False) -> int:
        """Register a new account with no password set — same bootstrap-token
        login path verify() already gives any passwordless row, so a fresh
        account logs in with the shared HARK_ADMIN_TOKEN once and sets its
        own password at /account, same flow the original admin account uses."""
        with contextlib.closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO users (username, is_admin) VALUES (?, ?)", (username, int(is_admin))
            )
            conn.commit()
            assert cur.lastrowid is not None  # always set after a real INSERT
            return cur.lastrowid

    def list_users(self) -> list[sqlite3.Row]:
        """invite_token is included (not just an invite_pending boolean) so a
        caller can rebuild the /invite/<token> link for a still-pending
        invite without having to create a new one — otherwise the link only
        ever existed transiently, right after creation."""
        with contextlib.closing(self._connect()) as conn:
            return conn.execute(
                "SELECT id, username, is_admin, password_hash IS NOT NULL AS has_password,"
                " invite_token, invite_token IS NOT NULL AS invite_pending, invite_expires_at,"
                " created_at FROM users ORDER BY id"
            ).fetchall()

    def delete_user(self, username: str) -> bool:
        """Returns False if no such user. Sessions cascade via the FK."""
        with contextlib.closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
            return cur.rowcount > 0

    def is_admin(self, user_id: int) -> bool:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
            return bool(row and row["is_admin"])

    def create_invite(self, username: str, is_admin: bool = False) -> tuple[int, str]:
        """Register a new account with a single-use invite token instead of
        the shared $HARK_ADMIN_TOKEN bootstrap — a link you can hand a
        specific friend, scoped to just their own account, rather than a
        master credential that also happens to work on any other
        as-yet-passwordless row. Returns (user_id, token); the caller (web
        route or CLI) builds the actual /invite/<token> URL, since Auth
        itself doesn't know the deployment's base_url."""
        token = secrets.token_urlsafe(24)
        expires = iso(utcnow() + timedelta(days=INVITE_EXPIRES_DAYS))
        with contextlib.closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO users (username, is_admin, invite_token, invite_expires_at)"
                " VALUES (?, ?, ?, ?)",
                (username, int(is_admin), token, expires),
            )
            conn.commit()
            assert cur.lastrowid is not None  # always set after a real INSERT
            return cur.lastrowid, token

    def find_by_invite_token(self, token: str) -> sqlite3.Row | None:
        """None if the token doesn't exist, already got used (password set,
        invite_token cleared by accept_invite), or expired."""
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, username, invite_expires_at FROM users WHERE invite_token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            if row["invite_expires_at"] and parse_iso(row["invite_expires_at"]) < utcnow():
                return None
            return row

    def accept_invite(self, token: str, password: str) -> int | None:
        """Sets the invited account's password and clears the invite token
        (single-use) in one step. Returns the user id on success, None if
        the token is invalid/expired — re-checked here, not just by the
        route calling find_by_invite_token() first, so this is safe to call
        on its own too."""
        user = self.find_by_invite_token(token)
        if user is None:
            return None
        self.set_password(user["id"], password)
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "UPDATE users SET invite_token = NULL, invite_expires_at = NULL WHERE id = ?",
                (user["id"],),
            )
            conn.commit()
        return user["id"]

    def verify(self, username: str, password: str) -> int | None:
        """Return user id on success. Fail-closed: an account with no stored
        password only accepts the bootstrap admin token, and if that is unset
        nothing is accepted."""
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, salt, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                stretch("timing-pad", password)  # equalise timing for unknown users
                return None
            if row["password_hash"]:
                if constant_eq(stretch(row["salt"], password), row["password_hash"]):
                    return row["id"]
                return None
            if self.admin_token and constant_eq(password, self.admin_token):
                return row["id"]
            return None

    def create_session(self, user_id: int) -> str:
        token = secrets.token_hex(32)
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, iso(utcnow() + timedelta(days=SESSION_DAYS))),
            )
            conn.commit()
        return token

    def session_user(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        with contextlib.closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT u.id, u.username, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.expires_at > ?
                """,
                (token, iso(utcnow())),
            ).fetchone()

    def drop_session(self, token: str | None) -> None:
        if not token:
            return
        with contextlib.closing(self._connect()) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()

    def set_password(self, user_id: int, password: str) -> None:
        """Set a new password and revoke every session *for this account*
        (all its devices log out). Scoped to user_id — before multi-user
        this was a bare `DELETE FROM sessions`, harmless when there was only
        ever one account's sessions to delete; left unscoped it would log
        out every other account too the moment any one of them changed a
        password, found while wiring up invite acceptance (which calls this
        right before creating the new account's own first session)."""
        salt = secrets.token_hex(16)
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "UPDATE users SET salt = ?, password_hash = ? WHERE id = ?",
                (salt, stretch(salt, password), user_id),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
