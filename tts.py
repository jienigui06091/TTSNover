# -*- coding: utf-8 -*-
"""服务端 TTS 代理：模板渲染 + 缓存 + 代理转发

放在服务端做，而不是浏览器直连，原因有三：
1. 绕开 CORS（api.fish.audio 不会给网页发 Access-Control-Allow-Origin）
2. API key 不用暴露给前端
3. 本机需要走 127.0.0.1:7890 代理，浏览器直接连会 Network unreachable
"""
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


# 预设：{{text}} 会自动做 JSON 转义，{{voice}} 独立可改
PRESETS = {
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
