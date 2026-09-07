"""
Hash-chained audit log — SQLite, unchanged logic from the pre-refactor
backend/main.py. Moved here as its own module (was inlined in the old
single backend service file) purely for the "modular internal components"
requirement — the audit_log.sqlite3 file, its schema, and the hash-chaining
algorithm are byte-for-byte identical to before.
"""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..storage.config import REPO_ROOT

DB_PATH = REPO_ROOT / "app" / "audit" / "audit_log.sqlite3"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            model_used TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            file_uploaded INTEGER NOT NULL,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        )
        """
    )
    # Additive columns for the admin "who's on what IP, using how many
    # tokens" view — added via migration (not in the CREATE TABLE above) so
    # a pre-existing audit_log.sqlite3 from before this change still opens
    # fine. Deliberately NOT part of the hash-chain payload below: the
    # tamper-evident chain covers the original compliance fields only, so
    # adding these columns doesn't retroactively invalidate any existing
    # entry's hash.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    for col, decl in (
        ("client_ip", "TEXT"),
        ("prompt_tokens", "INTEGER"),
        ("completion_tokens", "INTEGER"),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col} {decl}")
    conn.commit()
    conn.close()


def _last_hash(conn) -> str:
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else "0" * 64


def write_audit_entry(task_id: str, user_id: str, task_type: str, model_used: str,
                       file_uploaded: bool, client_ip: str | None = None,
                       prompt_tokens: int | None = None, completion_tokens: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    prev_hash = _last_hash(conn)
    timestamp = now_iso()
    # Hash payload unchanged from before — client_ip/tokens are informational
    # (admin visibility), not part of the tamper-evident compliance chain.
    payload = f"{task_id}|{user_id}|{task_type}|{model_used}|{timestamp}|{file_uploaded}|{prev_hash}"
    entry_hash = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        """INSERT INTO audit_log
           (task_id, user_id, task_type, model_used, timestamp, file_uploaded, prev_hash, entry_hash,
            client_ip, prompt_tokens, completion_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, user_id, task_type, model_used, timestamp, int(file_uploaded), prev_hash, entry_hash,
         client_ip, prompt_tokens, completion_tokens),
    )
    conn.commit()
    conn.close()


def read_audit_entries() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT task_id, user_id, task_type, model_used, timestamp, file_uploaded, "
        "client_ip, prompt_tokens, completion_tokens FROM audit_log ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [
        {
            "task_id": r[0],
            "user_id": r[1],
            "task_type": r[2],
            "model_used": r[3],
            "timestamp": r[4],
            "file_uploaded": bool(r[5]),
            "client_ip": r[6],
            "prompt_tokens": r[7],
            "completion_tokens": r[8],
        }
        for r in rows
    ]
