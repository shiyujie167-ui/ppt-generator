"""SQLite 存储层:账号 / 登录会话 / 材料库元数据(jobs 表由 jobs.py 自建)。

多账号就绪、单账号在用:所有数据从建表起就挂 user_id,开放多人时只需
加账号(migrate_v2.py adduser),无需改表迁移。文件本体(材料/成品)仍在
磁盘,库里只存元数据与归属。

单进程多线程模型:共享连接 + RLock 串行化(流量极小,足够);WAL 模式。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

import config

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  sha256 TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_materials_user ON materials(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        conn = sqlite3.connect(str(config.DB_FILE), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _CONN = conn
    return _CONN


def init() -> None:
    _connect()


def run(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    with _LOCK:
        conn = _connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def fetchall(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with _LOCK:
        return _connect().execute(sql, params).fetchall()


def fetchone(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    with _LOCK:
        return _connect().execute(sql, params).fetchone()


# ── 密码(标准库 scrypt,无额外依赖)──────────────────────────────


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode("utf-8"),
                                salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# ── 账号 ─────────────────────────────────────────────────────────


def create_user(username: str, password: str, role: str = "user") -> int:
    cur = run("INSERT INTO users (username, password_hash, role, disabled, created_at) VALUES (?, ?, ?, 0, ?)",
              (username, _hash_password(password), role, time.time()))
    return int(cur.lastrowid)


def get_user(username: str) -> sqlite3.Row | None:
    return fetchone("SELECT * FROM users WHERE username = ?", (username,))


def count_users() -> int:
    row = fetchone("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


def verify_login(username: str, password: str) -> sqlite3.Row | None:
    user = get_user(username)
    if user is None or user["disabled"] or not _verify_password(password, user["password_hash"]):
        return None
    return user


def set_disabled(username: str, disabled: bool) -> bool:
    cur = run("UPDATE users SET disabled = ? WHERE username = ?", (1 if disabled else 0, username))
    if disabled:  # 禁用即踢下线
        user = get_user(username)
        if user is not None:
            run("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    return cur.rowcount > 0


def set_password(username: str, password: str) -> bool:
    cur = run("UPDATE users SET password_hash = ? WHERE username = ?",
              (_hash_password(password), username))
    if cur.rowcount:
        user = get_user(username)
        if user is not None:
            run("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    return cur.rowcount > 0


# ── 登录会话(随机 token 落库,重启不掉线)──────────────────────


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    run("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now, now + config.SESSION_TTL_DAYS * 86400))
    run("DELETE FROM sessions WHERE expires_at < ?", (now,))  # 顺手清过期
    return token


def user_for_token(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    row = fetchone(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ? AND u.disabled = 0",
        (token, time.time()))
    return row


def delete_session(token: str) -> None:
    if token:
        run("DELETE FROM sessions WHERE token = ?", (token,))
