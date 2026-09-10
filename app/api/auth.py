"""
Server-side accounts and sessions.

Before this existed, "who is this request from" was a `user_id` STRING the
browser sent with every call, and nothing checked it. The login/signup pages
were browser-local only (accounts in localStorage), so the whole identity
model was advisory: anyone on the LAN could read the full audit log, list
any operator's Knowledge Base, download their documents, or delete them,
just by passing someone else's user_id. Confirmed by direct request against
a running instance, not theorised.

This module closes that. Accounts live in SQLite with PBKDF2-hashed
passwords (never plaintext, never reversible), login issues an opaque
bearer token, and `current_user` resolves that token to the user_id
server-side. Endpoints that own per-operator data take the user_id from the
TOKEN, never from the request body or query string, so a caller can only
ever act as themselves.

Deliberately still simple and dependency-free (hashlib + sqlite3, both
stdlib): the air-gapped single-node design has no identity provider to
federate with, and adding one would be a bigger change than the gap being
closed. Tokens are in-process, so they do not survive a restart — users
sign in again after a server restart, which for a single-node LAN workbench
is an acceptable trade rather than a security hole.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..storage.config import ADMIN_USER_IDS, REPO_ROOT

router = APIRouter()

# Overridable so the test suite gets its own throwaway database instead of
# writing accounts into the deployment's real one.
DB_PATH = Path(os.environ.get("ACCOUNTS_DB_PATH", REPO_ROOT / "app" / "audit" / "accounts.sqlite3"))

# PBKDF2 work factor. 240k is a reasonable 2020s-era floor for interactive
# logins and still fast enough (~0.1s) that a LAN sign-in feels instant.
_PBKDF2_ROUNDS = 240_000
_SALT_BYTES = 16

# token -> {"user_id": str, "created_at": float, "last_seen": float}
_SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 12 * 3600


def init_accounts_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id       TEXT PRIMARY KEY,
                nickname      TEXT,
                salt          BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)


def _valid_user_id(user_id: str) -> str:
    uid = (user_id or "").strip()
    # Same shape the signup form enforces client-side. Re-checked here
    # because a client-side check is a UX affordance, not a control — and
    # user_id becomes a filesystem path segment under KB_STORE_DIR.
    if not (2 <= len(uid) <= 32) or not all(c.isalnum() or c in "_.-" for c in uid):
        raise HTTPException(
            status_code=400,
            detail="user_id must be 2-32 characters: letters, numbers, '.', '_' or '-'",
        )
    return uid


class SignupRequest(BaseModel):
    user_id: str
    password: str
    nickname: Optional[str] = None


class LoginRequest(BaseModel):
    user_id: str
    password: str


def _issue_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    _SESSIONS[token] = {"user_id": user_id, "created_at": now, "last_seen": now}
    return token


@router.post("/api/auth/signup")
async def signup(req: SignupRequest):
    user_id = _valid_user_id(req.user_id)
    if len(req.password or "") < 4:
        raise HTTPException(status_code=400, detail="password must be at least 4 characters")

    salt = os.urandom(_SALT_BYTES)
    pw_hash = _hash_password(req.password, salt)

    conn = sqlite3.connect(DB_PATH)
    try:
        existing = conn.execute(
            "SELECT 1 FROM accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="that user_id is already taken")
        # Who gets the admin role:
        #   1. anyone named in config.json's "admin_user_ids", and
        #   2. the very first account on a fresh deployment, so a brand-new
        #      install always has exactly one way in.
        # Rule 2 alone was not enough: the moment ANY account exists (a demo
        # user, a test account) nobody can ever become admin again, which
        # locks the Admin panel out of its own data permanently. The config
        # list is the deliberate, re-runnable way to say who is staff.
        first_account = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None
        is_admin = 1 if (first_account or user_id in ADMIN_USER_IDS) else 0
        conn.execute(
            "INSERT INTO accounts (user_id, nickname, salt, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (user_id, (req.nickname or "").strip() or user_id, salt, pw_hash, is_admin),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "user_id": user_id,
        "nickname": (req.nickname or "").strip() or user_id,
        "is_admin": bool(is_admin),
        "token": _issue_token(user_id),
    }


@router.post("/api/auth/login")
async def login(req: LoginRequest):
    user_id = (req.user_id or "").strip()
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT nickname, salt, password_hash, is_admin FROM accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    # Same error and roughly the same work either way: revealing "no such
    # user" vs "wrong password" hands an attacker a free account enumerator.
    if row is None:
        _hash_password(req.password or "", b"\x00" * _SALT_BYTES)
        raise HTTPException(status_code=401, detail="invalid user_id or password")

    nickname, salt, stored_hash, is_admin = row
    if not hmac.compare_digest(_hash_password(req.password or "", salt), stored_hash):
        raise HTTPException(status_code=401, detail="invalid user_id or password")

    return {
        "user_id": user_id,
        "nickname": nickname or user_id,
        "is_admin": bool(is_admin),
        "token": _issue_token(user_id),
    }


@router.post("/api/auth/logout")
async def logout(authorization: Optional[str] = Header(default=None)):
    token = _bearer(authorization)
    if token:
        _SESSIONS.pop(token, None)
    return {"success": True}


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _resolve(token: Optional[str]) -> Optional[str]:
    """token -> user_id, honouring TTL. Returns None when unauthenticated."""
    if not token:
        return None
    sess = _SESSIONS.get(token)
    if sess is None:
        return None
    now = time.time()
    if now - sess["created_at"] > SESSION_TTL_SECONDS:
        _SESSIONS.pop(token, None)
        return None
    sess["last_seen"] = now
    return sess["user_id"]


async def current_user(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: the authenticated user_id, or 401.

    Every endpoint owning per-operator data depends on THIS for identity
    rather than on a user_id in the request — that substitution is the whole
    fix. A caller can no longer name someone else.
    """
    user_id = _resolve(_bearer(authorization))
    if user_id is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user_id


async def current_admin(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: the authenticated user_id, and it must be an admin."""
    user_id = await current_user(authorization)
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT is_admin FROM accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=403, detail="admin access required")
    return user_id


@router.get("/api/auth/me")
async def me(authorization: Optional[str] = Header(default=None)):
    """Lets the frontend confirm a stored token is still valid on load."""
    user_id = _resolve(_bearer(authorization))
    if user_id is None:
        raise HTTPException(status_code=401, detail="sign in required")
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT nickname, is_admin FROM accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    return {
        "user_id": user_id,
        "nickname": (row[0] if row else None) or user_id,
        "is_admin": bool(row[1]) if row else False,
    }
