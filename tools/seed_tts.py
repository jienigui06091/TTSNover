#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机已有的 Fish Audio key 灌进网页版的 TTS 设置里（不打印 key）。

key 来源：~/apk-lab/out/base3/tts/fish-audio.json（之前给「阅读」App 生成的配置）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import booklib  # noqa: E402
import tts  # noqa: E402

SRC = os.path.expanduser("~/apk-lab/out/base3/tts/fish-audio.json")
# 音色 ID 不入库：从命令行参数或环境变量 FISH_VOICE 传入
VOICE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FISH_VOICE", "")).strip()


def main():
    if not VOICE:
        print("用法：python3 tools/seed_tts.py <音色ID>   或先 export FISH_VOICE=<音色ID>")
        return 2
    if not os.path.exists(SRC):
        print("找不到 key 来源文件：", SRC)
        return 1
    src = json.load(open(SRC))
    hdr = json.loads(src.get("header") or "{}")
    auth = hdr.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) < 20:
        print("来源文件里没有可用的 Authorization")
        return 1
    model = hdr.get("model", "s2.1-pro-free")

    booklib.init_db()
    s = booklib.get_settings()
    s["tts"] = {
        "engine": "http",
        "url": "https://api.fish.audio/v1/tts",
        "method": "POST",
        "headers": json.dumps(
            {"Content-Type": "application/json", "Authorization": auth, "model": model},
            ensure_ascii=False),
        "body": tts.PRESETS["fish"]["body"],
        "voice": VOICE,
        "speed": 1.18,
        "proxy": tts.DEFAULT_PROXY,
    }
    booklib.put_settings(s)
    print("已写入 TTS 设置：")
    print("  接口   https://api.fish.audio/v1/tts")
    print("  key    已写入（长度 %d，不显示）" % len(auth))
    print("  音色   %s" % VOICE)
    print("  代理   %s" % tts.DEFAULT_PROXY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
