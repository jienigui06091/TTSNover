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
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tts")
DEFAULT_PROXY = "http://127.0.0.1:7890"

# ---------------- 小米 MiMo TTS ----------------
# OpenAI 兼容接口：POST /v1/chat/completions，audio 参数决定音色/格式，
# 返回 JSON（choices[0].message.audio.data 是 base64 音频），不是二进制流。
MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-tts"
MIMO_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mimo.key")
MIMO_STYLE = "自然、平稳的朗读语气，语速适中，适合长篇小说朗读"
MIMO_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}

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


def mimo_key():
    """小米 API Key：优先环境变量，其次 data/mimo.key（不落前端、不进 Git）。"""
    k = (os.environ.get("MIMO_API_KEY") or "").strip()
    if k:
        return k
    try:
        with open(MIMO_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_mimo_key(key):
    key = (key or "").strip()
    if not key:
        raise TTSError("Key 不能为空")
    os.makedirs(os.path.dirname(MIMO_KEY_FILE), exist_ok=True)
    fd = os.open(MIMO_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    try:
        os.chmod(MIMO_KEY_FILE, 0o600)
    except OSError:
        pass


def mimo_ready():
    return bool(mimo_key())


def is_mimo(cfg):
    prov = str(cfg.get("provider") or "").strip().lower()
    if prov:
        return prov in ("mimo", "xiaomi", "xiaomi-mimo", "mimo-tts")
    return "xiaomimimo.com" in str(cfg.get("url") or "")


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


def cached(key):
    p = os.path.join(CACHE_DIR, key + ".bin")
    if os.path.exists(p):
        meta = p + ".json"
        try:
            m = json.load(open(meta))
        except Exception:
            m = {}
        return open(p, "rb").read(), m.get("content_type", "audio/mpeg")
    return None


def _save(key, data, ctype):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, key + ".bin"), "wb") as f:
        f.write(data)
    with open(os.path.join(CACHE_DIR, key + ".bin.json"), "w") as f:
        json.dump({"content_type": ctype, "at": time.time()}, f)


_SPEAK_RE = re.compile(r"[0-9A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def clean_text(t):
    """朗读前的文本清洗：去零宽字符、合并省略号/破折号、压空白。"""
    t = str(t or "")
    t = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", t)
    t = re.sub(r"[.．]{3,}", "…", t)
    t = re.sub(r"…{2,}", "…", t)
    t = re.sub(r"[—–―]{2,}", "——", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def synthesize(cfg, text, voice=None, speed=None, use_cache=True):
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
        return synthesize_mimo(cfg, text, voice, use_cache=use_cache)

    url = render(cfg.get("url", ""), text, voice, speed).strip()
    if not url.startswith("http"):
        raise TTSError("接口地址没填或格式不对（必须以 http 开头）")

    key = cache_key({**cfg, "url": url}, text)
    if use_cache:
        hit = cached(key)
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

    data = None
    if method != "GET":
        body = render(cfg.get("body", "") or "", text, voice, speed)
        data = body.encode("utf-8")
        if "Content-Type" not in headers and "content-type" not in headers:
            headers["Content-Type"] = "application/json"

    proxy = (cfg.get("proxy") or "").strip()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with opener.open(req, timeout=float(cfg.get("timeout") or 60)) as r:
            audio = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode("utf-8", "replace")
        raise TTSError(f"上游返回 HTTP {e.code}", e.code, body)
    except urllib.error.URLError as e:
        raise TTSError(f"连不上上游：{e.reason}（检查代理设置）", detail=str(e.reason))
    except Exception as e:
        raise TTSError(f"请求失败：{e}")

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
        _save(key, audio, ctype)
    return audio, ctype, False


# ---------------- 小米 MiMo TTS 实现 ----------------

def mimo_cache_key(model, voice, style, text):
    raw = json.dumps(["mimo", model, voice, style, text], ensure_ascii=False, sort_keys=True)
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


def synthesize_mimo(cfg, text, voice=None, use_cache=True):
    """小米 MiMo TTS：POST /v1/chat/completions，音频在 JSON 的 base64 里。

    - user 消息 = 风格指令（语气/情绪），assistant 消息 = 要念的正文
    - audio.format 默认 mp3（约 64kbps，比 wav 小 7 倍，浏览器直接能播）
    - 语速不走上游参数：前端用 playbackRate 播放，改语速不必重新合成
    """
    key_api = mimo_key()
    if not key_api:
        raise TTSError("小米 API Key 未配置：把 key 写进 data/mimo.key，或设环境变量 MIMO_API_KEY")

    model = str(cfg.get("model") or MIMO_MODEL).strip()
    voice = str(voice or cfg.get("voice") or "冰糖").strip()
    style = clean_text(cfg.get("style") or "") or MIMO_STYLE
    fmt = str(cfg.get("format") or "mp3").strip().lower()
    if fmt not in MIMO_FORMATS:
        fmt = "mp3"
    url = str(cfg.get("url") or "").strip()
    if "xiaomimimo.com" not in url:
        url = MIMO_URL

    ck = mimo_cache_key(model, voice, style, text)
    if use_cache:
        hit = cached(ck)
        if hit:
            return hit[0], hit[1], True

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": style},
            {"role": "assistant", "content": text},
        ],
        "audio": {"format": fmt, "voice": voice},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key_api}

    proxy = (cfg.get("proxy") or "").strip()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=float(cfg.get("timeout") or 120)) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:800].decode("utf-8", "replace")
        raise TTSError("小米接口 HTTP %d：%s" % (e.code, _mimo_err(body)), e.code, body)
    except urllib.error.URLError as e:
        raise TTSError("连不上小米接口：%s" % (e.reason,), detail=str(e.reason))
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
        _save(ck, audio, ctype)
    return audio, ctype, False


# 预设：{{text}} 会自动做 JSON 转义，{{voice}} 独立可改
PRESETS = {
    "mimo": {
        "name": "小米 MiMo TTS（预置音色）",
        "provider": "mimo",
        "url": MIMO_URL,
        "method": "POST",
        "model": MIMO_MODEL,
        "headers": "",
        "body": "",
        "voice": "冰糖",
        "style": MIMO_STYLE,
        "speed": 1.0,
        "keepSpeed": True,
        "clientSpeed": True,
        "proxy": "",
        "note": "国内直连不用代理。音色：冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean。"
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
