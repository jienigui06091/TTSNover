# -*- coding: utf-8 -*-
"""首账号认领历史数据

多用户改造前，所有数据 user_id=0（未归属）。第一个注册成功的账号认领这些数据：
  - books / sources / settings 里 user_id=0 的行改成该账号
  - data/tts/*.bin 搬进 data/tts/<uid>/
  - data/mimo_clones/* 搬进 data/mimo_clones/<uid>/
  - 旧全局 data/mimo.key 加密存进 user_keys 后改名归档（不删除，留证据）

只做数据归属，不给任何管理员权限；之后的账号一律从空数据开始。
"""
import os
import shutil

import keys
import paths
import tts

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB = paths.DB
MIMO_KEY_FILE = paths.LEGACY_MIMO_KEY

import sqlite3


def conn():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def _move_files_into(src_dir, uid):
    """把 src_dir 顶层的散文件搬进 src_dir/<uid>/，返回搬了几个。子目录不动。"""
    if not os.path.isdir(src_dir):
        return 0
    dst = os.path.join(src_dir, str(int(uid)))
    os.makedirs(dst, exist_ok=True)
    n = 0
    for name in os.listdir(src_dir):
        p = os.path.join(src_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            shutil.move(p, os.path.join(dst, name))
            n += 1
        except OSError:
            pass
    return n


def claim_legacy(uid):
    uid = int(uid)
    out = {"books": 0, "sources": 0, "settings": 0, "tts_files": 0,
           "clone_files": 0, "mimo_key": False}
    with conn() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("books", "sources", "settings"):
            if t not in tables:
                continue
            cols = {r[1] for r in c.execute("PRAGMA table_info(%s)" % t)}
            if "user_id" not in cols:
                continue
            out[t] = c.execute("UPDATE %s SET user_id=? WHERE user_id=0" % t, (uid,)).rowcount
        c.commit()

    out["tts_files"] = _move_files_into(tts.CACHE_DIR, uid)
    out["clone_files"] = _move_files_into(tts.MIMO_CLONE_DIR, uid)

    # 旧全局小米 key → 该用户的加密 key（原文件改名归档，不留在明文位置）
    if os.path.isfile(MIMO_KEY_FILE):
        try:
            with open(MIMO_KEY_FILE, "r", encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                keys.set_key(uid, "mimo", k)
                out["mimo_key"] = True
            os.rename(MIMO_KEY_FILE, MIMO_KEY_FILE + ".migrated")
        except OSError:
            pass
    return out
