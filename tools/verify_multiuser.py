#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多用户自检（改动后跑一遍，不碰线上数据、不调 TTS 上游）

    python3 tools/verify_multiuser.py

流程：拷一份 data/reader.db 到 /tmp → 迁移 → 临时 uvicorn(8867) → 首账号认领 →
双账号隔离 → 阅读链路 → 书源增删 → Key 掩码 → TTS 限流 → 线上 8848 鉴权闸门。

1. 用 data/reader.db 的副本重建隔离环境（含 20 个缓存文件 + 假小米 key）
2. 起一个临时 uvicorn（8867）跑：首账号认领 → 双账号隔离 → 阅读链路 → 限流
3. 顺带检查线上 8848 的鉴权闸门
不碰线上库、不调任何 TTS 上游、不写 data/。
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = "/home/liumanu/code/webreader"
TMP = "/tmp/hermes-verify-data"
PORT = 8867
BASE = "http://127.0.0.1:%d" % PORT
LIVE = "http://127.0.0.1:8848"
ok = fail = 0
proc = None


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        fail += 1
        print("  FAIL  %s  %s" % (name, extra))


def call(path, method="GET", body=None, token=None, raw=None, ctype="application/json",
         base=None):
    url = (base or BASE) + urllib.parse.quote(path, safe="/?=&%")
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def j(b):
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return {}


def setup():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "tts"))
    shutil.copy(os.path.join(ROOT, "data/reader.db"), os.path.join(TMP, "reader.db"))
    bins = [f for f in os.listdir(os.path.join(ROOT, "data/tts")) if f.endswith(".bin")][:20]
    for f in bins:
        shutil.copy(os.path.join(ROOT, "data/tts", f), os.path.join(TMP, "tts", f))
    with open(os.path.join(TMP, "mimo.key"), "w") as f:
        f.write("sk-fake-legacy-key-9999\n")
    os.chmod(os.path.join(TMP, "mimo.key"), 0o600)
    subprocess.run([sys.executable, "tools/migrate_multiuser.py", "--db",
                    os.path.join(TMP, "reader.db")], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    return len(bins)


def start():
    env = dict(os.environ, WEBREADER_DATA=TMP, AUTH_ENFORCE="1", SEARCH_CONC="4")
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)
    env["no_proxy"] = "127.0.0.1,localhost"
    p = subprocess.Popen([os.path.join(ROOT, ".venv/bin/uvicorn"), "app:app",
                          "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(60):
        time.sleep(0.4)
        try:
            urllib.request.urlopen(BASE + "/login", timeout=2)
            return p
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("服务启动失败：" + p.stderr.read().decode()[-800:])
    raise RuntimeError("服务 24s 内没起来")


def main():
    global proc
    nfiles = setup()
    proc = start()
    print("环境：%s（真库副本 + %d 个缓存 + 假 key）" % (TMP, nfiles))

    print("A. 首账号认领")
    st, _, b = call("/api/auth/register", "POST", {"username": "主人", "password": "owner-pass-123"})
    d = j(b)
    cl = d.get("claimed") or {}
    tok_a = d.get("token", "")
    check("注册并返回 token", st == 200 and tok_a, str(st))
    check("认领 5 本书", cl.get("books") == 5, str(cl))
    check("认领 31 个书源", cl.get("sources") == 31, str(cl))
    check("认领缓存文件", cl.get("tts_files") == nfiles, str(cl))
    check("迁移旧小米 key", cl.get("mimo_key") is True, str(cl))
    check("旧 key 明文文件已归档",
          not os.path.exists(os.path.join(TMP, "mimo.key"))
          and os.path.exists(os.path.join(TMP, "mimo.key.migrated")))

    print("B. 第二个账号空数据 + 隔离")
    st, _, b = call("/api/auth/register", "POST", {"username": "bob", "password": "bob-pass-12345"})
    tok_b = j(b).get("token", "")
    check("bob 注册", st == 200 and tok_b, str(st))
    check("bob 不认领", j(b).get("claimed") is None, str(j(b).get("claimed")))
    st, _, b = call("/api/books", token=tok_a)
    books_a = j(b).get("books") or []
    st, _, b = call("/api/books", token=tok_b)
    check("A 有书 / B 无书", len(books_a) == 5 and len(j(b).get("books") or []) == 0,
          "%d/%d" % (len(books_a), len(j(b).get("books") or [])))
    st, _, b = call("/api/sources", token=tok_a)
    srcs_a = j(b).get("sources") or []
    st, _, b = call("/api/sources", token=tok_b)
    check("A 有 31 源 / B 无源", len(srcs_a) == 31 and len(j(b).get("sources") or []) == 0,
          "%d/%d" % (len(srcs_a), len(j(b).get("sources") or [])))
    bid = books_a[0]["id"]
    sid = srcs_a[0]["id"]
    check("B 读 A 的书 → 404", call("/api/books/%d" % bid, token=tok_b)[0] == 404)
    check("B 读 A 章节 → 404", call("/api/books/%d/ch/0" % bid, token=tok_b)[0] == 404)
    check("B 删 A 书 → deleted:0",
          j(call("/api/books/%d" % bid, "DELETE", token=tok_b)[2]).get("deleted") == 0)
    check("B 删 A 源 → 404", call("/api/sources/%s" % sid, "DELETE", token=tok_b)[0] == 404)
    check("B 切 A 源 → 404", call("/api/sources/%s/toggle" % sid, "POST", token=tok_b)[0] == 404)
    check("B 搜 A 正文 → 0 命中",
          len(j(call("/api/books/%d/search?q=的" % bid, token=tok_b)[2]).get("hits") or []) == 0)

    print("C. 阅读链路（A 的真实数据）")
    txt = next((x for x in books_a if x.get("fmt") != "src"), None)
    st, _, b = call("/api/books/%d" % txt["id"], token=tok_a)
    check("书详情", st == 200 and j(b).get("title") == txt["title"], str(st))
    st, _, b = call("/api/books/%d/ch/0" % txt["id"], token=tok_a)
    d = j(b)
    check("章节正文", st == 200 and len(d.get("paras") or []) >= 1,
          "%s paras=%s" % (st, len(d.get("paras") or [])))
    check("章节标题", bool(d.get("title")), str(d.get("title"))[:30])
    st, _, b = call("/api/books/%d/search?q=的" % txt["id"], token=tok_a)
    check("全书搜索有命中", len(j(b).get("hits") or []) > 0, str(st))
    st, _, b = call("/api/books/%d/progress" % txt["id"], "POST", {"ch": 3, "off": 0.5}, token=tok_a)
    st, _, b = call("/api/books", token=tok_a)
    nb = next(x for x in (j(b).get("books") or []) if x["id"] == txt["id"])
    check("进度写入并回读", int(nb.get("progress_ch") or -1) == 3, str(nb.get("progress_ch")))
    src = next((x for x in books_a if x.get("fmt") == "src"), None)
    st, _, b = call("/api/books/%d/ch/0" % src["id"], token=tok_a)
    check("书源书章节（允许上游失败）", st in (200, 404), str(st))

    print("D. 书源增删改 + 导入导出")
    st, h, b = call("/api/sources/export?only=0", token=tok_a)
    hd = {k.lower(): v for k, v in h.items()}
    check("导出带 attachment", st == 200 and "attachment" in (hd.get("content-disposition") or ""),
          str(hd)[:100])
    check("导出是数组", isinstance(j(b), list) and len(j(b)) == 31, str(len(j(b)) if isinstance(j(b), list) else b[:60]))
    st, _, b = call("/api/sources/import", "POST",
                    raw=json.dumps([{"bookSourceName": "临时源", "bookSourceUrl": "https://x.test"}],
                                   ensure_ascii=False).encode(), token=tok_b)
    check("B 导入自己的源", st == 200 and j(b).get("added") == 1, str(b[:120]))
    st, _, b = call("/api/sources", token=tok_b)
    ids = [x["id"] for x in j(b).get("sources") or []]
    st, _, b = call("/api/sources/delete", "POST", {"ids": ids}, token=tok_b)
    check("B 批量删除自己的源", j(b).get("deleted") == 1, str(b[:120]))
    st, _, b = call("/api/sources", token=tok_a)
    check("A 的 31 源没被动", len(j(b).get("sources") or []) == 31, str(len(j(b).get("sources") or [])))

    print("E. 设置 / Key / TTS 配额")
    st, _, b = call("/api/settings", "PUT", {"tts": {"provider": "mimo", "voice": "bob-v"}}, token=tok_b)
    st, _, b = call("/api/settings", token=tok_a)
    check("B 改设置不影响 A", (j(b).get("tts") or {}).get("voice") != "bob-v",
          str((j(b).get("tts") or {}).get("voice")))
    secret = "sk-bob-super-secret-abcdef3456"
    st, _, b = call("/api/keys", "POST", {"provider": "mimo", "key": secret}, token=tok_b)
    check("存 key 只回掩码", st == 200 and "****" in (j(b).get("masked") or "") and secret not in str(j(b)),
          str(b[:120]))
    st, _, b = call("/api/keys", token=tok_a)
    check("A/B key 互相独立", (j(b).get("keys") or {}).get("mimo", {}).get("masked")
          != j(call("/api/keys", token=tok_b)[2]).get("keys", {}).get("mimo", {}).get("masked"))
    blob = " ".join(r[0] for r in sqlite3.connect(os.path.join(TMP, "reader.db"))
                    .execute("SELECT cipher FROM user_keys"))
    check("库里无明文 key", secret not in blob and len(blob) > 40, "len=%d" % len(blob))
    st, _, b = call("/api/presets", token=tok_b)
    check("预设里 key 是掩码", "****" in (j(b).get("mimo_key_masked") or ""), str(j(b).get("mimo_key_masked")))
    codes = [call("/api/tts/test", "POST", {"text": "x"}, token=tok_b)[0] for _ in range(12)]
    check("TTS 测试接口限流生效", 429 in codes, str(codes))

    print("F. 线上 8848 鉴权闸门")
    st, h, b = call("/", base=LIVE)
    check("/ 未登录 → 跳登录", st in (200, 303) and ("login" in (h.get("location") or "") or b"authForm" in b), str(st))
    check("/login → 200", call("/login", base=LIVE)[0] == 200)
    check("/api/books 未登录 → 401", call("/api/books", base=LIVE)[0] == 401)
    check("/static/app.js → 200", call("/static/app.js", base=LIVE)[0] == 200)
    check("/api/docs → 200", call("/api/docs", base=LIVE)[0] == 200)
    st, _, b = call("/api/auth/login", "POST", {"username": "nobody-here", "password": "wrong-pass-1"},
                    base=LIVE)
    check("线上错误登录 → 401", st == 401, str(st))

    print("\n结果：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        if proc:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
