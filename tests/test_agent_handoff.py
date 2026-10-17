"""Test the chat->workflow handoff tool (start_research_workflow)."""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
from _writable_temp import temp_db_path  # noqa: E402

import database as db
DB = temp_db_path("_tmp_handoff.db")
db.DB_PATH = DB

import app as appmod
import workflow as wf

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# 规划这一步会真的调模型，这里换成固定返回值
PLANNED = {"topic": "图神经网络遥感应用", "query": "图神经网络 遥感",
           "source": "cn", "limit": 12, "focus": "方法与数据集",
           "plan": "先中文检索再归纳", "sections": ["研究背景", "主流方法"]}
wf.plan_workflow = lambda goal: dict(PLANNED, topic=goal)

seen = []
tool = appmod._make_research_tool(seen.append, user_id=1)

# 成功路径返回的是**给模型看的自然语言**，不是 JSON —— 见 test_chat_route.py
# 里的说明：返回 JSON 时模型会把 JSON 当答复复读给用户。
out = tool.invoke({"goal": "图神经网络在遥感图像中的应用进展"})
ok("工具返回字符串", isinstance(out, str), type(out))
ok("返回不是 JSON", not out.lstrip().startswith(("{", "[")), out[:60])
ok("返回是中文散文", "已为用户规划" in out and "。" in out, out[:60])
ok("返回带检索式", "图神经网络 遥感" in out, out[:160])
ok("返回带执行流程", "文献检索与筛选" in out and "参考文献" in out, out[:200])
ok("回调被触发一次", len(seen) == 1, len(seen))
ok("回调带上方案", bool((seen[0] if seen else {}).get("plan")), seen)
ok("回调带上目标", (seen[0] if seen else {}).get("goal") == "图神经网络在遥感图像中的应用进展",
   (seen[0] if seen else {}).get("goal"))

wid = (seen[0] if seen else {}).get("workflow_id")
ok("回调带上工作流 id", isinstance(wid, int), wid)
row = db.get_workflow(wid, user_id=1)
ok("工作流真的落库了", row is not None)
ok("落库状态是初始态", row and row["status"] in ("pending", "created", "draft"), row and row["status"])
ok("落库 goals 正确", row and row["goal"] == "图神经网络在遥感图像中的应用进展",
   row and row["goal"])

# 太短的目标要拦下来，不能白建一条工作流
before = len(db.get_workflows(1) or [])
short = json.loads(tool.invoke({"goal": "哦"}))
ok("过短目标被拒", short.get("error") == "goal_too_short", short)
ok("被拒时不建工作流", len(db.get_workflows(1) or []) == before,
   len(db.get_workflows(1) or []))

# 未登录（user_id=None）也要能规划，只是不落库
anon_seen = []
anon_tool = appmod._make_research_tool(anon_seen.append, user_id=None)
anon_out = anon_tool.invoke({"goal": "联邦学习隐私保护研究进展"})
ok("未登录也能规划", isinstance(anon_out, str) and "已为用户规划" in anon_out, anon_out[:60])
ok("未登录时不落库 id 为 None",
   (anon_seen[0] if anon_seen else {}).get("workflow_id") is None,
   (anon_seen[0] if anon_seen else {}).get("workflow_id"))
ok("未登录也触发回调", len(anon_seen) == 1, len(anon_seen))

# 规划炸了不能把整轮对话带崩
def boom(goal):
    raise RuntimeError("模型超时")
wf.plan_workflow = boom
bad = json.loads(tool.invoke({"goal": "随便一个够长的研究目标"}))
ok("规划失败返回错误而不是抛异常", bad.get("error") == "plan_failed", bad)
ok("错误里带上原因", "模型超时" in (bad.get("message") or ""), bad.get("message"))

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
