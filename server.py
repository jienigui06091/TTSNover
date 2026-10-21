#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页版阅读器 —— 后端（纯标准库，无第三方依赖）

启动：python3 server.py [端口]
"""
import json
import mimetypes
import os
import posixpath
import shutil
import sys
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import booklib
import sources
import tts

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8848))
MAX_UPLOAD = 60 * 1024 * 1024


def jbody(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if not n:
        return {}
    return json.loads(handler.rfile.read(n).decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "WebReader/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (self.address_string(), fmt % args))

    # ---------- 输出 ----------
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_download(self, data, ctype, filename):
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition",
                'attachment; filename="export.json"; filename*=UTF-8\'\'' +
                urllib.parse.quote(filename))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_bytes(self, data, ctype, code=200, cache="no-store"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def err(self, msg, code=400, **extra):
        self.send_json({"error": msg, **extra}, code)

    # ---------- 路由 ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self.static("index.html")
            if p.startswith("/static/"):
                return self.static(p[len("/static/"):])
            if p == "/api/books":
                return self.send_json({"books": booklib.list_books()})
            if p == "/api/presets":
                return self.send_json({
                    "presets": tts.PRESETS,
                    "mimo_voices": tts.MIMO_VOICES,
                    "mimo_ready": tts.mimo_ready(),
                })
            if p == "/api/settings":
                return self.send_json(booklib.get_settings())
            if p == "/api/sources/export":
                only = (q.get("only") or ["0"])[0] in ("1", "true", "yes")
                lst = sources.export_sources(only)
                data = json.dumps(lst, ensure_ascii=False, indent=1).encode("utf-8")
                fn = "书源-%s-%s.json" % ("启用" if only else "全部",
                                          time.strftime("%Y%m%d"))
                return self.send_download(data, "application/json; charset=utf-8", fn)
            if p == "/api/sources":
                lst = sources.list_sources()
                return self.send_json({"sources": lst, "total": len(lst),
                                       "enabled": sum(1 for x in lst if x["enabled"])})
            if p == "/api/src/search":
                kw = (q.get("q") or [""])[0].strip()
                if not kw:
                    return self.err("缺少关键词")
                page = int((q.get("page") or ["1"])[0])
                only = (q.get("sources") or [""])[0]
                ids = [x for x in only.split(",") if x] if only else None
                t0 = time.time()
                res, stat = sources.search(kw, ids, page)
                stat["ms"] = int((time.time() - t0) * 1000)
                return self.send_json({"key": kw, "results": res, "stats": stat})
            if p == "/api/src/info":
                return self.send_json(sources.book_info((q.get("sid") or [""])[0],
                                                        (q.get("url") or [""])[0]))
            if p == "/api/src/toc":
                return self.send_json(sources.book_toc((q.get("sid") or [""])[0],
                                                       (q.get("url") or [""])[0],
                                                       (q.get("toc") or [""])[0]))
            if p == "/api/src/ch":
                return self.send_json(sources.chapter_text((q.get("sid") or [""])[0],
                                                           (q.get("url") or [""])[0],
                                                           (q.get("book") or [""])[0]))
            if p == "/api/tts/cache":
                d = tts.CACHE_DIR
                n = len([f for f in os.listdir(d) if f.endswith(".bin")]) if os.path.isdir(d) else 0
                size = sum(
                    os.path.getsize(os.path.join(d, f))
                    for f in os.listdir(d) if f.endswith(".bin")
                ) if os.path.isdir(d) else 0
                return self.send_json({"files": n, "bytes": size})

            parts = [x for x in p.split("/") if x]
            # /api/books/<id>[/ch/<idx>][/search]
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "books":
                bid = int(parts[2])
                if len(parts) == 3:
                    b = booklib.get_book(bid)
                    return self.send_json(b) if b else self.err("书不存在", 404)
                if len(parts) == 5 and parts[3] == "ch":
                    idx = int(parts[4])
                    b = booklib.get_book(bid)
                    if b and b.get("fmt") == "src":
                        got = sources.ensure_chapter(bid, idx)
                        if not got:
                            return self.err("章节不存在", 404)
                        n = len(b.get("chapters") or [])
                        paras = [x.strip() for x in (got["content"] or "").split("\n") if x.strip()]
                        if got.get("error"):
                            paras = ["（抓取失败：%s）" % got["error"]]
                        return self.send_json({"idx": idx, "title": got["title"],
                                               "paras": paras, "count": n})
                    ch = booklib.get_chapter(bid, idx)
                    return self.send_json(ch) if ch else self.err("章节不存在", 404)
                if len(parts) == 4 and parts[3] == "search":
                    kw = (q.get("q") or [""])[0].strip()
                    if not kw:
                        return self.err("缺少关键词")
                    return self.send_json({"hits": booklib.search(bid, kw)})
            return self.err("接口不存在：" + p, 404)
        except Exception as e:
            traceback.print_exc()
            return self.err(f"服务器异常：{e}", 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if p == "/api/import":
                return self.api_import(q)
            if p == "/api/sources/import":
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if not raw:
                    return self.err("没有收到内容")
                try:
                    added, updated = sources.import_sources(raw)
                except Exception as e:
                    return self.err("不是合法的书源 JSON：%s" % e)
                return self.send_json({"ok": True, "added": added, "updated": updated,
                                       "total": len(sources.list_sources())})
            if p == "/api/src/add":
                d = jbody(self)
                try:
                    bid, n = sources.add_book(d.get("sid", ""), d.get("book") or {},
                                              d.get("tocUrl", ""), d.get("chapters"))
                except Exception as e:
                    return self.err(str(e))
                return self.send_json({"ok": True, "id": bid, "chapters": n})
            if p == "/api/tts":
                return self.api_tts()
            if p == "/api/tts/test":
                return self.api_tts_test()
            if p == "/api/tts/cache/clear":
                if os.path.isdir(tts.CACHE_DIR):
                    shutil.rmtree(tts.CACHE_DIR)
                return self.send_json({"ok": True})
            if p == "/api/tts/key":
                d = jbody(self)
                try:
                    tts.save_mimo_key(d.get("key") or "")
                except tts.TTSError as e:
                    return self.err(e.msg, 400)
                return self.send_json({"ok": True, "ready": tts.mimo_ready()})
            parts = [x for x in p.split("/") if x]
            if len(parts) == 4 and parts[1] == "sources" and parts[3] == "toggle":
                return self.send_json({"ok": True, "enabled": sources.toggle_source(parts[2])})
            if len(parts) == 4 and parts[1] == "sources" and parts[3] == "test":
                return self.send_json(sources.test_source(parts[2], jbody(self).get("key") or "剑来"))
            if len(parts) == 4 and parts[1] == "books" and parts[3] == "progress":
                d = jbody(self)
                booklib.set_progress(int(parts[2]), d.get("ch", 0), d.get("off", 0))
                return self.send_json({"ok": True})
            return self.err("接口不存在：" + p, 404)
        except Exception as e:
            traceback.print_exc()
            return self.err(f"服务器异常：{e}", 500)

    def do_PUT(self):
        try:
            if self.path.split("?")[0] == "/api/settings":
                booklib.put_settings(jbody(self))
                return self.send_json({"ok": True})
            return self.err("接口不存在", 404)
        except Exception as e:
            traceback.print_exc()
            return self.err(f"服务器异常：{e}", 500)

    def do_DELETE(self):
        try:
            parts = [x for x in self.path.split("?")[0].split("/") if x]
            if len(parts) == 3 and parts[1] == "books":
                booklib.delete_book(int(parts[2]))
                return self.send_json({"ok": True})
            if len(parts) == 3 and parts[1] == "sources":
                sources.delete_source(parts[2])
                return self.send_json({"ok": True})
            return self.err("接口不存在", 404)
        except Exception as e:
            traceback.print_exc()
            return self.err(f"服务器异常：{e}", 500)

    # ---------- 具体实现 ----------
    def static(self, rel):
        rel = posixpath.normpath(urllib.parse.unquote(rel)).lstrip("/")
        full = os.path.join(STATIC, rel)
        if not os.path.abspath(full).startswith(STATIC) or not os.path.isfile(full):
            return self.err("文件不存在", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith((".html", ".js", ".css")):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        cache = "no-cache" if full.endswith((".html", ".js", ".css")) else "max-age=86400"
        return self.send_bytes(data, ctype, cache=cache)

    def api_import(self, q):
        name = (q.get("filename") or ["book.txt"])[0]
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self.err("没有收到文件内容")
        if n > MAX_UPLOAD:
            return self.err("文件太大（上限 60MB）", 413)
        raw = self.rfile.read(n)
        low = name.lower()

        if low.endswith(".epub"):
            title, author, chapters = booklib.parse_epub(raw)
            fmt = "epub"
        else:
            text = booklib.decode_bytes(raw)
            chapters = booklib.split_chapters(text)
            title = os.path.splitext(os.path.basename(name))[0][:80] or "未命名"
            author = ""
            fmt = "txt"
            if not chapters:
                return self.err("解析不出正文，可能不是纯文本")
        bid = booklib.add_book(title, author, fmt, chapters)
        return self.send_json({"id": bid, "title": title, "author": author,
                               "chapters": len(chapters), "fmt": fmt})

    def _cfg_from_request(self, d):
        cfg = dict(tts.PRESETS.get("fish", {}))
        cfg.pop("note", None)
        saved = (booklib.get_settings() or {}).get("tts") or {}
        cfg.update({k: v for k, v in saved.items() if v not in (None, "")})
        cfg.update({k: v for k, v in (d.get("cfg") or {}).items() if v is not None})
        # 显式指定 provider：用该服务商自己的配置（profiles 里存过就用存的，否则用预设）
        prov = (d.get("provider") or "").strip()
        if prov:
            prof = (saved.get("profiles") or {}).get(prov) or {}
            for k, v in {**(tts.PRESETS.get(prov) or {}), **prof}.items():
                if k != "note" and v not in (None, ""):
                    cfg[k] = v
            cfg["provider"] = prov
        return cfg

    def api_tts(self):
        d = jbody(self)
        text = (d.get("text") or "").strip()
        if not text:
            return self.err("没有文本")
        cfg = self._cfg_from_request(d)
        for k in ("model", "style", "format"):
            if d.get(k) not in (None, ""):
                cfg[k] = d[k]
        if cfg.get("engine") == "web":
            return self.err("浏览器语音由前端处理，不该请求这里")
        try:
            audio, ctype, hit = tts.synthesize(
                cfg, text, d.get("voice"), d.get("speed"), use_cache=d.get("cache", True)
            )
        except tts.TTSError as e:
            return self.send_json(
                {"error": e.msg, "status": e.status, "body": e.body, "detail": e.detail}, 502
            )
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Cache", "hit" if hit else "miss")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    def api_tts_test(self):
        d = jbody(self)
        cfg = self._cfg_from_request(d)
        text = (d.get("text") or "你好，这是一次朗读测试。").strip()
        out = {"ok": False}
        import time
        t0 = time.time()
        try:
            audio, ctype, hit = tts.synthesize(
                cfg, text, d.get("voice"), d.get("speed"), use_cache=False
            )
            out.update(
                ok=True, bytes=len(audio), content_type=ctype,
                ms=int((time.time() - t0) * 1000), url=cfg.get("url", "")[:120],
            )
            if len(audio) > 4000:
                # 顺便存一份，方便前端试听
                os.makedirs(tts.CACHE_DIR, exist_ok=True)
                tmp = os.path.join(tts.CACHE_DIR, "_last_test.bin")
                with open(tmp, "wb") as f:
                    f.write(audio)
        except tts.TTSError as e:
            out.update(ok=False, error=e.msg, status=e.status, body=e.body,
                       detail=e.detail, ms=int((time.time() - t0) * 1000))
        return self.send_json(out)


def main():
    booklib.init_db()
    sources.init_db()
    os.makedirs(tts.CACHE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"网页版阅读器已启动：http://0.0.0.0:{PORT}/  （Ctrl+C 停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
