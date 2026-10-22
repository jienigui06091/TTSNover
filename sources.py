#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书源（Legado bookSource）支持：导入 / 管理 / 调用 Node 规则引擎 / 加入书架

数据表（书源按用户隔离：同一书源可被不同用户各自导入，主键 (user_id,id)）
  sources(user_id, id, name, url, grp, enabled, raw, added)
  books 追加：cover, src_id, src_name, src_url
  chapters 追加：url, fetched（0=只有链接，正文按需抓取）
"""
import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB = paths.DB
CLI = os.path.join(HERE, "srce", "cli.js")
NODE = "node"
CACHE = os.path.join(HERE, "data", "srccache")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  user_id INTEGER NOT NULL DEFAULT 0,
  id TEXT NOT NULL,
  name TEXT NOT NULL,
  url TEXT DEFAULT '',
  grp TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  raw TEXT NOT NULL,
  added REAL,
  PRIMARY KEY(user_id, id)
);
-- 书源健康度：连续失败到一定次数就先跳过，冷却结束再半开重试（死源不再拖慢每次搜索）
CREATE TABLE IF NOT EXISTS source_health(
  user_id INTEGER NOT NULL,
  sid TEXT NOT NULL,
  ok INTEGER DEFAULT 0,
  fail INTEGER DEFAULT 0,
  streak INTEGER DEFAULT 0,
  last_ok REAL DEFAULT 0,
  last_fail REAL DEFAULT 0,
  skip_until REAL DEFAULT 0,
  PRIMARY KEY(user_id, sid)
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
        # 老库（sources 无 user_id）就地重建为复合主键，旧数据 user_id=0 待认领
        have = {r["name"] for r in c.execute("PRAGMA table_info(sources)")}
        if "user_id" not in have:
            c.execute("ALTER TABLE sources RENAME TO sources_old")
            c.executescript(SCHEMA)
            c.execute("""INSERT INTO sources(user_id,id,name,url,grp,enabled,raw,added)
                         SELECT 0,id,name,url,grp,enabled,raw,added FROM sources_old""")
            c.execute("DROP TABLE sources_old")
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


def import_sources(uid, raw_bytes):
    """导入 Legado 分享文件（数组或单个对象）；同一用户重复导入=更新"""
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
            exists = c.execute(
                "SELECT 1 FROM sources WHERE user_id=? AND id=?", (int(uid), sid)
            ).fetchone()
            c.execute(
                """INSERT INTO sources(user_id,id,name,url,grp,enabled,raw,added) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,id) DO UPDATE SET name=excluded.name, url=excluded.url,
                     grp=excluded.grp, raw=excluded.raw""",
                (
                    int(uid),
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
    cache_clear(uid)
    return added, updated


def export_sources(uid, only_enabled=False):
    """导出为 Legado 分享格式（尽量还原原始 JSON，enabled 用库里的当前状态）"""
    sql = "SELECT name,url,grp,enabled,raw FROM sources WHERE user_id=?"
    if only_enabled:
        sql += " AND enabled=1"
    sql += " ORDER BY grp, name"
    out = []
    with conn() as c:
        for r in c.execute(sql, (int(uid),)):
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


def list_sources(uid, only_enabled=False):
    sql = "SELECT id,name,url,grp,enabled FROM sources WHERE user_id=?"
    if only_enabled:
        sql += " AND enabled=1"
    sql += " ORDER BY grp, name"
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, (int(uid),))]
    # 按分组统计
    return rows


def get_source(uid, sid):
    with conn() as c:
        r = c.execute("SELECT * FROM sources WHERE user_id=? AND id=?", (int(uid), sid)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["json"] = json.loads(d.pop("raw"))
    return d


def toggle_source(uid, sid, enabled=None):
    with conn() as c:
        if enabled is None:
            c.execute("UPDATE sources SET enabled=1-enabled WHERE user_id=? AND id=?", (int(uid), sid))
        else:
            c.execute("UPDATE sources SET enabled=? WHERE user_id=? AND id=?",
                      (1 if enabled else 0, int(uid), sid))
        r = c.execute("SELECT enabled FROM sources WHERE user_id=? AND id=?",
                      (int(uid), sid)).fetchone()
    cache_clear(uid)
    return None if not r else bool(r["enabled"])


def delete_source(uid, sid):
    with conn() as c:
        cur = c.execute("DELETE FROM sources WHERE user_id=? AND id=?", (int(uid), sid))
    cache_clear(uid)
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
# 单源超时：站点 3 秒不回基本就没戏，久等只会拖住整轮搜索
SRC_TIMEOUT = float(os.environ.get("SEARCH_SRC_TIMEOUT", "6"))
# 一轮搜索的总预算，到点就带着已有结果收工（前端是流式的，慢源不影响先看结果）
SEARCH_BUDGET = float(os.environ.get("SEARCH_BUDGET", "12"))
CONC = max(1, int(os.environ.get("SEARCH_CONC", "10")))
CACHE_TTL = int(os.environ.get("SEARCH_CACHE_TTL", "600"))
# 连续失败 n 次后的冷却秒数（第 1 次失败不冷却，避免偶发抖动就拉黑）
HEALTH_BACKOFF = (0, 0, 120, 300, 900, 3600)

_cache = {}
_cache_lock = threading.Lock()
_health_ready = False


def cache_get(uid, key, page=1, include_dead=False):
    """同关键词短 TTL 缓存；命中直接返回 (results, stat)，否则 None。"""
    ck = (int(uid), (key or "").strip().lower(), int(page or 1), bool(include_dead))
    with _cache_lock:
        hit = _cache.get(ck)
    if not hit:
        return None
    if time.time() - hit[0] >= CACHE_TTL:
        with _cache_lock:
            _cache.pop(ck, None)
        return None
    stat = dict(hit[2])
    stat["cached"] = True
    return [dict(r) for r in hit[1]], stat


def cache_put(uid, key, page, include_dead, results, stat):
    if not results:
        return
    ck = (int(uid), (key or "").strip().lower(), int(page or 1), bool(include_dead))
    st = dict(stat)
    st["cached"] = False
    ordered = sorted(results, key=lambda x: (-len(x.get("books") or []), x.get("source") or ""))
    with _cache_lock:
        if len(_cache) > 200:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                _cache.pop(k, None)
        _cache[ck] = (time.time(), ordered, st)


def cache_clear(uid=None):
    """书源增删改后清缓存，否则会拿旧源的结果糊弄人。"""
    with _cache_lock:
        if uid is None:
            _cache.clear()
        else:
            for k in [k for k in _cache if k[0] == int(uid)]:
                _cache.pop(k, None)


def _plain(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def _strip_author(s):
    return re.sub(r"^(作\s*者|author)\s*[:：]\s*", "", str(s or ""), flags=re.I).strip()


def _rank_books(books, key):
    """按「书名/作者命中关键词」打分排序。

    很多站点的搜索是模糊匹配，搜作者会混进一堆不相关的书；
    打分后作者命中的书排到前面，并在每本书上标 matched，前端可据此过滤。
    """
    k = _plain(key)
    scored = []
    for b in books or []:
        name = _plain(b.get("name"))
        author = _plain(_strip_author(b.get("author")))
        if not k:
            score, hit = 1, ""
        elif name == k:
            score, hit = 100, "name"
        elif author and author == k:
            score, hit = 90, "author"
        elif name.startswith(k):
            score, hit = 70, "name"
        elif author.startswith(k):
            score, hit = 60, "author"
        elif k in name:
            score, hit = 50, "name"
        elif author and k in author:
            score, hit = 40, "author"
        else:
            score, hit = 1, ""
        scored.append((score, hit, b))
    scored.sort(key=lambda x: -x[0])  # 稳定排序：同分保持站点原始顺序
    out = []
    for _score, hit, b in scored:
        out.append(
            {
                "name": b.get("name", ""),
                "author": b.get("author", ""),
                "kind": b.get("kind", ""),
                "intro": b.get("intro", ""),
                "cover": b.get("coverUrl") or b.get("cover", ""),
                "url": b.get("bookUrl") or b.get("url", ""),
                "lastChapter": b.get("lastChapter", ""),
                "matched": hit,
            }
        )
    return out


def _ensure_health_table():
    """source_health 是后加的，老库/测试副本可能还没有，用到时补建一次。"""
    global _health_ready
    if _health_ready:
        return
    with conn() as c:
        c.executescript(SCHEMA)
    _health_ready = True


def _health(uid):
    _ensure_health_table()
    with conn() as c:
        return {
            r["sid"]: dict(r)
            for r in c.execute(
                "SELECT sid,ok,fail,streak,last_ok,skip_until FROM source_health WHERE user_id=?",
                (int(uid),),
            )
        }


def _save_health(uid, stats):
    """stats: {sid: 是否成功}，一轮搜索结束一次性写回。"""
    now = time.time()
    with conn() as c:
        for sid, ok in stats.items():
            r = c.execute(
                "SELECT ok,fail,streak FROM source_health WHERE user_id=? AND sid=?",
                (int(uid), sid),
            ).fetchone()
            o, f, streak = (r["ok"], r["fail"], r["streak"]) if r else (0, 0, 0)
            if ok:
                c.execute(
                    "INSERT OR REPLACE INTO source_health"
                    " (user_id,sid,ok,fail,streak,last_ok,last_fail,skip_until)"
                    " VALUES(?,?,?,?,0,?,0,0)",
                    (int(uid), sid, o + 1, f, now),
                )
            else:
                streak += 1
                back = HEALTH_BACKOFF[min(streak, len(HEALTH_BACKOFF) - 1)]
                c.execute(
                    "INSERT OR REPLACE INTO source_health"
                    " (user_id,sid,ok,fail,streak,last_ok,last_fail,skip_until)"
                    " VALUES(?,?,?,?,?,0,?,?)",
                    (int(uid), sid, o, f + 1, streak, now, now + back),
                )


def health_report(uid):
    """给书源页用：哪些源被自动跳过、什么时候重试。"""
    now = time.time()
    with conn() as c:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT h.sid,h.ok,h.fail,h.streak,h.last_ok,h.last_fail,h.skip_until,"
                "COALESCE(s.name,'(已删除)') AS name"
                " FROM source_health h LEFT JOIN sources s"
                " ON s.user_id=h.user_id AND s.id=h.sid WHERE h.user_id=?"
                " ORDER BY h.skip_until DESC, h.fail DESC",
                (int(uid),),
            )
        ]
    for r in rows:
        r["dead"] = (r.get("skip_until") or 0) > now
        r["retry_in"] = max(0, int((r.get("skip_until") or 0) - now))
    return rows


def reset_health(uid, only_dead=True):
    """清掉冷却，让死源重新参与搜索（用户在书源页点「重试全部」时调）。"""
    sql = "UPDATE source_health SET streak=0, skip_until=0 WHERE user_id=?"
    if only_dead:
        sql += " AND skip_until>?"
    with conn() as c:
        cur = c.execute(sql, (int(uid), time.time()) if only_dead else (int(uid),))
        return cur.rowcount or 0


def _prepare(uid, source_ids=None, include_dead=False):
    srcs, skipped = [], 0
    health = _health(uid)
    now = time.time()
    for s in list_sources(uid, only_enabled=True):
        if source_ids and s["id"] not in source_ids:
            continue
        h = health.get(s["id"]) or {}
        if not include_dead and float(h.get("skip_until") or 0) > now:
            skipped += 1
            continue
        full = get_source(uid, s["id"])
        if full:
            srcs.append({"id": s["id"], "name": s["name"], "json": full["json"]})
    # 靠谱的源先跑：健康度好的排前面，流式结果能更快出东西
    order = {s["id"]: i for i, s in enumerate(srcs)}
    srcs.sort(key=lambda s: ((health.get(s["id"]) or {}).get("streak") or 0, order[s["id"]]))
    return srcs, skipped


def _one(src, key, page):
    sid = src["id"]
    try:
        res = call({"cmd": "search", "key": key, "sources": [src["json"]], "page": page},
                   timeout=SRC_TIMEOUT)
        r = (res.get("results") or [{}])[0]
    except subprocess.TimeoutExpired:
        r = {"source": src["name"], "error": "超时 %.0fs" % SRC_TIMEOUT}
    except Exception as e:
        r = {"source": src["name"], "error": str(e)[:120]}
    return {
        "source": src["name"],
        "sid": sid,
        "error": r.get("error", ""),
        "books": _rank_books(r.get("books") or [], key),
    }


def search_stream(uid, key, source_ids=None, page=1, include_dead=False):
    """返回 (meta, 迭代器)。迭代器每 yield 一个源的结果，前端可以边搜边显示。"""
    srcs, skipped = _prepare(uid, source_ids, include_dead)
    meta = {"sources": len(srcs) + skipped, "skipped": skipped}

    def gen():
        if not srcs:
            return
        health = {}
        acc = []
        deadline = time.time() + SEARCH_BUDGET
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(CONC, len(srcs)))
        futs = {ex.submit(_one, s, key, page): s for s in srcs}
        pending = set(futs)
        try:
            while pending:
                left = deadline - time.time()
                if left <= 0:
                    break
                done, pending = concurrent.futures.wait(
                    pending, timeout=left,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    s = futs[fut]
                    try:
                        r = fut.result()
                    except Exception as e:
                        r = {"source": s["name"], "sid": s["id"],
                             "error": str(e)[:120], "books": []}
                    health[s["id"]] = not r.get("error")
                    acc.append(r)
                    yield r
        finally:
            for fut in pending:
                fut.cancel()
            ex.shutdown(wait=False)
            for s in srcs:  # 预算用尽被砍掉的也算一次失败，下次少浪费并发
                health.setdefault(s["id"], False)
            try:
                _save_health(uid, health)
            except Exception:
                pass
            # 流式搜索也回写缓存，网页搜完再用 App/非流式接口就是秒回
            # （只搜了部分源时不写，避免把残缺结果当成全量缓存）
            if not source_ids:
                try:
                    ok = sum(1 for r in acc if r["books"])
                    cache_put(uid, key, page, include_dead, acc,
                              {"sources": len(srcs) + skipped, "ok": ok,
                               "books": sum(len(r["books"]) for r in acc),
                               "skipped": skipped})
                except Exception:
                    pass

    return meta, gen()


def search(uid, key, source_ids=None, page=1, include_dead=False, refresh=False):
    """并发搜索多个源；返回 (结果列表, 统计)。同关键词 10 分钟内直接吃缓存。"""
    if not refresh and not source_ids:
        hit = cache_get(uid, key, page, include_dead)
        if hit:
            return hit
    meta, it = search_stream(uid, key, source_ids, page, include_dead)
    out, ok, total = [], 0, 0
    for r in it:
        if r["books"]:
            ok += 1
            total += len(r["books"])
        out.append(r)
    out.sort(key=lambda x: (-len(x["books"]), x["source"]))
    stat = {"sources": meta["sources"], "ok": ok, "books": total,
            "skipped": meta["skipped"], "cached": False}
    if not source_ids:
        cache_put(uid, key, page, include_dead, out, stat)
    return out, stat


# ---------------- 详情 / 目录 / 正文 ----------------
def book_info(uid, sid, book_url):
    s = get_source(uid, sid)
    if not s:
        return {"error": "书源不存在"}
    return call({"cmd": "info", "source": s["json"], "bookUrl": book_url})


def book_toc(uid, sid, book_url, toc_url=""):
    s = get_source(uid, sid)
    if not s:
        return {"error": "书源不存在"}
    return call({"cmd": "toc", "source": s["json"], "bookUrl": book_url, "tocUrl": toc_url})


def chapter_text(uid, sid, chapter_url, book_url=""):
    s = get_source(uid, sid)
    if not s:
        return {"error": "书源不存在"}
    return call({"cmd": "content", "source": s["json"], "chapterUrl": chapter_url, "bookUrl": book_url})


def test_source(uid, sid, key="剑来"):
    s = get_source(uid, sid)
    if not s:
        return {"ok": False, "error": "书源不存在"}
    return call({"cmd": "test", "source": s["json"], "key": key}, timeout=45)


# ---------------- 加入书架 ----------------
def add_book(uid, sid, book, toc_url="", chapters=None):
    """把搜索结果 + 目录写进书架；正文按需抓取"""
    s = get_source(uid, sid)
    if not s:
        raise ValueError("书源不存在")
    if not chapters:
        t = book_toc(uid, sid, book["url"], toc_url)
        if t.get("error"):
            raise ValueError("取目录失败：" + str(t["error"]))
        chapters = t.get("chapters") or []
    if not chapters:
        raise ValueError("目录为空")
    with conn() as c:
        now = time.time()
        # 同一源同一本书重复加入 → 覆盖旧的（避免书架出现两本一样的）
        old = c.execute(
            "SELECT id FROM books WHERE user_id=? AND src_id=? AND src_url=? AND title=?",
            (int(uid), sid, book.get("url") or book.get("bookUrl") or "",
             (book.get("name") or "")[:120]),
        ).fetchone()
        if old:
            c.execute("DELETE FROM chapters WHERE book_id=?", (old["id"],))
            c.execute("DELETE FROM books WHERE id=?", (old["id"],))
        cur = c.execute(
            """INSERT INTO books(user_id,title,author,fmt,chars,created,opened,seed,cover,src_id,src_name,src_url)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(uid),
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


def ensure_chapter(uid, bid, idx):
    """章节正文没抓过就抓一次并落库（校验书属于当前用户）"""
    with conn() as c:
        r = c.execute(
            "SELECT ch.title,ch.content,ch.url,ch.fetched,b.src_id,b.src_url,b.title AS btitle FROM chapters ch "
            "JOIN books b ON b.id=ch.book_id WHERE ch.book_id=? AND ch.idx=? AND b.user_id=?",
            (bid, idx, int(uid)),
        ).fetchone()
        if not r:
            return None
        if r["fetched"] and (r["content"] or "").strip():
            return {"title": r["title"], "content": r["content"]}
        if not r["url"] or not r["src_id"]:
            return {"title": r["title"], "content": r["content"] or ""}
        sid, ch_url, book_url = r["src_id"], r["url"], r["src_url"]
        title = r["title"]
    res = chapter_text(uid, sid, ch_url, book_url)
    if res.get("error") or not (res.get("content") or "").strip():
        return {"title": title, "content": "", "error": res.get("error") or "正文为空"}
    text = res["content"].strip()
    with conn() as c:
        c.execute(
            "UPDATE chapters SET content=?,fetched=1 WHERE book_id=? AND idx=?", (text, bid, idx)
        )
        c.execute("UPDATE books SET chars=chars+? WHERE id=? AND user_id=?", (len(text), bid, int(uid)))
    return {"title": title, "content": text}
