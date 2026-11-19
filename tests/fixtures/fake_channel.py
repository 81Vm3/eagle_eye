#!/usr/bin/env python3
"""测试用的假外部渠道：fake_channel.py <mode> <describe|search>，按协议读 stdin / 写 stdout。

mode:
  ok          正常返回（search 回显 Query，meta 里带 cwd 和收到的 Query）
  error       exit 2，stdout 输出 cooldown 错误对象
  error0      exit 0，但 stdout 是错误对象
  timeout     先写一行 stderr 再睡 30 秒
  badjson     stdout 输出非 JSON，exit 0
  crash       不输出 stdout，stderr 写几行后 exit 3
  invalid     返回一条合法 Item 和一条缺必填字段的 Item
"""

import json
import os
import sys
import time

mode, verb = sys.argv[1], sys.argv[2]


def out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


print(f"fake {mode} {verb} 启动", file=sys.stderr, flush=True)
if verb == "describe":
    if mode == "badjson":
        print("not json")
        sys.exit(0)
    out({"protocol": 1, "name": "fake", "title": "假渠道", "capabilities": ["search"],
         "options": {"n": {"type": "int", "default": 2}}, "future_field": 1})

q = json.loads(sys.stdin.read() or "null")
if mode == "ok":
    n = int(q.get("options", {}).get("n", 2))
    items = [
        {"channel": "fake", "id": str(i), "url": f"https://example.com/{q['text']}/{i}", "title": f"t{i}",
         "created_at": f"2026-11-1{i}T00:00:00Z", "score": i, "metrics": {"likes": i}, "unknown": True}
        for i in range(n)
    ]
    out({"protocol": 1, "items": items, "meta": {"cwd": os.getcwd(), "query": q, "ignored": ["since_hours"]}})
if mode == "error":
    out({"protocol": 1, "error": {"code": "cooldown", "message": "风控冷却", "retry_after_seconds": 3600}}, 2)
if mode == "error0":
    out({"protocol": 1, "error": {"code": "busy", "message": "浏览器被占用"}})
if mode == "timeout":
    time.sleep(30)
    out({"protocol": 1, "items": []})
if mode == "badjson":
    print("panic: something", file=sys.stderr)
    print("{not json")
    sys.exit(0)
if mode == "crash":
    for i in range(30):
        print(f"stderr line {i}", file=sys.stderr)
    sys.exit(3)
if mode == "invalid":
    out({"protocol": 1, "items": [{"channel": "fake", "id": "1", "url": "https://e.com/1"}, {"id": "2"}]})
sys.exit(99)
