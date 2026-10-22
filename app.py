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
import threading
import traceback
from collections import deque
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
import booklib
import claim
import keys
import sources
import tts

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
MAX_UPLOAD = 60 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app):
    booklib.init_db()
    sources.init_db()
    auth.init_db()
    keys.init_db()
    os.makedirs(tts.CACHE_DIR, exist_ok=True)
    auth.purge_expired()
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


# ---------------- 鉴权 ----------------
AUTH_ENFORCE = os.environ.get("AUTH_ENFORCE", "1") not in ("0", "false", "no")
# 并发闸门：搜书一个任务最多起 SEARCH_CONC 个 node 子进程，全服最多同时跑 2 个搜索
SEARCH_MAX = int(os.environ.get("SEARCH_MAX", "2"))
SEARCH_SEM = threading.Semaphore(SEARCH_MAX)
TTS_PER_MIN = int(os.environ.get("TTS_PER_MIN", "120"))
_tts_hits = {}
_tts_lock = threading.Lock()
PUBLIC_PREFIX = ("/static/", "/api/auth/", "/api/docs", "/api/openapi.json")
PUBLIC_EXACT = ("/login", "/favicon.ico")


def token_from(request: Request) -> str:
    """Web 走 HttpOnly Cookie，App 走 Authorization: ***"""
    raw = request.cookies.get(auth.COOKIE_NAME)
    if raw:
        return raw
    h = request.headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return ""


def set_session_cookie(resp: Response, request: Request, token: str):
    https = (request.headers.get("x-forwarded-proto") == "https"
             or request.url.scheme == "https")
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.TOKEN_TTL,
                    httponly=True, samesite="lax", secure=https, path="/")


def current_user(request: Request):
    return getattr(request.state, "user", None)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # 先解析凭据（公开路径也解析，/api/auth/me、/api/auth/logout 才能拿到当前用户）
    raw = token_from(request)
    user = auth.user_by_token(raw) if raw else None
    if user:
        request.state.user = user
        request.state.token = raw
    if user or path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX):
        return await call_next(request)
    if path == "/" and AUTH_ENFORCE:
        # 未登录访问书架：直接送登录页，别先加载一个必然 401 的页面
        return RedirectResponse("/login?next=/", status_code=303)
    if not AUTH_ENFORCE:
        # 开发期兜底：未登录时按首个账号处理（AUTH_ENFORCE=0 才生效）
        u = auth.first_user()
        if u:
            request.state.user = u
            request.state.token = ""
            return await call_next(request)
    return JSONResponse({"error": "请先登录", "code": "unauthorized"}, status_code=401)


@app.post("/api/auth/register")
async def api_register(request: Request):
    ip = auth.client_ip(request)
    if not auth.rate_ok("reg:" + ip):
        return jerr("操作太频繁，请 1 分钟后再试", 429)
    d = await jbody(request)
    try:
        u = auth.create_user(d.get("username") or "", d.get("password") or "")
    except auth.AuthError as e:
        return jerr(e.msg, e.status)
    token = auth.create_session(u["id"], auth.device_of(request), ip)
    claimed = None
    if auth.count_users() == 1:  # 开放注册的第一个账号 = 老数据的主人
        try:
            claimed = claim.claim_legacy(u["id"])
        except Exception:
            traceback.print_exc()
    resp = JSONResponse({"ok": True, "user": u, "token": token,
                         "expires_in": auth.TOKEN_TTL, "claimed": claimed})
    set_session_cookie(resp, request, token)
    return resp


@app.post("/api/auth/login")
async def api_login(request: Request):
    ip = auth.client_ip(request)
    if not auth.rate_ok("login:" + ip):
        return jerr("登录尝试太频繁，请 1 分钟后再试", 429)
    d = await jbody(request)
    u = auth.verify_user(d.get("username") or "", d.get("password") or "")
    if not u:
        return jerr("用户名或密码不对", 401)
    token = auth.create_session(u["id"], auth.device_of(request), ip)
    resp = JSONResponse({"ok": True, "user": u, "token": token,
                         "expires_in": auth.TOKEN_TTL})
    set_session_cookie(resp, request, token)
    return resp


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    auth.revoke(token_from(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
async def api_me(request: Request):
    u = current_user(request)
    if not u:
        return jerr("请先登录", 401)
    return {"user": u}


def uid_of(request: Request) -> int:
    """中间件已经把当前用户放进 request.state；拿不到就是 401（理论上走不到）。"""
    u = current_user(request)
    if not u:
        raise StarletteHTTPException(status_code=401, detail="请先登录")
    return int(u["id"])


# ---------------- 静态页面 ----------------
@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(os.path.join(STATIC, "login.html"),
                        media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(
        os.path.join(STATIC, "index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------- 书架 / 设置 ----------------
@app.get("/api/books")
def api_books(request: Request):
    return {"books": booklib.list_books(uid_of(request))}


@app.get("/api/settings")
def api_get_settings(request: Request):
    return booklib.get_settings(uid_of(request))


@app.put("/api/settings")
async def api_put_settings(request: Request):
    booklib.put_settings(uid_of(request), await jbody(request))
    return {"ok": True}


@app.get("/api/presets")
def api_presets(request: Request):
    uid = uid_of(request)
    return {
        "presets": tts.PRESETS,
        "mimo_voices": tts.MIMO_VOICES,
        "mimo_ready": tts.mimo_ready(uid),
        "mimo_key_masked": keys.masked_for(uid, "mimo"),
        "mimo_modes": tts.MIMO_MODES,
        "mimo_clones": tts.clone_voices(uid),
        "mimo_design_default": tts.MIMO_DESIGN_DEFAULT,
        "mimo_clone_max": tts.MIMO_CLONE_MAX,
    }


# ---------------- 单本书 / 章节 ----------------
@app.get("/api/books/{bid}")
def api_book(bid: int, request: Request):
    b = booklib.get_book(uid_of(request), bid)
    return b if b else jerr("书不存在", 404)


@app.get("/api/books/{bid}/ch/{idx}")
def api_chapter(bid: int, idx: int, request: Request):
    uid = uid_of(request)
    b = booklib.get_book(uid, bid)
    if b and b.get("fmt") == "src":
        got = sources.ensure_chapter(uid, bid, idx)
        if not got:
            return jerr("章节不存在", 404)
        n = len(b.get("chapters") or [])
        paras = [x.strip() for x in (got["content"] or "").split("\n") if x.strip()]
        if got.get("error"):
            paras = ["（抓取失败：%s）" % got["error"]]
        return {"idx": idx, "title": got["title"], "paras": paras, "count": n}
    ch = booklib.get_chapter(uid, bid, idx)
    return ch if ch else jerr("章节不存在", 404)


@app.get("/api/books/{bid}/search")
def api_book_search(bid: int, request: Request, q: str = ""):
    kw = (q or "").strip()
    if not kw:
        return jerr("缺少关键词")
    return {"hits": booklib.search(uid_of(request), bid, kw)}


@app.post("/api/books/{bid}/progress")
async def api_progress(bid: int, request: Request):
    d = await jbody(request)
    booklib.set_progress(uid_of(request), bid, d.get("ch", 0), d.get("off", 0))
    return {"ok": True}


@app.delete("/api/books/{bid}")
def api_delete_book(bid: int, request: Request):
    n = booklib.delete_book(uid_of(request), bid)
    return {"ok": True, "deleted": n}


@app.post("/api/books/delete")
async def api_books_delete(request: Request):
    """书架批量删除：只删自己的书，章节级联清掉。"""
    d = await jbody(request)
    ids = d.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jerr("没有选中任何书籍")
    n = booklib.delete_books(uid_of(request), ids)
    return {"ok": True, "deleted": n}


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
    bid = booklib.add_book(uid_of(request), title, author, fmt, chapters)
    return {"id": bid, "title": title, "author": author,
            "chapters": len(chapters), "fmt": fmt}


# ---------------- 书源 ----------------
@app.get("/api/sources")
def api_sources(request: Request):
    lst = sources.list_sources(uid_of(request))
    return {"sources": lst, "total": len(lst),
            "enabled": sum(1 for x in lst if x["enabled"])}


@app.get("/api/sources/export")
def api_sources_export(request: Request, only: str = "0"):
    flag = only in ("1", "true", "yes")
    lst = sources.export_sources(uid_of(request), flag)
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
        added, updated = sources.import_sources(uid_of(request), raw)
    except Exception as e:
        return jerr("不是合法的书源 JSON：%s" % e)
    return {"ok": True, "added": added, "updated": updated,
            "total": len(sources.list_sources(uid_of(request)))}


@app.post("/api/sources/delete")
async def api_sources_delete(request: Request):
    d = await jbody(request)
    ids = d.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jerr("没有选中任何书源")
    n = 0
    for sid in ids:
        try:
            n += sources.delete_source(uid_of(request), str(sid))
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
        added, updated = sources.import_sources(uid_of(request), raw)
    except Exception as e:
        return jerr("链接内容不是合法的书源 JSON：%s" % e)
    return {"ok": True, "added": added, "updated": updated,
            "total": len(sources.list_sources(uid_of(request)))}


@app.delete("/api/sources/{sid}")
def api_source_delete(sid: str, request: Request):
    n = sources.delete_source(uid_of(request), sid)
    if not n:
        return jerr("书源不存在", 404)
    return {"ok": True, "deleted": n}


@app.post("/api/sources/{sid}/toggle")
def api_source_toggle(sid: str, request: Request):
    res = sources.toggle_source(uid_of(request), sid)
    if res is None:
        return jerr("书源不存在", 404)
    return {"ok": True, "enabled": res}


@app.post("/api/sources/{sid}/test")
async def api_source_test(sid: str, request: Request):
    d = await jbody(request)
    return sources.test_source(uid_of(request), sid, d.get("key") or "剑来")


# ---------------- 书源搜书 / 详情 / 目录 / 正文 ----------------
@app.get("/api/src/search")
def api_src_search(request: Request, q: str = "", page: int = 1,
                   sources_: str = Query("", alias="sources")):
    kw = (q or "").strip()
    if not kw:
        return jerr("缺少关键词")
    ids = [x for x in sources_.split(",") if x] if sources_ else None
    if not SEARCH_SEM.acquire(blocking=False):
        return jerr("服务器正在处理别人的搜索，请等几秒再试", 429)
    try:
        t0 = time.time()
        res, stat = sources.search(uid_of(request), kw, ids, page)
        stat["ms"] = int((time.time() - t0) * 1000)
    finally:
        SEARCH_SEM.release()
    return {"key": kw, "results": res, "stats": stat}


@app.get("/api/src/info")
def api_src_info(request: Request, sid: str = "", url: str = ""):
    return sources.book_info(uid_of(request), sid, url)


@app.get("/api/src/toc")
def api_src_toc(request: Request, sid: str = "", url: str = "", toc: str = ""):
    return sources.book_toc(uid_of(request), sid, url, toc)


@app.get("/api/src/ch")
def api_src_ch(request: Request, sid: str = "", url: str = "", book: str = ""):
    return sources.chapter_text(uid_of(request), sid, url, book)


@app.post("/api/src/add")
async def api_src_add(request: Request):
    d = await jbody(request)
    try:
        bid, n = sources.add_book(uid_of(request), d.get("sid", ""), d.get("book") or {},
                                  d.get("tocUrl", ""), d.get("chapters"))
    except Exception as e:
        return jerr(str(e))
    return {"ok": True, "id": bid, "chapters": n}


# ---------------- TTS ----------------
def _cfg_from_request(uid, d):
    """与旧 server.py 完全一致的配置合并顺序：预设 → 已存设置 → 请求覆盖 → provider profile。"""
    cfg = dict(tts.PRESETS.get("fish", {}))
    cfg.pop("note", None)
    saved = (booklib.get_settings(uid) or {}).get("tts") or {}
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


def _tts_cfg(uid, d):
    cfg = _cfg_from_request(uid, d)
    for k in ("model", "style", "format", "mimoMode", "mimoVoiceDesc", "mimoClone"):
        if d.get(k) not in (None, ""):
            cfg[k] = d[k]
    return cfg


def tts_quota_ok(uid, limit=None):
    """每用户每分钟合成次数上限（防止一个账号把 CPU/上游额度刷爆）。"""
    limit = limit or TTS_PER_MIN
    now = time.time()
    with _tts_lock:
        q = _tts_hits.setdefault(int(uid), deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


@app.post("/api/tts")
async def api_tts(request: Request):
    d = await jbody(request)
    text = (d.get("text") or "").strip()
    if not text:
        return jerr("没有文本")
    uid = uid_of(request)
    if not tts_quota_ok(uid):
        return jerr("朗读请求太频繁（每分钟上限 %d 次），稍等一下" % TTS_PER_MIN, 429)
    cfg = _tts_cfg(uid, d)
    if cfg.get("engine") == "web":
        return jerr("浏览器语音由前端处理，不该请求这里")
    try:
        audio, ctype, hit = tts.synthesize(
            uid, cfg, text, d.get("voice"), d.get("speed"), use_cache=d.get("cache", True))
    except tts.TTSError as e:
        return JSONResponse({"error": e.msg, "status": e.status, "body": e.body,
                             "detail": e.detail}, status_code=502)
    return Response(content=audio, media_type=ctype,
                    headers={"X-Cache": "hit" if hit else "miss", "Cache-Control": "no-store"})


@app.post("/api/tts/test")
async def api_tts_test(request: Request):
    d = await jbody(request)
    uid = uid_of(request)
    if not tts_quota_ok(uid, 10):
        return jerr("测试太频繁了，等一分钟再试", 429)
    cfg = _tts_cfg(uid, d)
    text = (d.get("text") or "你好，这是一次朗读测试。").strip()
    out = {"ok": False}
    t0 = time.time()
    try:
        audio, ctype, hit = tts.synthesize(uid, cfg, text, d.get("voice"), d.get("speed"), use_cache=False)
        out.update(ok=True, bytes=len(audio), content_type=ctype,
                   ms=int((time.time() - t0) * 1000), url=cfg.get("url", "")[:120])
        if len(audio) > 4000:
            with open(os.path.join(tts.cache_dir(uid), "_last_test.bin"), "wb") as f:
                f.write(audio)
    except tts.TTSError as e:
        out.update(ok=False, error=e.msg, status=e.status, body=e.body,
                   detail=e.detail, ms=int((time.time() - t0) * 1000))
    return out


@app.get("/api/tts/cache")
def api_tts_cache(request: Request):
    return tts.cache_stats(uid_of(request))


@app.post("/api/tts/cache/clear")
def api_tts_cache_clear(request: Request):
    tts.cache_clear(uid_of(request))
    return {"ok": True}


@app.post("/api/tts/key")
async def api_tts_key(request: Request):
    d = await jbody(request)
    try:
        tts.save_mimo_key(uid_of(request), d.get("key") or "")
    except tts.TTSError as e:
        return jerr(e.msg, 400)
    uid = uid_of(request)
    return {"ok": True, "ready": tts.mimo_ready(uid), "masked": keys.masked_for(uid, "mimo")}


@app.post("/api/tts/mimo/clone")
async def api_tts_clone(request: Request):
    d = await jbody(request)
    try:
        raw = base64.b64decode((d.get("data") or "").split(",")[-1])
    except Exception:
        return jerr("样本 base64 解不开", 400)
    try:
        ent = tts.save_clone_voice(uid_of(request), d.get("name") or "", raw, d.get("filename") or "")
    except tts.TTSError as e:
        return jerr(e.msg, 400)
    return {"ok": True, "saved": ent, "clones": tts.clone_voices(uid_of(request))}


@app.post("/api/tts/mimo/clone/delete")
async def api_tts_clone_delete(request: Request):
    d = await jbody(request)
    try:
        tts.delete_clone_voice(uid_of(request), d.get("name") or "")
    except tts.TTSError as e:
        return jerr(e.msg, 400)
    return {"ok": True, "clones": tts.clone_voices(uid_of(request))}


# ---------------- 每用户 API Key ----------------
KEY_PROVIDERS = ("mimo", "fish", "siliconflow", "openai", "azure")


@app.get("/api/keys")
def api_keys(request: Request):
    uid = uid_of(request)
    return {"keys": {p: {"has": keys.has_key(uid, p), "masked": keys.masked_for(uid, p)}
                     for p in KEY_PROVIDERS}}


@app.post("/api/keys")
async def api_keys_set(request: Request):
    d = await jbody(request)
    uid = uid_of(request)
    prov = (d.get("provider") or "").strip()
    key = (d.get("key") or "").strip()
    if prov not in KEY_PROVIDERS:
        return jerr("不支持的 provider")
    if not key:
        return jerr("Key 不能为空")
    keys.set_key(uid, prov, key)
    return {"ok": True, "provider": prov, "masked": keys.masked_for(uid, prov)}


@app.delete("/api/keys/{provider}")
def api_keys_delete(provider: str, request: Request):
    uid = uid_of(request)
    n = keys.delete_key(uid, provider)
    return {"ok": True, "deleted": n}


# ---------------- 静态资源（放最后，避免吃掉 /api） ----------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8849))
    uvicorn.run(app, host="0.0.0.0", port=port)
