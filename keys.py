# -*- coding: utf-8 -*-
"""用户 API Key 加密存储（AES-GCM）

- 每个用户的 key 用服务端主密钥加密后存 user_keys 表，明文不落库、不回前端、不进日志
- 主密钥：环境变量 WEBREADER_MASTER_KEY（base64 32 字节），否则 data/secret.key（首次自动生成，0600）
- 前端只能拿到 has_key / 掩码（sk-****3f9a）

表：
  user_keys(user_id, provider, cipher, updated, PRIMARY KEY(user_id, provider))
"""
import base64
import os
import sqlite3
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB = paths.DB
MASTER_FILE = paths.SECRET_KEY

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_keys(
  user_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  cipher TEXT NOT NULL,
  updated REAL,
  PRIMARY KEY(user_id, provider)
);
"""


def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


def master_key() -> bytes:
    env = (os.environ.get("WEBREADER_MASTER_KEY") or "").strip()
    if env:
        raw = base64.b64decode(env)
        if len(raw) != 32:
            raise RuntimeError("WEBREADER_MASTER_KEY 必须是 base64 编码的 32 字节")
        return raw
    try:
        with open(MASTER_FILE, "rb") as f:
            raw = f.read()
        if len(raw) == 32:
            return raw
    except OSError:
        pass
    raw = os.urandom(32)
    os.makedirs(os.path.dirname(MASTER_FILE), exist_ok=True)
    fd = os.open(MASTER_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return raw


def encrypt(plain: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(master_key()).encrypt(nonce, plain.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(blob: str) -> str:
    raw = base64.b64decode(blob)
    return AESGCM(master_key()).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def set_key(uid: int, provider: str, plain: str):
    plain = (plain or "").strip()
    if not plain:
        raise ValueError("Key 不能为空")
    with conn() as c:
        c.execute(
            "INSERT INTO user_keys(user_id,provider,cipher,updated) VALUES(?,?,?,?)"
            " ON CONFLICT(user_id,provider) DO UPDATE SET cipher=excluded.cipher,"
            " updated=excluded.updated",
            (int(uid), provider, encrypt(plain), time.time()),
        )


def get_key(uid: int, provider: str) -> str:
    with conn() as c:
        r = c.execute(
            "SELECT cipher FROM user_keys WHERE user_id=? AND provider=?",
            (int(uid), provider),
        ).fetchone()
    if not r:
        return ""
    try:
        return decrypt(r["cipher"])
    except Exception:
        return ""


def has_key(uid: int, provider: str) -> bool:
    with conn() as c:
        r = c.execute(
            "SELECT 1 FROM user_keys WHERE user_id=? AND provider=?", (int(uid), provider)
        ).fetchone()
    return bool(r)


def delete_key(uid: int, provider: str) -> int:
    with conn() as c:
        cur = c.execute(
            "DELETE FROM user_keys WHERE user_id=? AND provider=?", (int(uid), provider)
        )
        return cur.rowcount or 0


def mask(plain: str) -> str:
    """sk-abcd...wxyz → 只露头 3 尾 4，中间固定星号。"""
    s = (plain or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return s[:3] + "****" + s[-4:]


def masked_for(uid: int, provider: str) -> str:
    k = get_key(uid, provider)
    return mask(k) if k else ""
