# -*- coding: utf-8 -*-
"""账号与会话：注册 / 登录 / token 校验 / 限流

设计要点
  - 密码：hashlib.scrypt(n=2^14, r=8, p=1) + 每用户 16 字节随机 salt
  - 会话：随机 32 字节 token 明文只返回给客户端，库里只存 sha256(token)
  - 双通道：Web 用 HttpOnly Cookie，App 用 Authorization: Bearer <token>
  - 限流：同一 IP 每分钟最多 5 次登录/注册尝试（进程内计数，单机够用）
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("WEBREADER_DB") or os.path.join(HERE, "data", "reader.db")

COOKIE_NAME = "wr_token"
TOKEN_TTL = 30 * 24 * 3600          # 30 天
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PW, MAX_PW = 8, 128
USER_RE = re.compile(r"^[\w\u4e00-\u9fa5.@-]{2,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pass_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  created REAL NOT NULL,
  last_login REAL
);
CREATE TABLE IF NOT EXISTS tokens(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created REAL NOT NULL,
  expires REAL NOT NULL,
  device TEXT DEFAULT '',
  ip TEXT DEFAULT '',
  last_used REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
"""


class AuthError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg = msg
        self.status = status


def conn():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


# ---------------- 密码 ----------------
def _hash(pw: str, salt_hex: str) -> str:
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return dk.hex()


def create_user(username: str, password: str):
    u = (username or "").strip()
    if not USER_RE.match(u):
        raise AuthError("用户名 2-32 位，可用中文/字母/数字/._@-")
    if not (MIN_PW <= len(password or "") <= MAX_PW):
        raise AuthError("密码长度需 8-128 位")
    salt = secrets.token_hex(16)
    ph = _hash(password, salt)
    now = time.time()
    with conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, pass_hash, salt, created) VALUES(?,?,?,?)",
                (u, ph, salt, now))
        except sqlite3.IntegrityError:
            raise AuthError("用户名已被注册", 409)
        return {"id": cur.lastrowid, "username": u}


def verify_user(username: str, password: str):
    """校验成功返回用户 dict，失败返回 None（耗时对齐，避免用户名枚举）。"""
    u = (username or "").strip()
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
    if not row:
        _hash(password or "x", "00" * 16)   # 假哈希，抹平时间差
        return None
    if not hmac.compare_digest(_hash(password or "", row["salt"]), row["pass_hash"]):
        return None
    with conn() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), row["id"]))
    return {"id": row["id"], "username": row["username"]}


# ---------------- 会话 ----------------
def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(user_id: int, device="", ip="") -> str:
    raw = secrets.token_urlsafe(32)
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO tokens(token_hash, user_id, created, expires, device, ip, last_used)"
            " VALUES(?,?,?,?,?,?,?)",
            (_h(raw), user_id, now, now + TOKEN_TTL, (device or "")[:120], (ip or "")[:64], now))
    return raw


def user_by_token(raw: str):
    if not raw:
        return None
    th = _h(raw)
    with conn() as c:
        row = c.execute(
            "SELECT u.id, u.username, t.expires, t.last_used FROM tokens t"
            " JOIN users u ON u.id = t.user_id WHERE t.token_hash=?", (th,)).fetchone()
        if not row:
            return None
        if row["expires"] < time.time():
            c.execute("DELETE FROM tokens WHERE token_hash=?", (th,))
            return None
        if not row["last_used"] or time.time() - row["last_used"] > 300:
            c.execute("UPDATE tokens SET last_used=? WHERE token_hash=?", (time.time(), th))
        return {"id": row["id"], "username": row["username"]}


def revoke(raw: str):
    if not raw:
        return
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE token_hash=?", (_h(raw),))


def revoke_all(user_id: int):
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))


def purge_expired():
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE expires < ?", (time.time(),))


def count_users() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def first_user():
    with conn() as c:
        row = c.execute("SELECT id, username FROM users ORDER BY id LIMIT 1").fetchone()
        return {"id": row["id"], "username": row["username"]} if row else None


# ---------------- 限流 ----------------
_LOCK = threading.Lock()
_HITS = {}
LIMIT_N, LIMIT_WIN = 5, 60.0


def rate_ok(key: str) -> bool:
    """同一 key（IP）在 60 秒窗口内最多 5 次；返回 False 表示超限。"""
    now = time.time()
    with _LOCK:
        q = _HITS.setdefault(key, deque())
        while q and now - q[0] > LIMIT_WIN:
            q.popleft()
        if len(q) >= LIMIT_N:
            return False
        q.append(now)
        return True


def client_ip(request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return xff or (request.client.host if request.client else "")


def device_of(request) -> str:
    return (request.headers.get("user-agent") or "")[:120]
