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

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB = paths.DB

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
        return {"id": row["id"], "username": row["username"], "expires": row["expires"]}


def session_row(raw: str):
    """按明文 token 查会话行；不存在或已过期返回 None（顺手清掉过期行）。"""
    if not raw:
        return None
    th = _h(raw)
    with conn() as c:
        row = c.execute("SELECT * FROM tokens WHERE token_hash=?", (th,)).fetchone()
        if not row:
            return None
        if row["expires"] < time.time():
            c.execute("DELETE FROM tokens WHERE token_hash=?", (th,))
            return None
        return dict(row)


def refresh_session(raw: str, device="", ip=""):
    """换发 token：旧 token 立即失效，新 token 重新计 30 天。

    App 每次启动拿旧 token 换新的，就不用每 30 天让用户重登一次。
    """
    row = session_row(raw)
    if not row:
        return None
    new_raw = create_session(row["user_id"], device or row["device"], ip or row["ip"])
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE token_hash=?", (row["token_hash"],))
    return new_raw


def list_sessions(user_id: int, current_raw: str = ""):
    """列出该用户所有未过期会话；sid 用 token_hash 前 12 位，不泄露 token 本身。"""
    cur = _h(current_raw) if current_raw else ""
    now = time.time()
    out = []
    with conn() as c:
        rows = c.execute(
            "SELECT token_hash,device,ip,created,expires,last_used FROM tokens"
            " WHERE user_id=? AND expires>? ORDER BY created DESC", (int(user_id), now))
        for r in rows:
            out.append({
                "sid": r["token_hash"][:12],
                "device": r["device"] or "",
                "ip": r["ip"] or "",
                "created": r["created"],
                "expires": r["expires"],
                "last_used": r["last_used"],
                "current": r["token_hash"] == cur,
            })
    return out


def revoke_sid(user_id: int, sid: str) -> int:
    """按 sid（token_hash 前 12 位）删自己的会话；sid 短于 8 位不认，避免误删。"""
    sid = (sid or "").strip().lower()
    if len(sid) < 8 or len(sid) > 64:
        return 0
    with conn() as c:
        cur = c.execute("DELETE FROM tokens WHERE user_id=? AND token_hash LIKE ?",
                        (int(user_id), sid + "%"))
        return cur.rowcount


def revoke_others(user_id: int, keep_raw: str) -> int:
    """踢掉除当前设备外的所有会话。"""
    keep = _h(keep_raw) if keep_raw else ""
    with conn() as c:
        cur = c.execute("DELETE FROM tokens WHERE user_id=? AND token_hash<>?",
                        (int(user_id), keep))
        return cur.rowcount


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


def rate_ok(key: str, n: int = LIMIT_N, win: float = LIMIT_WIN) -> bool:
    """同一 key（IP）在 win 秒窗口内最多 n 次；返回 False 表示超限。"""
    now = time.time()
    with _LOCK:
        q = _HITS.setdefault(key, deque())
        while q and now - q[0] > win:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(now)
        return True


def client_ip(request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return xff or (request.client.host if request.client else "")


def device_of(request) -> str:
    return (request.headers.get("user-agent") or "")[:120]
