"""Live chat check: real DeepSeek, the exact queries from the user's screenshot.

The fake-agent test proves the wiring; this proves the real path works and
that an actual answer comes back (the failure mode was an empty reply).
"""
import json
import os
import sys

ROOT = r"C:\code\论文助手\AIagent2\AIagent1"
sys.path.insert(0, ROOT)
# 用统一的加载逻辑：.env → .env.local（key 只在后者里，见 envconfig.py）
import envconfig
envconfig.load_env_files(ROOT)

if not os.getenv("DEEPSEEK_API_KEY"):
    print("SKIP  没有 DEEPSEEK_API_KEY，跳过真机测试。")
    print(f"      请写入：{envconfig.ENV_LOCAL_FILE}")
    sys.exit(0)

import database as db
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path  # noqa: E402

DB = temp_db_path("_tmp_livechat.db")
db.DB_PATH = DB

import app as appmod

print("模型:", appmod.DEEPSEEK_MODEL)
db.init_db()
c = appmod.app.test_client()

fails = 0
for msg in ("农业", "自动化", "帮我写一篇关于农业自动化的综述"):
    r = c.post("/api/chat", json={"message": msg, "history": [], "source_pref": "all"},
               buffered=True)
    events = []
    for line in r.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                pass
    answer = "".join(e.get("token") or "" for e in events if e.get("type") == "token")
    papers = 0
    for e in events:
        if e.get("type") == "papers":
            papers = len(e.get("papers") or [])
    kinds = [e.get("type") for e in events]
    ready = next((e for e in events if e.get("type") == "workflow_ready"), None)
    print(f"\n{'='*64}\n用户: {msg}")
    print(f"  HTTP={r.status_code} 事件={kinds}")
    print(f"  论文={papers} 回答长度={len(answer)}")
    print(f"  回答: {answer[:220]}")
    if ready:
        print(f"  [移交] workflow_id={ready.get('workflow_id')} "
              f"goal={ready.get('goal')!r} query={(ready.get('plan') or {}).get('query')!r}")
    if r.status_code != 200 or not answer:
        fails += 1
        print("  ❌ 这一轮没有拿到回答")

print(f"\n{'='*64}")
print("真机对话检查:", "FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)
