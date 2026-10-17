"""Live end-to-end run: real DeepSeek + real academic providers, no mocks.

Drives POST /api/workflow through the Flask test client and prints what the
pipeline actually produced. Slow (several model calls + real API searches),
so run it in the background.
"""
import json, os, sys, time

ROOT = r"C:\code\论文助手\AIagent2\AIagent1"
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

if not os.getenv("DEEPSEEK_API_KEY"):
    print("!! DEEPSEEK_API_KEY 未配置，无法真机跑")
    sys.exit(2)

import database as db
# 临时库/报告放到确认可写的目录（见 tests/_writable_temp.py 的说明）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path, temp_file_path  # noqa: E402

DB = temp_db_path("_tmp_live.db")
db.DB_PATH = DB

import app as appmod

db.init_db()
client = appmod.app.test_client()
client.post("/api/register", json={"username": "livetester", "password": "Passw0rd1"})
r = client.post("/api/login", json={"username": "livetester", "password": "Passw0rd1"})
print("登录:", r.status_code)

GOAL = "图神经网络在遥感图像语义分割中的应用进展"
print("研究目标:", GOAL)
t0 = time.time()

resp = client.post("/api/workflow", json={"goal": GOAL})
print("HTTP:", resp.status_code, "| 耗时 %.1fs" % (time.time() - t0))

events = [json.loads(l[6:]) for l in resp.get_data(as_text=True).splitlines()
          if l.startswith("data: ")]
print("\n事件流:")
for e in events:
    if e.get("type") == "plan":
        p = e["plan"]
        print(f"  [plan] id={e['workflow_id']} topic={p.get('topic')!r}")
        print(f"         query={p.get('query')!r} source={p.get('source')} limit={p.get('limit')}")
        print(f"         focus={p.get('focus')!r}")
        print(f"         sections={p.get('sections')}")
    elif e.get("type") == "step":
        print(f"  [step] {e.get('key'):11s} {e.get('status'):12s} {str(e.get('detail'))[:70]}")
    elif e.get("type") == "error":
        print(f"  [error] {e.get('message')}")
    elif e.get("type") == "done":
        print(f"  [done] status={e.get('status')}")

snap = next((e for e in events if e.get("type") == "snapshot"), None)
if snap:
    print("\n步骤终态:", {s["key"]: s["status"] for s in snap["steps"]})
    print("命中文献:", snap.get("paperCount"))
    stats = snap.get("stats") or {}
    print("统计: 总数=%s 年份=%s 有PDF=%s 类型=%s" % (
        stats.get("total"), stats.get("years"), stats.get("withPdf"), stats.get("docTypes")))
    synth = snap.get("synth") or {}
    print("\n综述章节:", [s.get("heading") for s in (synth.get("sections") or [])])
    print("共识:", (synth.get("consensus") or [])[:3])
    print("空白:", (synth.get("gaps") or [])[:3])
    refs = snap.get("references") or []
    print(f"\n参考文献 {len(refs)} 条，前 3 条:")
    for x in refs[:3]:
        print("   ", x)
    report = snap.get("report") or ""
    print(f"\n报告 {len(report)} 字")
    out = temp_file_path("live_report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print("报告已写出:", out)
    print("\n--- 报告开头 1200 字 ---")
    print(report[:1200])
    print("--- 报告结尾 500 字 ---")
    print(report[-500:])

print("\n总耗时 %.1fs" % (time.time() - t0))
