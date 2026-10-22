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


def snapshot(dst):
    """WAL 库不能直接拷主文件（未 checkpoint 的写入会漏），用 backup API 取一致快照。"""
    src = sqlite3.connect(os.path.join(ROOT, "data/reader.db"))
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()


def reset_to_unclaimed(db):
    """把库退回“没人注册过”的状态：清账号/令牌/Key，所有数据归 user_id=0。

    线上库可能早已被首账号认领，也可能本来就是 0 —— 两种都要能跑，所以：
    books 直接改 0；sources/settings 是复合主键 (user_id, id)，用 OR REPLACE 让
    主键冲突的旧 user_id=0 行让位，绝不能先 DELETE user_id=0（那会把自己删空）。
    """
    c = sqlite3.connect(db)
    for t in ("tokens", "user_keys", "users"):
        c.execute("DELETE FROM %s" % t)
    c.execute("UPDATE books SET user_id=0")
    for t in ("sources", "settings"):
        c.execute("UPDATE OR REPLACE %s SET user_id=0" % t)
    c.commit()
    c.close()


def import_txt(tok, name="自检用书.txt"):
    """造一本两章的 TXT 进书架，返回新书 id。线上书架被清空时靠它兜底。"""
    body = ("第一章 甲\n" + "正文甲。" * 40 + "\n\n第二章 乙\n" + "正文乙。" * 40 + "\n")
    st, _, b = call("/api/import?filename=" + urllib.parse.quote(name), "POST",
                    raw=body.encode(), ctype="text/plain", token=tok)
    return j(b).get("id")


def setup():
    """拷真库 → 退回“未认领”状态（清账号、数据归 user_id=0）→ 迁移 → 假 key + 缓存样本。

    线上库可能早就被首账号认领过了，所以这里主动重置，脚本才能反复跑。
    """
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "tts"))
    db = os.path.join(TMP, "reader.db")
    snapshot(db)
    reset_to_unclaimed(db)
    bins = []
    for d, _, fs in os.walk(os.path.join(ROOT, "data/tts")):
        bins += [os.path.join(d, f) for f in fs if f.endswith(".bin")]
    bins = bins[:20]
    for f in bins:
        shutil.copy(f, os.path.join(TMP, "tts", os.path.basename(f)))
    with open(os.path.join(TMP, "mimo.key"), "w") as f:
        f.write("sk-fake-legacy-key-9999\n")
    os.chmod(os.path.join(TMP, "mimo.key"), 0o600)
    subprocess.run([sys.executable, "tools/migrate_multiuser.py", "--db",
                    os.path.join(TMP, "reader.db")], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    c = sqlite3.connect(os.path.join(TMP, "reader.db"))
    meta = {"books": c.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "sources": c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "chapters": c.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]}
    c.close()
    return len(bins), meta


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
    nfiles, meta = setup()
    proc = start()
    print("环境：%s（真库副本：%d 书 / %d 章 / %d 源 + %d 个缓存 + 假 key）"
          % (TMP, meta["books"], meta["chapters"], meta["sources"], nfiles))

    print("A. 首账号认领")
    st, _, b = call("/api/auth/register", "POST", {"username": "主人", "password": "owner-pass-123"})
    d = j(b)
    cl = d.get("claimed") or {}
    tok_a = d.get("token", "")
    check("注册并返回 token", st == 200 and tok_a, str(st))
    # 线上库的书/源数量会变（用户会删书、加源），断言跟着快照走，脚本才能反复跑
    check("认领全部书（%d 本）" % meta["books"], cl.get("books") == meta["books"], str(cl))
    check("认领全部书源（%d 个）" % meta["sources"], cl.get("sources") == meta["sources"], str(cl))
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
    check("A 有书 / B 无书", len(books_a) == meta["books"] and len(j(b).get("books") or []) == 0,
          "%d/%d" % (len(books_a), len(j(b).get("books") or [])))
    st, _, b = call("/api/sources", token=tok_a)
    srcs_a = j(b).get("sources") or []
    st, _, b = call("/api/sources", token=tok_b)
    check("A 有 %d 源 / B 无源" % meta["sources"],
          len(srcs_a) == meta["sources"] and len(j(b).get("sources") or []) == 0,
          "%d/%d" % (len(srcs_a), len(j(b).get("sources") or [])))
    # 线上书架可能被用户清空 / 书源被删光，缺什么就自己造一份，脚本才能反复跑
    tmp_guard = None
    if not books_a:
        tmp_guard = import_txt(tok_a)
        books_a = j(call("/api/books", token=tok_a)[2]).get("books") or []
        check("书架为空时自动导入一本", bool(tmp_guard) and bool(books_a), str(tmp_guard))
    if not srcs_a:
        call("/api/sources/import", "POST",
             raw=json.dumps([{"bookSourceName": "自检源", "bookSourceUrl": "https://x.test"}],
                            ensure_ascii=False).encode(), token=tok_a)
        srcs_a = j(call("/api/sources", token=tok_a)[2]).get("sources") or []
        check("书源为空时自动导入一个", bool(srcs_a), str(len(srcs_a)))
    n_srcs = len(srcs_a)
    bid = books_a[0]["id"]
    sid = srcs_a[0]["id"]
    check("B 读 A 的书 → 404", call("/api/books/%d" % bid, token=tok_b)[0] == 404)
    check("B 读 A 章节 → 404", call("/api/books/%d/ch/0" % bid, token=tok_b)[0] == 404)
    check("B 单本删 A 书 → 404", call("/api/books/%d" % bid, "DELETE", token=tok_b)[0] == 404)
    check("B 删 A 源 → 404", call("/api/sources/%s" % sid, "DELETE", token=tok_b)[0] == 404)
    check("B 切 A 源 → 404", call("/api/sources/%s/toggle" % sid, "POST", token=tok_b)[0] == 404)
    check("B 搜 A 正文 → 0 命中",
          len(j(call("/api/books/%d/search?q=的" % bid, token=tok_b)[2]).get("hits") or []) == 0)

    print("C. 阅读链路（A 的真实数据）")
    txt = next((x for x in books_a if x.get("fmt") != "src"), None)
    tmp_c = None
    if not txt:
        # 线上书可能全是书源书，这里自己造一本，别让脚本靠数据形状
        tmp_c = import_txt(tok_a)
        books_a = j(call("/api/books", token=tok_a)[2]).get("books") or []
        txt = next((x for x in books_a if x["id"] == tmp_c), None)
        check("线上无 TXT 书时自动导入一本", bool(tmp_c) and txt is not None, str(tmp_c))
    st, _, b = call("/api/books/%d" % txt["id"], token=tok_a)
    check("书详情", st == 200 and j(b).get("title") == txt["title"], str(st))
    st, _, b = call("/api/books/%d/ch/0" % txt["id"], token=tok_a)
    d = j(b)
    check("章节正文", st == 200 and len(d.get("paras") or []) >= 1,
          "%s paras=%s" % (st, len(d.get("paras") or [])))
    check("章节标题", bool(d.get("title")), str(d.get("title"))[:30])
    kw = next((ch for ch in "".join(d.get("paras") or [])
               if ch not in "。，、！？：；“”‘’（）【】 \n\t"), "的")
    st, _, b = call("/api/books/%d/search?q=%s" % (txt["id"], urllib.parse.quote(kw)), token=tok_a)
    check("全书搜索有命中", len(j(b).get("hits") or []) > 0, "%s kw=%s" % (st, kw))
    st, _, b = call("/api/books/%d/progress" % txt["id"], "POST", {"ch": 3, "off": 0.5}, token=tok_a)
    st, _, b = call("/api/books", token=tok_a)
    nb = next(x for x in (j(b).get("books") or []) if x["id"] == txt["id"])
    check("进度写入并回读", int(nb.get("progress_ch") or -1) == 3, str(nb.get("progress_ch")))
    src = next((x for x in books_a if x.get("fmt") == "src"), None)
    if src:
        st, _, b = call("/api/books/%d/ch/0" % src["id"], token=tok_a)
        check("书源书章节（允许上游失败）", st in (200, 404), str(st))
    for tid in [x for x in (tmp_c, tmp_guard) if x]:
        call("/api/books/%d" % tid, "DELETE", token=tok_a)

    print("D. 书源增删改 + 导入导出")
    st, h, b = call("/api/sources/export?only=0", token=tok_a)
    hd = {k.lower(): v for k, v in h.items()}
    check("导出带 attachment", st == 200 and "attachment" in (hd.get("content-disposition") or ""),
          str(hd)[:100])
    check("导出是数组", isinstance(j(b), list) and len(j(b)) == n_srcs,
          str(len(j(b)) if isinstance(j(b), list) else b[:60]))
    st, _, b = call("/api/sources/import", "POST",
                    raw=json.dumps([{"bookSourceName": "临时源", "bookSourceUrl": "https://x.test"}],
                                   ensure_ascii=False).encode(), token=tok_b)
    check("B 导入自己的源", st == 200 and j(b).get("added") == 1, str(b[:120]))
    st, _, b = call("/api/sources", token=tok_b)
    ids = [x["id"] for x in j(b).get("sources") or []]
    st, _, b = call("/api/sources/delete", "POST", {"ids": ids}, token=tok_b)
    check("B 批量删除自己的源", j(b).get("deleted") == 1, str(b[:120]))
    st, _, b = call("/api/sources", token=tok_a)
    check("A 的 %d 源没被动" % n_srcs,
          len(j(b).get("sources") or []) == n_srcs,
          str(len(j(b).get("sources") or [])))

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

    print("F. 书架删除（批量 + 级联 + 越权）")
    txt = ("第一章 甲\n" + "正文甲。" * 40 + "\n\n第二章 乙\n" + "正文乙。" * 40 +
           "\n\n第三章 丙\n" + "正文丙。" * 40 + "\n")
    st, _, b = call("/api/import?filename=" + urllib.parse.quote("临时书.txt"), "POST",
                    raw=txt.encode(), ctype="text/plain", token=tok_a)
    tmp_id = j(b).get("id")
    check("导入临时书", st == 200 and tmp_id, str(st) + str(b[:80]))
    st, _, b = call("/api/books/%s/ch/0" % tmp_id, token=tok_a)
    check("临时书有章节", st == 200 and len(j(b).get("paras") or []) > 0, str(st))
    st, _, b = call("/api/books/delete", "POST", {"ids": [tmp_id, 999999]}, token=tok_b)
    check("B 删 A 的书 → deleted:0", j(b).get("deleted") == 0, str(b[:80]))
    check("B 删完 A 的书还在", call("/api/books/%s" % tmp_id, token=tok_a)[0] == 200)
    st, _, b = call("/api/books/delete", "POST", {"ids": [tmp_id, 999999]}, token=tok_a)
    check("A 批量删除 → 只删自己的 1 本", j(b).get("deleted") == 1, str(b[:80]))
    check("删后书 404", call("/api/books/%s" % tmp_id, token=tok_a)[0] == 404)
    ch_left = sqlite3.connect(os.path.join(TMP, "reader.db")).execute(
        "SELECT COUNT(*) FROM chapters WHERE book_id=?", (tmp_id,)).fetchone()[0]
    check("章节级联删除干净", ch_left == 0, "剩 %d" % ch_left)
    check("删空 id 列表 → 400", call("/api/books/delete", "POST", {"ids": []}, token=tok_a)[0] == 400)

    print("H. App 支持：版本 / 书架分页 / 设备管理 / token 续期")
    st, _, b = call("/api/version")          # 不带 token，模拟 App 启动前的检查
    d = j(b)
    check("/api/version 免登录可访问", st == 200 and d.get("version") and d.get("min_version"), str(st))
    check("/api/version 带 api 版本号", d.get("api") == 1, str(d))

    # 线上书架可能只剩 1 本，先造 3 本再测分页，结果才不依赖真实数据
    base_total = len(j(call("/api/books", token=tok_a)[2]).get("books") or [])
    made = []
    for i in range(3):
        body = ("第一章 甲\n" + "正文甲。" * 30 + "\n\n第二章 乙\n" + "正文乙。" * 30 + "\n")
        st, _, b = call("/api/import?filename=" + urllib.parse.quote("分页书%d.txt" % i), "POST",
                        raw=body.encode(), ctype="text/plain", token=tok_a)
        made.append(j(b).get("id"))
    total_now = base_total + 3
    st, _, b = call("/api/books?limit=2&offset=0", token=tok_a)
    d = j(b)
    check("书架分页 limit=2 → 2 本", len(d.get("books") or []) == 2, str(len(d.get("books") or [])))
    check("分页同时回 total=%d" % total_now, d.get("total") == total_now, str(d.get("total")))
    st, _, b = call("/api/books?limit=2&offset=%d" % (total_now - 1), token=tok_a)
    check("分页 offset 到底 → 1 本", len(j(b).get("books") or []) == 1,
          str(len(j(b).get("books") or [])))
    st, _, b = call("/api/books", token=tok_a)
    check("不分页仍返回全部（网页端不受影响）", len(j(b).get("books") or []) == total_now,
          str(len(j(b).get("books") or [])))
    st, _, b = call("/api/books/delete", "POST", {"ids": made}, token=tok_a)
    check("清掉分页用的临时书", j(b).get("deleted") == 3, str(b[:80]))

    st, _, b = call("/api/auth/login", "POST",
                    {"username": "bob", "password": "bob-pass-12345",
                     "device": "Pixel 8 / App 1.0"})
    tok_b2 = j(b).get("token", "")
    check("登录可带 device 字段", st == 200 and tok_b2, str(st))
    st, _, b = call("/api/auth/sessions", token=tok_b2)
    ss = j(b).get("sessions") or []
    check("会话列表 ≥2 条", len(ss) >= 2, str(len(ss)))
    check("当前会话被标记 current", any(x.get("current") for x in ss), str(ss)[:120])
    check("列表显示设备名", any("Pixel" in (x.get("device") or "") for x in ss), str(ss)[:120])
    check("sid 是 12 位且不等于 token",
          all(len(x["sid"]) == 12 and x["sid"] != tok_b2 for x in ss), str(ss)[:120])
    other = next(x["sid"] for x in ss if not x["current"])
    st, _, b = call("/api/auth/sessions/" + other, "DELETE", token=tok_b2)
    check("按 sid 踢掉另一个设备", st == 200 and j(b).get("revoked") == 1, str(b[:80]))
    check("sid 太短 → 404（不误删）",
          call("/api/auth/sessions/abc", "DELETE", token=tok_b2)[0] == 404)
    check("别人的 sid 删不动",
          call("/api/auth/sessions/" + other, "DELETE", token=tok_a)[0] == 404)

    st, _, b = call("/api/auth/refresh", "POST", token=tok_b2)
    tok_b3 = j(b).get("token", "")
    check("refresh 返回新 token", st == 200 and tok_b3 and tok_b3 != tok_b2, str(st))
    check("旧 token 立刻失效", call("/api/auth/me", token=tok_b2)[0] == 401)
    check("新 token 可用", call("/api/auth/me", token=tok_b3)[0] == 200)
    check("未登录 refresh → 401", call("/api/auth/refresh", "POST")[0] == 401)
    st, _, b = call("/api/auth/me", token=tok_b3)
    check("/api/auth/me 回 expires_at", (j(b).get("expires_at") or 0) > time.time(), str(j(b)))

    st, _, b = call("/api/auth/login", "POST", {"username": "bob", "password": "bob-pass-12345"})
    tok_b4 = j(b).get("token", "")
    st, _, b = call("/api/auth/logout_all", "POST", token=tok_b4)
    check("logout_all 踢掉其它设备", st == 200 and j(b).get("revoked") >= 1, str(b[:80]))
    check("logout_all 保留当前会话", call("/api/auth/me", token=tok_b4)[0] == 200)
    check("被踢的 token 已失效", call("/api/auth/me", token=tok_b3)[0] == 401)

    print("G. 线上 8848 鉴权闸门")
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
