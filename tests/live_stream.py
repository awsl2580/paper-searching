"""真机验证工作流流式推送：事件必须是渐进到达的，不能末尾一次性回放。

对着**正在运行的服务**（默认 http://127.0.0.1:8000）打真实请求，
按到达时刻给每个 SSE 事件打时间戳 —— 这就是浏览器面板实际经历的过程。

为什么单独写这个：单元测试 test_streaming.py 用的是 mock 模型，
只能证明"代码是边跑边推"；真实链路上还可能被缓冲、被网关攒包、
或者某一步特别慢导致观感又变回"卡住"。这个脚本量的是真实时间线。

用法：
    python app.py            # 另开一个终端把服务跑起来
    python tests/live_stream.py

判据：首个 step 事件要在 1 秒内到达，且 step 事件跨度要占总时长的一半以上
（说明是一路推过来的，而不是最后才刷出来）。
"""
import json
import sys
import time
import urllib.request
import uuid
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8000"
jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def post(path, payload, stream=False):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"})
    return opener.open(req, timeout=300)


# 用户名要唯一：重复注册同一个名字服务端会 400（第一版就是因此跑挂的）
user = "livestream_" + uuid.uuid4().hex[:8]
post("/api/register", {"username": user, "password": "Passw0rd1"})
post("/api/login", {"username": user, "password": "Passw0rd1"})

GOAL = "图神经网络在遥感图像语义分割中的应用进展"


def run_once(label):
    t0 = time.time()
    resp = post("/api/workflow", {"goal": GOAL})
    stamps = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except ValueError:
            continue
        t = time.time() - t0
        stamps.append((t, ev))
        if ev.get("type") == "done":
            break
    total = stamps[-1][0]
    steps = [s for s in stamps if s[1].get("type") == "step"]
    first = steps[0][0] if steps else None
    spread = (steps[-1][0] - steps[0][0]) if steps else 0
    print(f"\n[{label}] 总耗时 {total:.1f}s | 首个 step t={first:.2f}s | step 跨度 {spread:.1f}s")
    for t, ev in stamps:
        if ev.get("type") == "step" and ev.get("status") == "running":
            print(f"    {t:6.2f}s  {ev.get('key'):<11}{str(ev.get('detail'))[:44]}")
    return total, first, spread, stamps


total, first, spread, stamps = run_once("第一次")
total2, first2, spread2, stamps2 = run_once("第二次（同一目标，规划应命中缓存）")

ok = first is not None and first < 1.0 and spread > total * 0.4
print(f"\n流式推送: {'PASS' if ok else 'FAIL'}（首个 step t={first:.2f}s，跨度 {spread:.1f}s）")
print(f"第二次比第一次: {total - total2:+.1f}s")

if ok:
    sys.exit(0)
print("结果: FAIL")
sys.exit(1)
