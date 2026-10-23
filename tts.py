# -*- coding: utf-8 -*-
"""服务端 TTS 代理：模板渲染 + 缓存 + 代理转发

放在服务端做，而不是浏览器直连，原因有三：
1. 绕开 CORS（api.fish.audio 不会给网页发 Access-Control-Allow-Origin）
2. API key 不用暴露给前端
3. 本机需要走 127.0.0.1:7890 代理，浏览器直接连会 Network unreachable
"""
import base64
import hashlib
import json
import os

import keys
import paths
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

CACHE_DIR = paths.TTS_CACHE
DEFAULT_PROXY = "http://127.0.0.1:7890"

# ---------------- 小米 MiMo TTS ----------------
# OpenAI 兼容接口：POST /v1/chat/completions，audio 参数决定音色/格式，
# 返回 JSON（choices[0].message.audio.data 是 base64 音频），不是二进制流。
MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-tts"
MIMO_KEY_FILE = paths.LEGACY_MIMO_KEY
MIMO_STYLE = "自然、平稳的朗读语气，语速适中，适合长篇小说朗读"
MIMO_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}

# 三种模型：预置音色 / 音色设计（文字描述生成音色）/ 音色克隆（音频样本复刻）
MIMO_MODELS = {
    "preset": "mimo-v2.5-tts",
    "design": "mimo-v2.5-tts-voicedesign",
    "clone": "mimo-v2.5-tts-voiceclone",
}
MIMO_MODES = [
    {"key": "preset", "label": "预置音色", "model": MIMO_MODELS["preset"],
     "hint": "官方精品音色列表，开箱可用"},
    {"key": "design", "label": "音色设计", "model": MIMO_MODELS["design"],
     "hint": "用一两句话描述想要的音色，模型现造一个；合成速度接近预置"},
    {"key": "clone", "label": "音色克隆", "model": MIMO_MODELS["clone"],
     "hint": "上传一段干净人声样本复刻音色；上游较慢，单句常要几十秒，已开缓存"},
]
MIMO_DESIGN_DEFAULT = ("五十多岁的男性评书先生，北方口音，嗓音醇厚略带沙哑，"
                       "语速舒缓、抑扬顿挫分明，像在茶馆里讲长篇故事。")
# 克隆样本：mp3/wav，base64 后要 <10MB（原文约 ≤7MB）
MIMO_CLONE_DIR = paths.MIMO_CLONES
MIMO_CLONE_MAX = 7 * 1024 * 1024
MIMO_CLONE_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav"}

# 官方预置音色（文档「预置音色列表」）
MIMO_VOICES = [
    {"id": "mimo_default", "name": "MiMo 默认", "lang": "中/英", "gender": ""},
    {"id": "冰糖", "name": "冰糖", "lang": "中文", "gender": "女"},
    {"id": "茉莉", "name": "茉莉", "lang": "中文", "gender": "女"},
    {"id": "苏打", "name": "苏打", "lang": "中文", "gender": "男"},
    {"id": "白桦", "name": "白桦", "lang": "中文", "gender": "男"},
    {"id": "Mia", "name": "Mia", "lang": "英文", "gender": "女"},
    {"id": "Chloe", "name": "Chloe", "lang": "英文", "gender": "女"},
    {"id": "Milo", "name": "Milo", "lang": "英文", "gender": "男"},
    {"id": "Dean", "name": "Dean", "lang": "英文", "gender": "男"},
]


def mimo_key(uid):
    """小米 API Key：优先环境变量，其次该用户加密存的 key，最后兼容旧全局 data/mimo.key。

    明文只在服务端内存里出现，不回前端、不进日志。
    """
    k = (os.environ.get("MIMO_API_KEY") or "").strip()
    if k:
        return k
    try:
        k = keys.get_key(uid, "mimo")
        if k:
            return k
    except Exception:
        pass
    if int(uid) == 0:  # 迁移前的老库/老进程兜底
        try:
            with open(MIMO_KEY_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def save_mimo_key(uid, key):
    key = (key or "").strip()
    if not key:
        raise TTSError("Key 不能为空")
    keys.set_key(uid, "mimo", key)


def mimo_ready(uid):
    return bool(mimo_key(uid))


def cache_dir(uid):
    """TTS 缓存按用户分目录，A 的音频不会被 B 命中。"""
    d = os.path.join(CACHE_DIR, str(int(uid)))
    os.makedirs(d, exist_ok=True)
    return d


def cache_stats(uid):
    d = cache_dir(uid)
    files = [f for f in os.listdir(d) if f.endswith(".bin")]
    size = sum(os.path.getsize(os.path.join(d, f)) for f in files)
    return {"files": len(files), "bytes": size}


def cache_clear(uid):
    d = cache_dir(uid)
    for f in os.listdir(d):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return True


def is_mimo(cfg):
    prov = str(cfg.get("provider") or "").strip().lower()
    if prov:
        return prov in ("mimo", "xiaomi", "xiaomi-mimo", "mimo-tts")
    return "xiaomimimo.com" in str(cfg.get("url") or "")


# ---------------- 克隆样本库（data/mimo_clones，gitignore 覆盖） ----------------

def clone_dir(uid):
    d = os.path.join(MIMO_CLONE_DIR, str(int(uid)))
    os.makedirs(d, exist_ok=True)
    return d


def _clone_index_path(uid):
    return os.path.join(clone_dir(uid), "index.json")


def _clone_index(uid):
    try:
        with open(_clone_index_path(uid), "r", encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, list) else []
    except (OSError, ValueError):
        return []


def _clone_write_index(uid, items):
    os.makedirs(clone_dir(uid), exist_ok=True)
    p = _clone_index_path(uid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def clone_voices(uid):
    """已保存的克隆样本（只回名字/大小，路径不出服务端）。顺手清掉文件已丢的脏条目。"""
    out, items, dirty = [], _clone_index(uid), False
    d = clone_dir(uid)
    for it in items:
        f = os.path.join(d, str(it.get("file") or ""))
        if os.path.isfile(f):
            out.append({"name": it.get("name") or "", "file": it.get("file"),
                        "bytes": os.path.getsize(f), "ext": it.get("ext") or "mp3",
                        "added": it.get("added") or 0})
        else:
            dirty = True
    if dirty:
        try:
            _clone_write_index(uid, [i for i in items
                                if os.path.isfile(os.path.join(d, str(i.get("file") or "")))])
        except OSError:
            pass
    out.sort(key=lambda x: x.get("added") or 0)
    return out


def save_clone_voice(uid, name, data, filename=""):
    """存一段样本（mp3/wav）。同名覆盖，旧文件顺手删掉。"""
    name = (name or "").strip() or "未命名样本"
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if ext not in MIMO_CLONE_MIME:
        ext = "wav" if data[:4] == b"RIFF" else "mp3"
    if not data:
        raise TTSError("样本是空的")
    if len(data) < 4000:
        raise TTSError("样本太小（%d 字节），至少给几秒干净人声" % len(data))
    if len(data) > MIMO_CLONE_MAX:
        raise TTSError("样本太大（%.1f MB），base64 后要 <10MB，请压到 %.0f MB 以内"
                       % (len(data) / 1048576.0, MIMO_CLONE_MAX / 1048576.0))
    d = clone_dir(uid)
    # 文件名带名字哈希：两个人起不同名字但传同一段音频，也不会互相删掉
    fn = hashlib.sha256((name + "\0").encode("utf-8") + data).hexdigest()[:16] + "." + ext
    with open(os.path.join(d, fn), "wb") as f:
        f.write(data)
    items = _clone_index(uid)
    for old in [i for i in items if i.get("name") == name]:
        if old.get("file") and old["file"] != fn:
            _drop_file_if_unused(uid, old["file"], items, skip_name=name)
    items = [i for i in items if i.get("name") != name]
    items.append({"name": name, "file": fn, "ext": ext, "added": int(time.time())})
    _clone_write_index(uid, items)
    return {"name": name, "file": fn, "bytes": len(data), "ext": ext}


def _drop_file_if_unused(uid, fn, items, skip_name=None):
    """还有别的样本条目引用同一个文件时不能删（同名不同内容/同内容不同名都可能撞）。"""
    for i in items:
        if i.get("file") == fn and i.get("name") != skip_name:
            return False
    try:
        os.remove(os.path.join(clone_dir(uid), str(fn)))
    except OSError:
        pass
    return True


def delete_clone_voice(uid, name):
    items = _clone_index(uid)
    keep, gone = [], None
    for i in items:
        if i.get("name") == name:
            gone = i
        else:
            keep.append(i)
    if not gone:
        raise TTSError("没有这个克隆样本：%s" % name)
    _drop_file_if_unused(uid, gone.get("file") or "", items, skip_name=name)
    _clone_write_index(uid, keep)
    return True


def _clone_data_uri(uid, name):
    """返回 (data:audio/...;base64,..., 指纹)。指纹进缓存 key，换样本自动失效。"""
    name = (name or "").strip()
    if not name:
        raise TTSError("音色克隆模式要先选一个样本（或上传一段 mp3/wav）")
    d = clone_dir(uid)
    for it in _clone_index(uid):
        if it.get("name") != name:
            continue
        p = os.path.join(d, str(it.get("file") or ""))
        if not os.path.isfile(p):
            break
        data = open(p, "rb").read()
        if not data:
            break
        ext = str(it.get("ext") or "mp3").lower()
        mime = MIMO_CLONE_MIME.get(ext, "audio/mpeg")
        fp = hashlib.sha256(data).hexdigest()[:16]
        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii")), fp
    raise TTSError("找不到克隆样本「%s」，请重新上传" % name)


def mimo_mode(cfg):
    """preset / design / clone。显式 mimoMode 优先；老配置只有 model 时按 model 认。"""
    mode = str(cfg.get("mimoMode") or cfg.get("mimo_mode") or "").strip().lower()
    if mode in MIMO_MODELS:
        return mode
    model = str(cfg.get("model") or "").strip().lower()
    for k, m in MIMO_MODELS.items():
        if model == m:
            return k
    return "preset"


class TTSError(Exception):
    def __init__(self, msg, status=0, body="", detail=""):
        super().__init__(msg)
        self.msg, self.status, self.body, self.detail = msg, status, body, detail


def _json_esc(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)[1:-1]


def _xml_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(tpl: str, text: str, voice: str, speed: float) -> str:
    if not tpl:
        return ""
    vals = {
        "text": _json_esc(text),
        "text_raw": text,
        "text_xml": _xml_esc(text),
        "voice": _json_esc(voice or ""),
        "voice_raw": voice or "",
        "voice_url": urllib.parse.quote(voice or "", safe=""),
        "speed": ("%.2f" % float(speed)).rstrip("0").rstrip("."),
        "speed_pct": str(int(round((float(speed) - 1) * 100))),
        "ts": str(int(time.time() * 1000)),
    }

    def rep(m):
        k = m.group(1).strip()
        return vals.get(k, m.group(0))

    return re.sub(r"\{\{([a-zA-Z_]+)\}\}", rep, tpl)


def cache_key(cfg, text):
    raw = json.dumps(
        [
            cfg.get("method", "POST").upper(),
            cfg.get("url", ""),
            cfg.get("headers", ""),
            cfg.get("body", ""),
            text,
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cached(uid, key):
    p = os.path.join(cache_dir(uid), key + ".bin")
    if os.path.exists(p):
        meta = p + ".json"
        try:
            m = json.load(open(meta))
        except Exception:
            m = {}
        return open(p, "rb").read(), m.get("content_type", "audio/mpeg")
    return None


def _save(uid, key, data, ctype):
    d = cache_dir(uid)
    with open(os.path.join(d, key + ".bin"), "wb") as f:
        f.write(data)
    with open(os.path.join(d, key + ".bin.json"), "w") as f:
        json.dump({"content_type": ctype, "at": time.time()}, f)


_SPEAK_RE = re.compile(r"[0-9A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _send(url, data, headers, method, cfg, timeout):
    """按 cfg.proxy 发请求；代理连不上时自动退回直连。返回 (raw, content_type, 实际用的代理)。

    典型场景：设置里配了本机 127.0.0.1:7890 的代理，服务部署到别的机器后那台机器
    没有这个代理，直连其实通 —— 这时不该让用户看到「连不上上游」。
    上游明确回了 HTTP 状态（HTTPError）就照原样抛，不换路，免得掩盖真实错误。

    注意：Request 必须每轮重建。urllib 走代理时会调 req.set_proxy() 改写 req.host，
    复用同一个对象会把第二次「直连」又打到刚才那个代理上。
    """
    proxy = (cfg.get("proxy") or "").strip()
    plan = [proxy, ""] if proxy else [""]
    last = None
    for i, p in enumerate(plan):
        handlers = [urllib.request.ProxyHandler({"http": p, "https": p} if p else {})]
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", ""), p
        except urllib.error.HTTPError as e:
            setattr(e, "used_proxy", p)  # 让上层知道这次是走代理还是直连得到的应答
            raise
        except urllib.error.URLError as e:
            last = e
            if p and i == 0:
                continue
            raise
        except Exception as e:
            last = e
            if p and i == 0:
                continue
            raise
    raise last if last else TTSError("请求失败")


def _unreachable_hint(url, reason):
    """把「连不上」翻译成能动手的建议：出口问题 / 代理问题 / DNS 问题。"""
    host = urllib.parse.urlparse(url).hostname or url
    r = str(reason)
    if "Name or service not known" in r or "Temporary failure in name resolution" in r:
        return f"服务器解析不了 {host}（DNS 有问题）"
    if "Network is unreachable" in r:
        return f"服务器没有到 {host} 的可用路由（多半是 IPv6 优先但没 IPv6 出口）"
    if "timed out" in r or "timeout" in r:
        return f"服务器连 {host} 超时（境外服务器访问国内接口常见，给服务配 HTTPS_PROXY 走国内代理即可）"
    if "Connection refused" in r:
        return f"服务器到 {host} 的连接被拒绝（如果配了代理，检查代理还活着没）"
    return f"服务器连不上 {host}"



def clean_text(t):
    """朗读前的文本清洗：去零宽字符、合并省略号/破折号、压空白。"""
    t = str(t or "")
    t = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", t)
    t = re.sub(r"[.．]{3,}", "…", t)
    t = re.sub(r"…{2,}", "…", t)
    t = re.sub(r"[—–―]{2,}", "——", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def synthesize(uid, cfg, text, voice=None, speed=None, use_cache=True):
    """cfg: {url, method, headers, body, proxy, contentType}"""
    voice = voice if voice is not None else cfg.get("voice", "")
    speed = float(speed if speed is not None else cfg.get("speed", 1.0) or 1.0)
    speed = max(0.5, min(2.0, speed))

    text = clean_text(text)
    # 纯标点 / 纯符号的片段直接拒掉，别送给上游（会 400 或念出噪声）
    if not _SPEAK_RE.search(text):
        raise TTSError("这段没有可朗读的文字（纯标点或符号），已跳过", 400)

    # 小米 MiMo：返回 JSON+base64，走独立分支，不走模板渲染
    if is_mimo(cfg):
        return synthesize_mimo(uid, cfg, text, voice, use_cache=use_cache)

    url = render(cfg.get("url", ""), text, voice, speed).strip()
    if not url.startswith("http"):
        raise TTSError("接口地址没填或格式不对（必须以 http 开头）")

    key = cache_key({**cfg, "url": url}, text)
    if use_cache:
        hit = cached(uid, key)
        if hit:
            return hit[0], hit[1], True

    method = (cfg.get("method") or "POST").upper()
    hdr_raw = render(cfg.get("headers", "") or "{}", text, voice, speed)
    try:
        headers = json.loads(hdr_raw) if hdr_raw.strip() else {}
    except json.JSONDecodeError as e:
        raise TTSError(f"请求头不是合法 JSON：{e}", detail=hdr_raw[:300])
    if not isinstance(headers, dict):
        raise TTSError("请求头必须是 JSON 对象")

    # 预设里的占位符 "你的KEY" 换成这个用户自己存的 key：每人一份，明文不落库、不进日志。
    # 没配置时给一句人话，别把 urllib 的 latin-1 编码异常直接抛给前端。
    if any("你的KEY" in str(v) for v in headers.values()):
        prov = str(cfg.get("provider") or "").strip()
        ukey = keys.get_key(uid, prov) if prov else ""
        if not ukey:
            pname = (PRESETS.get(prov) or {}).get("name") or "这个朗读接口"
            raise TTSError("%s 的 API Key 还没配置：在「朗读设置」里填入自己的 key（只存服务端）" % pname, 400)
        headers = {hk: (str(hv).replace("你的KEY", ukey) if isinstance(hv, str) else hv)
                   for hk, hv in headers.items()}

    data = None
    if method != "GET":
        body = render(cfg.get("body", "") or "", text, voice, speed)
        data = body.encode("utf-8")
        if "Content-Type" not in headers and "content-type" not in headers:
            headers["Content-Type"] = "application/json"

    try:
        audio, ctype, used_proxy = _send(url, data, headers, method, cfg,
                                         float(cfg.get("timeout") or 60))
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode("utf-8", "replace")
        raise TTSError(f"上游返回 HTTP {e.code}", e.code, body)
    except urllib.error.URLError as e:
        raise TTSError(f"连不上上游：{e.reason}｜{_unreachable_hint(url, e.reason)}",
                       detail=str(e.reason))
    except Exception as e:
        raise TTSError(f"请求失败：{e}")
    if used_proxy != (cfg.get("proxy") or "").strip():
        print(f"[tts] 代理 {cfg.get('proxy')} 不可用，已改直连 {url}", flush=True)

    if not audio:
        raise TTSError("上游返回了 0 字节音频", 200, "")
    low = ctype.lower()
    if low.startswith("application/json") or low.startswith("text/"):
        raise TTSError(
            "上游返回的不是音频，是文本（多半是报错信息或 key 无效）",
            200,
            audio[:600].decode("utf-8", "replace"),
        )
    if len(audio) < 1200:
        raise TTSError(f"音频只有 {len(audio)} 字节，几乎肯定是错误响应", 200,
                       audio[:400].decode("utf-8", "replace"))
    if not low.startswith("audio"):
        ctype = "audio/mpeg"

    if use_cache:
        _save(uid, key, audio, ctype)
    return audio, ctype, False


# ---------------- 小米 MiMo TTS 实现 ----------------

def mimo_cache_key(mode, model, voice_key, style, text):
    raw = json.dumps(["mimo", mode, model, voice_key, style, text], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mimo_err(body):
    """从 {"error":{"code","message","param"}} 里抽出人话。"""
    try:
        j = json.loads(body) if isinstance(body, str) else body
        e = j.get("error") if isinstance(j, dict) else None
        if isinstance(e, dict):
            msg = e.get("message") or ""
            param = e.get("param") or ""
            out = msg + ("：" + param if param else "")
            return out or json.dumps(j, ensure_ascii=False)[:200]
        if isinstance(e, str):
            return e
    except Exception:
        pass
    return str(body)[:200]


def synthesize_mimo(uid, cfg, text, voice=None, use_cache=True):
    """小米 MiMo TTS：POST /v1/chat/completions，音频在 JSON 的 base64 里。

    三种模型（文档「语音合成 MiMo-V2.5-TTS 系列」）：
    - preset：mimo-v2.5-tts              → audio.voice = 预置音色名
    - design：mimo-v2.5-tts-voicedesign  → user 消息 = 音色描述，不带 voice
    - clone ：mimo-v2.5-tts-voiceclone   → audio.voice = data:audio/...;base64,<样本>
    assistant 消息永远是正文；语速不走上游参数，前端 playbackRate 变速。
    """
    key_api = mimo_key(uid)
    if not key_api:
        raise TTSError("你的小米 API Key 还没配置：在朗读设置里填入自己的 key（只存服务端）")

    mode = mimo_mode(cfg)
    model = MIMO_MODELS[mode]
    style = clean_text(cfg.get("style") or "") or MIMO_STYLE
    fmt = str(cfg.get("format") or "mp3").strip().lower()
    if fmt not in MIMO_FORMATS:
        fmt = "mp3"
    url = str(cfg.get("url") or "").strip()
    if "xiaomimimo.com" not in url:
        url = MIMO_URL

    # 每种模式下"音色"的等价物，进缓存 key，避免串音
    if mode == "preset":
        voice = str(voice or cfg.get("voice") or "冰糖").strip()
        voice_key = voice
        user_msg = style
        audio = {"format": fmt, "voice": voice}
    elif mode == "design":
        desc = str(cfg.get("mimoVoiceDesc") or cfg.get("mimo_desc") or "").strip() or MIMO_DESIGN_DEFAULT
        voice = ""
        voice_key = desc
        # 音色描述写在 user 里；用户另外改过风格指令时接在后面（同一段话术）
        user_msg = desc if style == MIMO_STYLE else (desc + "\n" + style)
        audio = {"format": fmt}
    else:
        clone_name = str(cfg.get("mimoClone") or cfg.get("mimo_clone") or "").strip()
        uri, fp = _clone_data_uri(uid, clone_name)
        voice = clone_name
        voice_key = "%s#%s" % (clone_name, fp)
        user_msg = style
        audio = {"format": fmt, "voice": uri}

    ck = mimo_cache_key(mode, model, voice_key, style, text)
    if use_cache:
        hit = cached(uid, ck)
        if hit:
            return hit[0], hit[1], True

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": text},
        ],
        "audio": audio,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key_api}

    # 克隆模式上游排队很飘（实测 3s ~ 170s），给足超时，别把正常请求判成失败
    default_timeout = 240 if mode == "clone" else 120
    try:
        raw, _ctype, used_proxy = _send(url, data, headers, "POST", cfg,
                                        float(cfg.get("timeout") or default_timeout))
    except urllib.error.HTTPError as e:
        body = e.read()[:800].decode("utf-8", "replace")
        raise TTSError("小米接口 HTTP %d：%s" % (e.code, _mimo_err(body)), e.code, body)
    except urllib.error.URLError as e:
        raise TTSError("连不上小米接口：%s｜%s" % (e.reason, _unreachable_hint(url, e.reason)),
                       detail=str(e.reason))
    except Exception as e:
        raise TTSError("请求小米接口失败：%s" % (e,))

    try:
        j = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise TTSError("小米接口返回的不是 JSON", 200, raw[:600].decode("utf-8", "replace"))
    if isinstance(j, dict) and j.get("error"):
        raise TTSError("小米接口报错：%s" % _mimo_err(j), 200,
                       json.dumps(j, ensure_ascii=False)[:600])
    try:
        b64 = j["choices"][0]["message"]["audio"]["data"]
    except Exception:
        raise TTSError("小米接口返回里没有音频字段", 200,
                       json.dumps(j, ensure_ascii=False)[:600])
    try:
        audio = base64.b64decode(b64)
    except Exception as e:
        raise TTSError("音频 base64 解不开：%s" % (e,))
    if len(audio) < 1200:
        raise TTSError("音频只有 %d 字节，几乎肯定是错误响应" % len(audio), 200)

    ctype = MIMO_FORMATS.get(fmt, "audio/mpeg")
    if use_cache:
        _save(uid, ck, audio, ctype)
    return audio, ctype, False


# 预设：{{text}} 会自动做 JSON 转义，{{voice}} 独立可改
PRESETS = {
    "mimo": {
        "name": "小米 MiMo TTS（预置/设计/克隆）",
        "provider": "mimo",
        "url": MIMO_URL,
        "method": "POST",
        "model": MIMO_MODEL,
        "headers": "",
        "body": "",
        "voice": "冰糖",
        "style": MIMO_STYLE,
        "mimoMode": "preset",
        "mimoVoiceDesc": "",
        "mimoClone": "",
        "speed": 1.0,
        "keepSpeed": True,
        "clientSpeed": True,
        "proxy": "",
        "note": "国内直连不用代理。三种音色来源：预置音色（冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean）、"
                "音色设计（文字描述现造音色）、音色克隆（上传 mp3/wav 样本复刻）。"
                "语速用浏览器变速播放（改速度不重新合成）。Key 存服务端 data/mimo.key。",
    },
    "fish": {
        "name": "Fish Audio（克隆音色）",
        "url": "https://api.fish.audio/v1/tts",
        "method": "POST",
        "headers": '{"Content-Type":"application/json","Authorization":"Bearer 你的KEY","model":"s2.1-pro-free"}',
        "body": '{"text":"{{text}}","reference_id":"{{voice}}","format":"mp3",'
                '"mp3_bitrate":128,"chunk_length":200,"normalize":true,'
                '"latency":"normal","prosody":{"speed":{{speed}}}}',
        "voice": "",
        "speed": 1.18,
        "proxy": DEFAULT_PROXY,
        "note": "音色 ID = fish.audio 音色页地址栏 /model/ 后面那串 32 位 hex",
    },
    "siliconflow": {
        "name": "硅基流动 CosyVoice2",
        "url": "https://api.siliconflow.cn/v1/audio/speech",
        "method": "POST",
        "headers": '{"Content-Type":"application/json","Authorization":"Bearer 你的KEY"}',
        "body": '{"model":"FunAudioLLM/CosyVoice2-0.5B","input":"{{text}}",'
                '"voice":"{{voice}}","response_format":"mp3","stream":false,"speed":{{speed}}}',
        "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
        "speed": 1.0,
        "proxy": "",
        "note": "国内直连，不用代理；音色如 anna / bella / alex",
    },
    "openai": {
        "name": "OpenAI 兼容 /v1/audio/speech",
        "url": "https://api.openai.com/v1/audio/speech",
        "method": "POST",
        "headers": '{"Content-Type":"application/json","Authorization":"Bearer 你的KEY"}',
        "body": '{"model":"tts-1","input":"{{text}}","voice":"{{voice}}",'
                '"response_format":"mp3","speed":{{speed}}}',
        "voice": "alloy",
        "speed": 1.0,
        "proxy": DEFAULT_PROXY,
        "note": "任何兼容接口都行（自建 one-api / new-api 等）",
    },
    "azure": {
        "name": "Azure 语音（SSML）",
        "url": "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1",
        "method": "POST",
        "headers": '{"Content-Type":"application/ssml+xml",'
                   '"X-Microsoft-OutputFormat":"audio-24khz-48kbitrate-mono-mp3",'
                   '"Ocp-Apim-Subscription-Key":"你的KEY"}',
        "body": "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
                "<voice name='{{voice_raw}}'><prosody rate='{{speed_pct}}%'>{{text_xml}}</prosody>"
                "</voice></speak>",
        "voice": "zh-CN-XiaoxiaoNeural",
        "speed": 1.0,
        "proxy": DEFAULT_PROXY,
        "note": "音色如 zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural / zh-CN-YunyangNeural",
    },
    "web": {
        "name": "浏览器自带语音（离线可用）",
        "url": "",
        "method": "GET",
        "headers": "{}",
        "body": "",
        "voice": "",
        "speed": 1.0,
        "proxy": "",
        "note": "用手机系统 TTS，零成本零配置，音色不如克隆音色自然",
    },
}


# 下拉顺序：老配置（fish）排最前，小米紧随其后
_ORDER = ["fish", "mimo", "siliconflow", "openai", "azure", "web"]
PRESETS = {k: PRESETS[k] for k in _ORDER + [x for x in PRESETS if x not in _ORDER]}


# ---------------- 连通性自检 ----------------

def diag(uid, cfg):
    """探服务器到上游的连通性：DNS → TCP(逐 IP) → HTTP。

    给「连不上上游 / 连不上小米接口」定位用：能一眼看出是 DNS 解析不了、
    IPv6 没出口、代理挂了，还是上游明确拒绝（401/403）。
    上游只要有 HTTP 应答就算「通」，不需要有效 key。
    """
    url = (cfg.get("url") or "").strip()
    if is_mimo(cfg) and not url:
        url = (PRESETS.get("mimo") or {}).get("url") or "https://api.xiaomimimo.com/v1/chat/completions"
    proxy = (cfg.get("proxy") or "").strip()
    out = {
        "provider": cfg.get("provider") or "",
        "engine": cfg.get("engine") or "",
        "url": url,
        "proxy": proxy,
        "ips": [],
        "tcp": [],
        "http": None,
        "ok": False,
        "error": "",
    }
    p = urllib.parse.urlparse(url)
    host = p.hostname
    if not host:
        out["error"] = "接口地址没填或格式不对（必须以 http 开头）"
        return out
    port = p.port or (443 if p.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        out["error"] = "DNS 解析失败：%s" % (e,)
        return out

    seen = set()
    for fam, _t, _pr, _c, sa in infos:
        ip = sa[0]
        if (fam, ip) in seen:
            continue
        seen.add((fam, ip))
        rec = {"family": "IPv4" if fam == socket.AF_INET else "IPv6",
               "ip": ip, "connect_ms": None, "ok": False, "err": ""}
        t0 = time.time()
        try:
            s = socket.create_connection((ip, port), timeout=6)
            s.close()
            rec["ok"] = True
        except Exception as e:
            rec["err"] = str(e)
        rec["connect_ms"] = int((time.time() - t0) * 1000)
        out["tcp"].append(rec)
        out["ips"].append(ip)

    try:
        raw, _ctype, used_proxy = _send(url, None, {}, "GET", cfg, 15)
        out["http"] = {"status": 200, "bytes": len(raw), "used_proxy": used_proxy}
        out["ok"] = True
    except urllib.error.HTTPError as e:
        # 401/403/405 都说明网络通了，只是没带 key 或方法不对
        out["http"] = {"status": e.code, "used_proxy": getattr(e, "used_proxy", proxy),
                       "note": "上游有应答，网络是通的"}
        out["ok"] = True
    except Exception as e:
        out["error"] = "HTTP 请求失败：%s｜%s" % (e, _unreachable_hint(url, e))
    if out["http"] and proxy and out["http"].get("used_proxy") != proxy:
        out["note"] = "配置的代理 %s 连不上，已自动退回直连" % proxy
    return out
