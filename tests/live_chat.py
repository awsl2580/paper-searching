"""Live chat check: 真的调 DeepSeek 模型（配了 DEEPSEEK_API_KEY 时），不打桩。

The fake-agent test proves the wiring; this proves the real path works and
that an actual answer comes back (the failure mode was an empty reply).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
# 用统一的加载逻辑：.env → .env.local（Key 可以只写在后者里，见 envconfig.py）
import envconfig
envconfig.load_env_files(ROOT)

if not envconfig.api_key():
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

_info = appmod.llm_layer.model_info()
print("模型后端:", _info["backend_label"])
for _r in _info["roles"]:
    print("  ", _r["label"], _r["model"])
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
