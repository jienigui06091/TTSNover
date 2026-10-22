#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书源（Legado bookSource）支持：导入 / 管理 / 调用 Node 规则引擎 / 加入书架

数据表
  sources(id, name, url, grp, enabled, raw, added)
  books 追加：cover, src_id, src_name, src_url
  chapters 追加：url, fetched（0=只有链接，正文按需抓取）
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("WEBREADER_DB") or os.path.join(HERE, "data", "reader.db")
CLI = os.path.join(HERE, "srce", "cli.js")
NODE = "node"
CACHE = os.path.join(HERE, "data", "srccache")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT DEFAULT '',
  grp TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  raw TEXT NOT NULL,
  added REAL
);
"""


def conn():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # 增量加列（老库升级）
        for tbl, col, ddl in (
            ("books", "cover", "TEXT DEFAULT ''"),
            ("books", "src_id", "TEXT DEFAULT ''"),
            ("books", "src_name", "TEXT DEFAULT ''"),
            ("books", "src_url", "TEXT DEFAULT ''"),
            ("chapters", "url", "TEXT DEFAULT ''"),
            ("chapters", "fetched", "INTEGER DEFAULT 1"),
        ):
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if col not in have:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")


def _sid(src):
    key = (src.get("bookSourceUrl") or "") + "|" + (src.get("bookSourceName") or "")
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def import_sources(raw_bytes):
    """导入 Legado 分享文件（数组或单个对象）"""
    data = json.loads(raw_bytes.decode("utf-8-sig"))
    if isinstance(data, dict):
        data = [data]
    added = updated = 0
    now = time.time()
    with conn() as c:
        for src in data:
            if not isinstance(src, dict) or not src.get("bookSourceUrl"):
                continue
            sid = _sid(src)
            exists = c.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone()
            c.execute(
                """INSERT INTO sources(id,name,url,grp,enabled,raw,added) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url,
                     grp=excluded.grp, raw=excluded.raw""",
                (
                    sid,
                    src.get("bookSourceName") or "未命名",
                    src.get("bookSourceUrl") or "",
                    src.get("bookSourceGroup") or "",
                    1 if src.get("enabled", True) else 0,
                    json.dumps(src, ensure_ascii=False),
                    now,
                ),
            )
            if exists:
                updated += 1
            else:
                added += 1
    return added, updated


def export_sources(only_enabled=False):
    """导出为 Legado 分享格式（尽量还原原始 JSON，enabled 用库里的当前状态）"""
    sql = "SELECT name,url,grp,enabled,raw FROM sources"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY grp, name"
    out = []
    with conn() as c:
        for r in c.execute(sql):
            try:
                src = json.loads(r["raw"] or "{}")
            except Exception:
                src = {}
            if not isinstance(src, dict):
                src = {}
            src.setdefault("bookSourceName", r["name"])
            src.setdefault("bookSourceUrl", r["url"])
            if not src.get("bookSourceName"):
                src["bookSourceName"] = r["name"]
            if not src.get("bookSourceUrl"):
                src["bookSourceUrl"] = r["url"]
            if r["grp"]:
                src["bookSourceGroup"] = r["grp"]
            src.setdefault("bookSourceType", 0)
            src["enabled"] = bool(r["enabled"])
            src["enabledExplore"] = bool(src.get("enabledExplore", False))
            out.append(src)
    return out


def list_sources(only_enabled=False):
    sql = "SELECT id,name,url,grp,enabled FROM sources"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY grp, name"
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql)]
    # 按分组统计
    return rows


def get_source(sid):
    with conn() as c:
        r = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["json"] = json.loads(d.pop("raw"))
    return d


def toggle_source(sid, enabled=None):
    with conn() as c:
        if enabled is None:
            c.execute("UPDATE sources SET enabled=1-enabled WHERE id=?", (sid,))
        else:
            c.execute("UPDATE sources SET enabled=? WHERE id=?", (1 if enabled else 0, sid))
        r = c.execute("SELECT enabled FROM sources WHERE id=?", (sid,)).fetchone()
    return bool(r and r["enabled"])


def delete_source(sid):
    with conn() as c:
        cur = c.execute("DELETE FROM sources WHERE id=?", (sid,))
        return cur.rowcount or 0


def call(job, timeout=120):
    """调 Node 引擎，stdin 传 JSON，stdout 收 JSON"""
    p = subprocess.run(
        [NODE, CLI],
        input=json.dumps(job, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=os.path.dirname(CLI),
    )
    out = p.stdout.decode("utf-8", "replace").strip()
    if not out:
        raise RuntimeError("引擎无输出：" + p.stderr.decode("utf-8", "replace")[-300:])
    return json.loads(out)


# ---------------- 搜索 ----------------
def search(key, source_ids=None, page=1, limit=None, timeout=90):
    """并发搜索多个源；返回 (结果列表, 统计)"""
    srcs = []
    for s in list_sources(only_enabled=True):
        if source_ids and s["id"] not in source_ids:
            continue
        full = get_source(s["id"])
        if full:
            srcs.append(full["json"])
    if limit:
        srcs = srcs[:limit]
    if not srcs:
        return [], {"sources": 0, "ok": 0, "books": 0}
    res = call({"cmd": "search", "key": key, "sources": srcs, "page": page}, timeout=timeout)
    out, ok, total = [], 0, 0
    for r in res.get("results", []):
        books = r.get("books") or []
        if books:
            ok += 1
            total += len(books)
        out.append(
            {
                "source": r.get("source"),
                "error": r.get("error", ""),
                "books": [
                    {
                        "name": b.get("name", ""),
                        "author": b.get("author", ""),
                        "kind": b.get("kind", ""),
                        "intro": b.get("intro", ""),
                        "cover": b.get("coverUrl") or b.get("cover", ""),
                        "url": b.get("bookUrl") or b.get("url", ""),
                        "lastChapter": b.get("lastChapter", ""),
                    }
                    for b in books
                ],
            }
        )
    out.sort(key=lambda x: (-len(x["books"]), x["source"]))
    return out, {"sources": len(srcs), "ok": ok, "books": total}


# ---------------- 详情 / 目录 / 正文 ----------------
def book_info(sid, book_url):
    s = get_source(sid)
    if not s:
        return {"error": "书源不存在"}
    return call({"cmd": "info", "source": s["json"], "bookUrl": book_url})


def book_toc(sid, book_url, toc_url=""):
    s = get_source(sid)
    if not s:
        return {"error": "书源不存在"}
    return call({"cmd": "toc", "source": s["json"], "bookUrl": book_url, "tocUrl": toc_url})


def chapter_text(sid, chapter_url, book_url=""):
    s = get_source(sid)
    if not s:
        return {"error": "书源不存在"}
    return call({"cmd": "content", "source": s["json"], "chapterUrl": chapter_url, "bookUrl": book_url})


def test_source(sid, key="剑来"):
    s = get_source(sid)
    if not s:
        return {"ok": False, "error": "书源不存在"}
    return call({"cmd": "test", "source": s["json"], "key": key}, timeout=45)


# ---------------- 加入书架 ----------------
def add_book(sid, book, toc_url="", chapters=None):
    """把搜索结果 + 目录写进书架；正文按需抓取"""
    import booklib

    s = get_source(sid)
    if not s:
        raise ValueError("书源不存在")
    if not chapters:
        t = book_toc(sid, book["url"], toc_url)
        if t.get("error"):
            raise ValueError("取目录失败：" + str(t["error"]))
        chapters = t.get("chapters") or []
    if not chapters:
        raise ValueError("目录为空")
    with conn() as c:
        now = time.time()
        # 同一源同一本书重复加入 → 覆盖旧的（避免书架出现两本一样的）
        old = c.execute(
            "SELECT id FROM books WHERE src_id=? AND src_url=? AND title=?",
            (sid, book.get("url") or book.get("bookUrl") or "", (book.get("name") or "")[:120]),
        ).fetchone()
        if old:
            c.execute("DELETE FROM chapters WHERE book_id=?", (old["id"],))
            c.execute("DELETE FROM books WHERE id=?", (old["id"],))
        cur = c.execute(
            """INSERT INTO books(title,author,fmt,chars,created,opened,seed,cover,src_id,src_name,src_url)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (book.get("name") or "未命名")[:120],
                book.get("author") or "",
                "src",
                0,
                now,
                now,
                abs(hash(book.get("name") or "")) % 997,
                book.get("cover") or "",
                sid,
                s["name"],
                book.get("url") or book.get("bookUrl") or "", 
            ),
        )
        bid = cur.lastrowid
        c.executemany(
            "INSERT INTO chapters(book_id,idx,title,content,url,fetched) VALUES(?,?,?,?,?,0)",
            [(bid, i, ch.get("name") or f"第{i+1}章", "", ch.get("url") or "") for i, ch in enumerate(chapters)],
        )
    return bid, len(chapters)


def ensure_chapter(bid, idx):
    """章节正文没抓过就抓一次并落库"""
    with conn() as c:
        r = c.execute(
            "SELECT ch.title,ch.content,ch.url,ch.fetched,b.src_id,b.src_url,b.title AS btitle FROM chapters ch "
            "JOIN books b ON b.id=ch.book_id WHERE ch.book_id=? AND ch.idx=?",
            (bid, idx),
        ).fetchone()
        if not r:
            return None
        if r["fetched"] and (r["content"] or "").strip():
            return {"title": r["title"], "content": r["content"]}
        if not r["url"] or not r["src_id"]:
            return {"title": r["title"], "content": r["content"] or ""}
        sid, ch_url, book_url = r["src_id"], r["url"], r["src_url"]
        title = r["title"]
    res = chapter_text(sid, ch_url, book_url)
    if res.get("error") or not (res.get("content") or "").strip():
        return {"title": title, "content": "", "error": res.get("error") or "正文为空"}
    text = res["content"].strip()
    with conn() as c:
        c.execute(
            "UPDATE chapters SET content=?,fetched=1 WHERE book_id=? AND idx=?", (text, bid, idx)
        )
        c.execute("UPDATE books SET chars=chars+? WHERE id=?", (len(text), bid))
    return {"title": title, "content": text}
