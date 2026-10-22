# -*- coding: utf-8 -*-
"""统一路径解析

所有数据都在一个目录里（默认项目下 data/），云上部署可以用环境变量整体搬走：
    WEBREADER_DATA=/var/lib/webreader      # 数据目录（库、缓存、克隆样本、主密钥）
    WEBREADER_DB=/path/to/reader.db        # 单独指定库文件（优先级最高，测试用）

这样测试实例只要换个 WEBREADER_DATA，就不会碰到线上数据。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("WEBREADER_DATA") or os.path.join(HERE, "data")
DB = os.environ.get("WEBREADER_DB") or os.path.join(DATA, "reader.db")

TTS_CACHE = os.path.join(DATA, "tts")
MIMO_CLONES = os.path.join(DATA, "mimo_clones")
SECRET_KEY = os.path.join(DATA, "secret.key")
LEGACY_MIMO_KEY = os.path.join(DATA, "mimo.key")


def ensure_dirs():
    for d in (DATA, TTS_CACHE, MIMO_CLONES):
        os.makedirs(d, exist_ok=True)
