#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多用户改造 P2：把单用户库迁移成按 user_id 隔离的结构

用法：
    python3 tools/migrate_multiuser.py [--db PATH] [--dry-run]

做的事（单个事务内，失败整体回滚）：
  1. 备份 data/reader.db → data/reader.db.bak-<时间戳>
  2. books 加 user_id 列（默认 0 = 待认领）
  3. settings 改成 (user_id, k) 复合主键
  4. sources 重建为 (user_id, id) 复合主键（同一书源可被多个用户各自导入）
  5. 建索引
老数据的 user_id 保持 0；第一个注册的账号注册时会自动认领（auth.claim_legacy）。
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "data", "reader.db")


def table_info(c, t):
    return {r[1]: r for r in c.execute("PRAGMA table_info(%s)" % t)}


def has_table(c, t):
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def migrate(db_path, dry_run=False):
    if not os.path.exists(db_path):
        print("库不存在：%s" % db_path)
        return 1
    bak = db_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    print("库：%s（%.2f MB）" % (db_path, os.path.getsize(db_path) / 1048576))

    if dry_run:
        c = sqlite3.connect(db_path)
        print("[dry-run] books 列：", list(table_info(c, "books")) if has_table(c, "books") else "无")
        print("[dry-run] settings 列：", list(table_info(c, "settings")) if has_table(c, "settings") else "无")
        print("[dry-run] sources 列：", list(table_info(c, "sources")) if has_table(c, "sources") else "无")
        for t in ("books", "chapters", "settings", "sources"):
            if has_table(c, t):
                print("[dry-run] %-8s %d 行" % (t, c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))
        c.close()
        return 0

    shutil.copy2(db_path, bak)
    print("已备份 → %s" % os.path.basename(bak))

    c = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    c.execute("PRAGMA foreign_keys=OFF")
    try:
        c.execute("BEGIN IMMEDIATE")

        # 1) books.user_id
        cols = table_info(c, "books")
        if "user_id" not in cols:
            c.execute("ALTER TABLE books ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
            print("books += user_id")
        else:
            print("books.user_id 已存在，跳过")

        # 2) settings → (user_id, k)
        scols = table_info(c, "settings")
        if "user_id" not in scols:
            c.execute("ALTER TABLE settings RENAME TO settings_old")
            c.execute("CREATE TABLE settings(user_id INTEGER NOT NULL DEFAULT 0,"
                      " k TEXT NOT NULL, v TEXT, PRIMARY KEY(user_id, k))")
            c.execute("INSERT INTO settings(user_id, k, v) SELECT 0, k, v FROM settings_old")
            n = c.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
            c.execute("DROP TABLE settings_old")
            print("settings 重建为 (user_id,k) 复合主键，迁移 %d 行" % n)
        else:
            print("settings.user_id 已存在，跳过")

        # 3) sources → (user_id, id)
        src_cols = table_info(c, "sources")
        if src_cols and "user_id" not in src_cols:
            c.execute("ALTER TABLE sources RENAME TO sources_old")
            c.execute("""CREATE TABLE sources(
              user_id INTEGER NOT NULL DEFAULT 0,
              id TEXT NOT NULL,
              name TEXT NOT NULL,
              url TEXT DEFAULT '',
              grp TEXT DEFAULT '',
              enabled INTEGER DEFAULT 1,
              raw TEXT NOT NULL,
              added REAL,
              PRIMARY KEY(user_id, id)
            )""")
            c.execute("""INSERT INTO sources(user_id,id,name,url,grp,enabled,raw,added)
                         SELECT 0,id,name,url,grp,enabled,raw,added FROM sources_old""")
            n = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            c.execute("DROP TABLE sources_old")
            print("sources 重建为 (user_id,id) 复合主键，迁移 %d 行" % n)
        elif not src_cols:
            print("sources 表不存在（新库），跳过")
        else:
            print("sources.user_id 已存在，跳过")

        # 4) 索引
        c.execute("CREATE INDEX IF NOT EXISTS ix_books_user ON books(user_id, opened DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sources_user ON sources(user_id, grp, name)")

        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        c.close()
        print("迁移失败，已回滚；原库未被修改（备份仍在 %s）" % os.path.basename(bak))
        raise

    # 校验
    print("\n迁移后：")
    for t in ("books", "chapters", "settings", "sources"):
        if has_table(c, t):
            print("  %-8s %d 行" % (t, c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))
    if has_table(c, "sources"):
        print("  sources 待认领(user_id=0)：%d"
              % c.execute("SELECT COUNT(*) FROM sources WHERE user_id=0").fetchone()[0])
    c.close()
    print("完成。第一个注册的账号会自动认领这些数据。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("WEBREADER_DB") or DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(migrate(a.db, a.dry_run))


if __name__ == "__main__":
    main()
