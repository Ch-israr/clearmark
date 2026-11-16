"""SQLite storage for ClearMark: users, credits and operations.

All access goes through this module. A single process-wide lock guards
writes, because the Flask request threads and the background worker thread
share one database file. Every helper opens its own short-lived
connection, so no connection is ever shared across threads.
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 0,
    free_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    funding_state TEXT NOT NULL,
    used_trial INTEGER NOT NULL DEFAULT 0,
    media TEXT NOT NULL,
    filename TEXT NOT NULL,
    cost INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'standard',
    upload_path TEXT,
    result_path TEXT,
    result_name TEXT,
    idempotency_key TEXT,
    details TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    expires_at REAL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE INDEX IF NOT EXISTS idx_operations_user ON operations (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations (status, created_at);
"""


def connect(db_path):
    """Open a new SQLite connection for the calling thread."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path):
    """Create the database file and schema if they do not exist yet."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()


def _migrate(conn):
    """Add columns that older databases predate. SQLite has no
    ADD COLUMN IF NOT EXISTS, so check the table first."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(operations)")}
    if "mode" not in cols:
        conn.execute(
            "ALTER TABLE operations ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'"
        )


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------- users

def create_user(db_path, email, password_hash):
    user_id = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, credits, free_used, created_at)"
                " VALUES (?, ?, ?, 0, 0, ?)",
                (user_id, email, password_hash, now),
            )
            conn.commit()
        finally:
            conn.close()
    return get_user_by_id(db_path, user_id)


def get_user_by_email(db_path, email):
    with _LOCK:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def get_user_by_id(db_path, user_id):
    with _LOCK:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def adjust_credits(db_path, user_id, delta):
    """Add delta credits (negative to spend) and return the new balance."""
    with _LOCK:
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE users SET credits = credits + ? WHERE id = ?",
                (delta, user_id),
            )
            row = conn.execute(
                "SELECT credits FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    return row["credits"]


def mark_free_used(db_path, user_id):
    with _LOCK:
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE users SET free_used = 1 WHERE id = ?", (user_id,)
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- operations

def create_operation(db_path, user_id, media, filename, cost, used_trial,
                     upload_path, idempotency_key, expires_at, mode="standard"):
    op_id = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO operations (id, user_id, status, funding_state,"
                " used_trial, media, filename, cost, mode, upload_path,"
                " idempotency_key, created_at, expires_at)"
                " VALUES (?, ?, 'uploaded', 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (op_id, user_id, int(used_trial), media, filename, cost,
                 mode, str(upload_path), idempotency_key, now, expires_at),
            )
            conn.commit()
        finally:
            conn.close()
    return get_operation(db_path, op_id)


def get_operation(db_path, op_id):
    with _LOCK:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (op_id,)
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def get_operation_by_upload(db_path, user_id, idempotency_key):
    """Find the operation created for one upload (idempotency support)."""
    if not idempotency_key:
        return None
    with _LOCK:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE user_id = ? AND idempotency_key = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (user_id, idempotency_key),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def list_operations(db_path, user_id, limit=20, before=None):
    with _LOCK:
        conn = connect(db_path)
        try:
            if before:
                rows = conn.execute(
                    "SELECT * FROM operations WHERE user_id = ? AND created_at < ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (user_id, before, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM operations WHERE user_id = ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(r) for r in rows]


def update_operation(db_path, op_id, **fields):
    """Update the named columns of one operation. Values must be scalars
    (details is stored as a JSON string by the caller)."""
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [op_id]
    with _LOCK:
        conn = connect(db_path)
        try:
            conn.execute(
                f"UPDATE operations SET {assignments} WHERE id = ?", values
            )
            conn.commit()
        finally:
            conn.close()


def claim_next_queued(db_path):
    """Atomically move the oldest queued operation to running and return it,
    or return None when the queue is empty."""
    with _LOCK:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE status = 'queued'"
                " ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            op_id = row["id"]
            cursor = conn.execute(
                "UPDATE operations SET status = 'running', started_at = ?"
                " WHERE id = ? AND status = 'queued'",
                (time.time(), op_id),
            )
            conn.commit()
            if cursor.rowcount != 1:
                return None
        finally:
            conn.close()
    return get_operation(db_path, op_id)


def fail_stale_running(db_path):
    """On startup, fail any operation left running by a crashed worker and
    return them so the caller can refund their reserved credits."""
    with _LOCK:
        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM operations WHERE status = 'running'"
            ).fetchall()
            conn.execute(
                "UPDATE operations SET status = 'failed',"
                " error_code = 'INTERRUPTED',"
                " error_message = 'The server restarted while this file was"
                " being processed. Your credits were refunded.'"
                " WHERE status = 'running'"
            )
            conn.commit()
        finally:
            conn.close()
    return [_row_to_dict(r) for r in rows]


def expired_operations(db_path, now=None):
    """Operations whose files are past their expiry time."""
    now = time.time() if now is None else now
    with _LOCK:
        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM operations WHERE expires_at IS NOT NULL"
                " AND expires_at < ?"
                " AND (upload_path IS NOT NULL OR result_path IS NOT NULL)",
                (now,),
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(r) for r in rows]


def details_to_json(details):
    if details is None:
        return None
    return json.dumps(details)


def details_from_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
