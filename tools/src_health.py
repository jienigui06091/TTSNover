#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书源体检：用几个常见书名测每个源，两个关键词都搜不到就停用。

用法：python3 tools/src_health.py [--dry] [--jobs 12]
"""
import concurrent.futures as cf
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sources  # noqa: E402

KEYS = ["剑来", "诡秘之主"]
JOBS = 12


def check(s):
    got = []
    for k in KEYS:
        try:
            r = sources.test_source(s["id"], k)
            n = int(r.get("count") or 0)
        except Exception:
            n = 0
        got.append(n)
        if n:
            break
    return s, max(got) if got else 0


def main():
    dry = "--dry" in sys.argv
    jobs = JOBS
    for i, a in enumerate(sys.argv):
        if a == "--jobs":
            jobs = int(sys.argv[i + 1])
    lst = sources.list_sources(only_enabled=True)
    print(f"体检 {len(lst)} 个源，关键词 {KEYS}，并发 {jobs}", flush=True)
    dead = []
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        for s, n in ex.map(check, lst):
            print(f"  {'✓' if n else '✗'} {s['name']:<22} {n} 本", flush=True)
            if not n:
                dead.append(s)
    print(f"\n可用 {len(lst) - len(dead)} / {len(lst)}，失效 {len(dead)}")
    if dry:
        return
    for s in dead:
        sources.toggle_source(s["id"], False)
    print("已停用失效源，之后搜索只跑可用源。")


if __name__ == "__main__":
    main()
