#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页版阅读器 —— FastAPI 后端

多用户改造 P0：把 server.py 的 24 条路由平移到 FastAPI，行为/响应体保持一致，
业务逻辑仍复用 booklib / sources / tts（数据隔离在 P2 接入）。

启动：
    .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8849
"""
import base64
import json
import os
import shutil
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import booklib
import sources
import tts

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
MAX_UPLOAD = 60 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app):
    booklib.init_db()
    sources.init_db()
    os.makedirs(tts.CACHE_DIR, exist_ok=True)
    yield


app = FastAPI(
    title="TTSNover API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


# ---------------- 基础工具 ----------------
def jerr(msg, code=400, **extra):
    return JSONResponse({"error": msg, **extra}, status_code=code)


async def jbody(request: Request):
    """读 JSON body；空 body 当 {}，与旧 server.py 的 jbody 行为一致。"""
    raw = await request.body()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse({"error": "服务器异常：%s" % exc}, status_code=500)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    """保持旧接口的 {"error": ...} 形状，前端 api() 靠它取错误文案。"""
    if exc.status_code == 404:
        return JSONResponse({"error": "接口不存在：%s" % request.url.path}, status_code=404)
    detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse({"error": detail}, status_code=exc.status_code)


@app.on_event("startup")
def on_startup():
    booklib.init_db()
    sources.init_db()
    os.makedirs(tts.CACHE_DIR, exist_ok=True)


# ---------------- 静态页面 ----------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(
        os.path.join(STATIC, "index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------- 书架 / 设置 ----------------
@app.get("/api/books")
def api_books():
    return {"books": booklib.list_books()}


@app.get("/api/settings")
def api_get_settings():
    return booklib.get_settings()


@app.put("/api/settings")
async def api_put_settings(request: Request):
    booklib.put_settings(await jbody(request))
    return {"ok": True}


@app.get("/api/presets")
def api_presets():
    return {
        "presets": tts.PRESETS,
        "mimo_voices": tts.MIMO_VOICES,
        "mimo_ready": tts.mimo_ready(),
        "mimo_modes": tts.MIMO_MODES,
        "mimo_clones": tts.clone_voices(),
        "mimo_design_default": tts.MIMO_DESIGN_DEFAULT,
        "mimo_clone_max": tts.MIMO_CLONE_MAX,
    }


# ---------------- 单本书 / 章节 ----------------
@app.get("/api/books/{bid}")
def api_book(bid: int):
    b = booklib.get_book(bid)
    return b if b else jerr("书不存在", 404)


@app.get("/api/books/{bid}/ch/{idx}")
def api_chapter(bid: int, idx: int):
    b = booklib.get_book(bid)
    if b and b.get("fmt") == "src":
        got = sources.ensure_chapter(bid, idx)
        if not got:
            return jerr("章节不存在", 404)
        n = len(b.get("chapters") or [])
        paras = [x.strip() for x in (got["content"] or "").split("\n") if x.strip()]
        if got.get("error"):
            paras = ["（抓取失败：%s）" % got["error"]]
        return {"idx": idx, "title": got["title"], "paras": paras, "count": n}
    ch = booklib.get_chapter(bid, idx)
    return ch if ch else jerr("章节不存在", 404)


@app.get("/api/books/{bid}/search")
def api_book_search(bid: int, q: str = ""):
    kw = (q or "").strip()
    if not kw:
        return jerr("缺少关键词")
    return {"hits": booklib.search(bid, kw)}


@app.post("/api/books/{bid}/progress")
async def api_progress(bid: int, request: Request):
    d = await jbody(request)
    booklib.set_progress(bid, d.get("ch", 0), d.get("off", 0))
    return {"ok": True}


@app.delete("/api/books/{bid}")
def api_delete_book(bid: int):
    booklib.delete_book(bid)
    return {"ok": True}


# ---------------- 导入 ----------------
@app.post("/api/import")
async def api_import(request: Request, filename: str = "book.txt"):
    raw = await request.body()
    if not raw:
        return jerr("没有收到文件内容")
    if len(raw) > MAX_UPLOAD:
        return jerr("文件太大（上限 60MB）", 413)
    low = filename.lower()
    if low.endswith(".epub"):
        title, author, chapters = booklib.parse_epub(raw)
        fmt = "epub"
    else:
        text = booklib.decode_bytes(raw)
        chapters = booklib.split_chapters(text)
        title = os.path.splitext(os.path.basename(filename))[0][:80] or "未命名"
        author = ""
        fmt = "txt"
        if not chapters:
            return jerr("解析不出正文，可能不是纯文本")
    bid = booklib.add_book(title, author, fmt, chapters)
    return {"id": bid, "title": title, "author": author,
            "chapters": len(chapters), "fmt": fmt}


# ---------------- 书源 ----------------
@app.get("/api/sources")
def api_sources():
    lst = sources.list_sources()
    return {"sources": lst, "total": len(lst),
            "enabled": sum(1 for x in lst if x["enabled"])}


@app.get("/api/sources/export")
def api_sources_export(only: str = "0"):
    flag = only in ("1", "true", "yes")
    lst = sources.export_sources(flag)
    data = json.dumps(lst, ensure_ascii=False, indent=1).encode("utf-8")
    fn = "书源-%s-%s.json" % ("启用" if flag else "全部", time.strftime("%Y%m%d"))
    return Response(
        content=data,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition":
                'attachment; filename="export.json"; filename*=UTF-8\'\'' + urllib.parse.quote(fn),
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/sources/import")
async def api_sources_import(request: Request):
    raw = await request.body()
    if not raw:
        return jerr("没有收到内容")
    try:
        added, updated = sources.import_sources(raw)
    except Exception as e:
        return jerr("不是合法的书源 JSON：%s" % e)
    return {"ok": True, "added": added, "updated": updated,
            "total": len(sources.list_sources())}


@app.post("/api/sources/delete")
async def api_sources_delete(request: Request):
    d = await jbody(request)
    ids = d.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jerr("没有选中任何书源")
    n = 0
    for sid in ids:
        try:
            n += sources.delete_source(str(sid))
        except Exception:
            traceback.print_exc()
    return {"ok": True, "deleted": n}


@app.post("/api/sources/fetch")
async def api_sources_fetch(request: Request):
    d = await jbody(request)
    url = (d.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return jerr("请填 http(s) 开头的书源链接")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (webreader)"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read(20 * 1024 * 1024)
    except Exception as e:
        return jerr("下载失败：%s" % e)
    try:
        added, updated = sources.import_sources(raw)
    except Exception as e:
        return jerr("链接内容不是合法的书源 JSON：%s" % e)
    return {"ok": True, "added": added, "updated": updated,
            "total": len(sources.list_sources())}


@app.delete("/api/sources/{sid}")
def api_source_delete(sid: str):
    sources.delete_source(sid)
    return {"ok": True}


@app.post("/api/sources/{sid}/toggle")
def api_source_toggle(sid: str):
    return {"ok": True, "enabled": sources.toggle_source(sid)}


@app.post("/api/sources/{sid}/test")
async def api_source_test(sid: str, request: Request):
    d = await jbody(request)
    return sources.test_source(sid, d.get("key") or "剑来")


# ---------------- 书源搜书 / 详情 / 目录 / 正文 ----------------
@app.get("/api/src/search")
def api_src_search(q: str = "", page: int = 1, sources_: str = Query("", alias="sources")):
    kw = (q or "").strip()
    if not kw:
        return jerr("缺少关键词")
    ids = [x for x in sources_.split(",") if x] if sources_ else None
    t0 = time.time()
    res, stat = sources.search(kw, ids, page)
    stat["ms"] = int((time.time() - t0) * 1000)
    return {"key": kw, "results": res, "stats": stat}


@app.get("/api/src/info")
def api_src_info(sid: str = "", url: str = ""):
    return sources.book_info(sid, url)


@app.get("/api/src/toc")
def api_src_toc(sid: str = "", url: str = "", toc: str = ""):
    return sources.book_toc(sid, url, toc)


@app.get("/api/src/ch")
def api_src_ch(sid: str = "", url: str = "", book: str = ""):
    return sources.chapter_text(sid, url, book)


@app.post("/api/src/add")
async def api_src_add(request: Request):
    d = await jbody(request)
    try:
        bid, n = sources.add_book(d.get("sid", ""), d.get("book") or {},
                                  d.get("tocUrl", ""), d.get("chapters"))
    except Exception as e:
        return jerr(str(e))
    return {"ok": True, "id": bid, "chapters": n}


# ---------------- TTS ----------------
def _cfg_from_request(d):
    """与旧 server.py 完全一致的配置合并顺序：预设 → 已存设置 → 请求覆盖 → provider profile。"""
    cfg = dict(tts.PRESETS.get("fish", {}))
    cfg.pop("note", None)
    saved = (booklib.get_settings() or {}).get("tts") or {}
    cfg.update({k: v for k, v in saved.items() if v not in (None, "")})
    cfg.update({k: v for k, v in (d.get("cfg") or {}).items() if v is not None})
    prov = (d.get("provider") or "").strip()
    if prov:
        prof = (saved.get("profiles") or {}).get(prov) or {}
        for k, v in {**(tts.PRESETS.get(prov) or {}), **prof}.items():
            if k != "note" and v not in (None, ""):
                cfg[k] = v
        cfg["provider"] = prov
    return cfg


def _tts_cfg(d):
    cfg = _cfg_from_request(d)
    for k in ("model", "style", "format", "mimoMode", "mimoVoiceDesc", "mimoClone"):
        if d.get(k) not in (None, ""):
            cfg[k] = d[k]
    return cfg


@app.post("/api/tts")
async def api_tts(request: Request):
    d = await jbody(request)
    text = (d.get("text") or "").strip()
    if not text:
        return jerr("没有文本")
    cfg = _tts_cfg(d)
    if cfg.get("engine") == "web":
        return jerr("浏览器语音由前端处理，不该请求这里")
    try:
        audio, ctype, hit = tts.synthesize(
            cfg, text, d.get("voice"), d.get("speed"), use_cache=d.get("cache", True))
    except tts.TTSError as e:
        return JSONResponse({"error": e.msg, "status": e.status, "body": e.body,
                             "detail": e.detail}, status_code=502)
    return Response(content=audio, media_type=ctype,
                    headers={"X-Cache": "hit" if hit else "miss", "Cache-Control": "no-store"})


@app.post("/api/tts/test")
async def api_tts_test(request: Request):
    d = await jbody(request)
    cfg = _tts_cfg(d)
    text = (d.get("text") or "你好，这是一次朗读测试。").strip()
    out = {"ok": False}
    t0 = time.time()
    try:
        audio, ctype, hit = tts.synthesize(cfg, text, d.get("voice"), d.get("speed"), use_cache=False)
        out.update(ok=True, bytes=len(audio), content_type=ctype,
                   ms=int((time.time() - t0) * 1000), url=cfg.get("url", "")[:120])
        if len(audio) > 4000:
            os.makedirs(tts.CACHE_DIR, exist_ok=True)
            with open(os.path.join(tts.CACHE_DIR, "_last_test.bin"), "wb") as f:
                f.write(audio)
    except tts.TTSError as e:
        out.update(ok=False, error=e.msg, status=e.status, body=e.body,
                   detail=e.detail, ms=int((time.time() - t0) * 1000))
    return out


@app.get("/api/tts/cache")
def api_tts_cache():
    d = tts.CACHE_DIR
    files = [f for f in os.listdir(d) if f.endswith(".bin")] if os.path.isdir(d) else []
    size = sum(os.path.getsize(os.path.join(d, f)) for f in files) if files else 0
    return {"files": len(files), "bytes": size}


@app.post("/api/tts/cache/clear")
def api_tts_cache_clear():
    if os.path.isdir(tts.CACHE_DIR):
        shutil.rmtree(tts.CACHE_DIR)
    os.makedirs(tts.CACHE_DIR, exist_ok=True)
    return {"ok": True}


@app.post("/api/tts/key")
async def api_tts_key(request: Request):
    d = await jbody(request)
    try:
        tts.save_mimo_key(d.get("key") or "")
    except tts.TTSError as e:
        return jerr(e.msg, 400)
    return {"ok": True, "ready": tts.mimo_ready()}


@app.post("/api/tts/mimo/clone")
async def api_tts_clone(request: Request):
    d = await jbody(request)
    try:
        raw = base64.b64decode((d.get("data") or "").split(",")[-1])
    except Exception:
        return jerr("样本 base64 解不开", 400)
    try:
        ent = tts.save_clone_voice(d.get("name") or "", raw, d.get("filename") or "")
    except tts.TTSError as e:
        return jerr(e.msg, 400)
    return {"ok": True, "saved": ent, "clones": tts.clone_voices()}


@app.post("/api/tts/mimo/clone/delete")
async def api_tts_clone_delete(request: Request):
    d = await jbody(request)
    try:
        tts.delete_clone_voice(d.get("name") or "")
    except tts.TTSError as e:
        return jerr(e.msg, 400)
    return {"ok": True, "clones": tts.clone_voices()}


# ---------------- 静态资源（放最后，避免吃掉 /api） ----------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8849))
    uvicorn.run(app, host="0.0.0.0", port=port)
