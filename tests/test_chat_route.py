"""Chat route test with a FAKE agent (no model calls, no network).

Why this file exists: the workflow suites never exercised /api/chat, so a
NameError inside generate() (undefined DEEPSEEK_MODEL) shipped undetected and
every chat request returned an empty reply. This locks the route down:
SSE framing, token streaming, papers extraction, status events, the
workflow_ready handoff, and "no silent empty answer".
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
from _writable_temp import temp_db_path  # noqa: E402

import database as db
DB = temp_db_path("_tmp_chat.db")
db.DB_PATH = DB

import app as appmod
from langchain_core.messages import AIMessage

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


PAPERS = [{"id": "openalex:W1", "title": "农业经济研究", "year": 2024,
           "authors": ["张三"], "journal": "农业学报", "citationCount": 5,
           "relevance": 90, "source": "openalex", "sourceLabel": "OpenAlex"}]


class FakeAgent:
    """按脚本吐 astream_events，形状与 langchain 的真事件一致。

    script 里每一项是：
        ("tool_start", name)      工具开始
        ("tool_end", content)     工具返回
        ("call_tool", (name, args)) 真的去调路由建的那个工具（含副作用）
        ("token", text)           模型吐字
        ("reasoning", text)       模型"思考"分片（思考模型才有）
    """

    def __init__(self, script):
        self.script = script
        self.tools = {}

    async def astream_events(self, payload, version="v2"):
        for item in self.script:
            kind = item[0]
            if kind == "tool_start":
                yield {"event": "on_tool_start", "name": item[1], "data": {}}
            elif kind == "tool_end":
                yield {"event": "on_tool_end",
                       "data": {"output": AIMessage(content=item[1])}}
            elif kind == "call_tool":
                name, args = item[1]
                out = self.tools[name].invoke(args)
                yield {"event": "on_tool_end",
                       "data": {"output": AIMessage(content=out) if isinstance(out, str) else out}}
            elif kind == "token":
                yield {"event": "on_chat_model_stream",
                       "data": {"chunk": AIMessage(content=item[1])}}
            elif kind == "reasoning":
                # 思考模型的分片：正文为空，思考内容放在 additional_kwargs
                yield {"event": "on_chat_model_stream",
                       "data": {"chunk": AIMessage(
                           content="", additional_kwargs={"reasoning_content": item[1]})}}


def run_chat(script, message="农业", **body):
    """script 里 ("call_tool", 参数dict) 会**真的调用**路由建的那个工具，
    这样 workflow_ready 链路（工具 -> notify -> state -> SSE）才是真被验证的，
    而不是我伪造一个事件假装它成功了。"""
    agent = FakeAgent(script)
    captured = {}

    def fake_create_agent(**kw):
        captured["tools"] = {getattr(t, "name", ""): t for t in kw.get("tools") or []}
        agent.tools = captured["tools"]   # 让 call_tool 能调到路由建的那个工具实例
        return agent

    orig = appmod.create_agent
    appmod.create_agent = fake_create_agent
    try:
        c = appmod.app.test_client()
        r = c.post("/api/chat", json={"message": message, "history": [],
                                      "source_pref": "all", **body}, buffered=True)
        raw = r.get_data(as_text=True)
    finally:
        appmod.create_agent = orig
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                pass
    return r.status_code, events, captured.get("tools") or {}


def tokens_of(events):
    return "".join(e.get("token") or "" for e in events if e.get("type") == "token")


# ── 1. 普通检索问答：工具 -> 论文 -> 正文 ───────────────────────────
code, evs, agent = run_chat([
    ("tool_start", "search_papers"),
    ("tool_end", json.dumps({"papers": PAPERS}, ensure_ascii=False)),
    ("token", "找到 1 篇："),
    ("token", "农业经济研究"),
])
ok("POST /api/chat 返回 200", code == 200, code)
ok("SSE 以 data: 前缀", all(isinstance(e, dict) for e in evs), evs[:1])
ok("有 done 事件", any(e.get("type") == "done" for e in evs))
ok("吐出 token 事件", bool([e for e in evs if e.get("type") == "token"]))
ok("回答内容正确", tokens_of(evs) == "找到 1 篇：农业经济研究", tokens_of(evs))
ok("论文事件带 papers", any(e.get("type") == "papers" for e in evs))
papers_ev = next((e for e in evs if e.get("type") == "papers"), {})
ok("论文数据完整", (papers_ev.get("papers") or [{}])[0].get("title") == "农业经济研究",
   papers_ev)
ok("工具调用前有状态提示", any(e.get("type") == "status" for e in evs))
ok("检索类工具提示检索语", any("检索" in (e.get("text") or "")
                            for e in evs if e.get("type") == "status"),
   [e.get("text") for e in evs if e.get("type") == "status"])

# ── 2. 闲聊（不调工具）：答案也要补发出去，不能空 ──────────────────
code2, evs2, _ = run_chat([("token", "你好，"), ("token", "有什么可以帮你？")],
                          message="你好")
ok("闲聊也返回 200", code2 == 200, code2)
ok("闲聊回答不为空", tokens_of(evs2) == "你好，有什么可以帮你？", tokens_of(evs2))

# ── 3. 核心回归：任何情况下都不能出现"回答为空" ────────────────────
# 这正是截图里的症状（界面显示"本轮没有收到回复内容，请重试"）
for label, script, msg in [
    ("检索问答", [("tool_start", "search_papers"),
                  ("tool_end", json.dumps({"papers": PAPERS}, ensure_ascii=False)),
                  ("token", "答案")], "农业"),
    ("闲聊", [("token", "答案")], "自动化"),
]:
    _, evs3, _ = run_chat(script, message=msg)
    got = tokens_of(evs3)
    ok(f"{label}：回答非空", len(got) > 0, repr(got))

# ── 4. 任务移交：workflow_ready 事件要带方案 ───────────────────────
def fake_plan(goal):
    return {"topic": goal, "query": "农业 自动化", "source": "cn", "limit": 8,
            "focus": "f", "plan": "p", "sections": ["s"]}


orig_plan = appmod.wf.plan_workflow
appmod.wf.plan_workflow = fake_plan
try:
    _, evs4, tools4 = run_chat([
        ("tool_start", "start_research_workflow"),
        ("call_tool", ("start_research_workflow",
                       {"goal": "农业自动化研究进展"})),
        ("token", "方案已就绪"),
    ], message="帮我写篇农业自动化的综述")
finally:
    appmod.wf.plan_workflow = orig_plan

ok("路由注册了移交工具", "start_research_workflow" in tools4, sorted(tools4))
ok("有 workflow_ready 事件", any(e.get("type") == "workflow_ready" for e in evs4),
   [e.get("type") for e in evs4])
ready = next((e for e in evs4 if e.get("type") == "workflow_ready"), {})
ok("workflow_ready 带 goal", ready.get("goal") == "农业自动化研究进展", ready.get("goal"))
ok("workflow_ready 带 plan", bool(ready.get("plan")), ready.get("plan"))
ok("移交类工具提示规划语",
   any("规划" in (e.get("text") or "") for e in evs4 if e.get("type") == "status"),
   [e.get("text") for e in evs4 if e.get("type") == "status"])

# ── 5. 空消息要 400，不能进到模型 ─────────────────────────────────
c = appmod.app.test_client()
ok("空消息 -> 400", c.post("/api/chat", json={"message": "  "}).status_code == 400)

# ── 6. 结构化数据不能当正文复读（截图外的真实事故） ────────────────
# 事故现场：移交工作流时，模型的回答从 {"topic":"...","query":"..."} 开始，
# 整坨 JSON 被当成正文流到了界面上。这里锁死出口行为。
JSON_REPLY = '{"topic": "农业自动化", "query": "农业 自动化", "sections": ["a", "b"]}'


def fake_plan2(goal):
    return {"topic": goal, "query": "农业 自动化", "source": "all", "limit": 8,
            "focus": "f", "plan": "p", "sections": ["s"]}


orig_plan2 = appmod.wf.plan_workflow
appmod.wf.plan_workflow = fake_plan2
try:
    _, evs6, _ = run_chat([
        ("tool_start", "start_research_workflow"),
        ("call_tool", ("start_research_workflow", {"goal": "农业自动化研究进展"})),
        ("token", JSON_REPLY),
    ], message="帮我写一篇关于农业自动化的综述")
finally:
    appmod.wf.plan_workflow = orig_plan2

got6 = tokens_of(evs6)
ok("模型吐 JSON 时正文不出现花括号", "{" not in got6 and "topic" not in got6, got6[:120])
ok("替换成了人话", "开始执行" in got6, got6[:160])
ok("仍然给出 workflow_ready 事件",
   any(e.get("type") == "workflow_ready" for e in evs6), [e.get("type") for e in evs6])
ok("被替代后回答非空", len(got6) > 0, repr(got6))

# 正常散文回答不能被误伤
_, evs7, _ = run_chat([("tool_start", "search_papers"),
                       ("tool_end", json.dumps({"papers": PAPERS}, ensure_ascii=False)),
                       ("token", "已找到 1 篇，标题是《农业经济研究》。")])
got7 = tokens_of(evs7)
ok("正常回答不被静音", got7 == "已找到 1 篇，标题是《农业经济研究》。", got7)

# 工具的返回本身也必须是自然语言，不能是 JSON 或"字段：值"清单
_, _, tools8 = run_chat([("tool_start", "x"), ("token", "ok")])
tool8 = tools8.get("start_research_workflow")
appmod.wf.plan_workflow = fake_plan2
try:
    raw8 = tool8.invoke({"goal": "农业自动化技术研究进展"})
finally:
    appmod.wf.plan_workflow = orig_plan2
ok("工具返回不是 JSON", not raw8.lstrip().startswith(("{", "[")), raw8[:60])
ok("工具返回是中文散文", "。" in raw8 and "已为用户规划" in raw8, raw8[:60])
ok("工具返回带执行流程", "检索" in raw8 and "参考文献" in raw8, raw8[:120])

# ── 7. 思考模型（deepseek-flash）：思考阶段不能让界面看起来卡死 ──────
# 实测 deepseek-flash 默认开思考：一个需要推理的问题会先吐 4637 字
# reasoning_content，首字在 21.4s 才出现（对比 deepseek-chat 的 0.6s）。
# 如果代码只读 content，这 21 秒里前端一个字节都收不到，就是个空白转圈。
_, evs9, _ = run_chat([
    ("reasoning", "先分析题目……"),
    ("reasoning", "再梳理方法……"),
    ("token", "结论如下。"),
])
statuses9 = [e.get("text") or "" for e in evs9 if e.get("type") == "status"]
# 注意：开头的 "正在思考…" 是**早就存在**的初始状态（app.py 里 yield 的第一条），
# 不是思考模型的信号。所以这里必须匹配那条更具体的文案，
# 否则断言会把初始状态也算进来 —— 第一版就是这么写错的。
ok("思考阶段会推一条状态给前端",
   any("模型正在思考" in s for s in statuses9), statuses9)
ok("思考状态只报一次（不是每个分片都刷）",
   sum(1 for s in statuses9 if "模型正在思考" in s) == 1, statuses9)
ok("正文开始后状态切回生成回答",
   any("生成回答" in s for s in statuses9), statuses9)
got9 = tokens_of(evs9)
ok("思考内容不会被当成正文吐出去",
   "先分析题目" not in got9 and "再梳理方法" not in got9, got9[:80])
ok("正文照常输出", got9 == "结论如下。", got9)

# 没有思考分片时（非思考模型）不应出现"模型正在思考"这条新增状态
_, evs10, _ = run_chat([("tool_start", "search_papers"),
                        ("tool_end", json.dumps({"papers": PAPERS}, ensure_ascii=False)),
                        ("token", "答案")])
statuses10 = [e.get("text") or "" for e in evs10 if e.get("type") == "status"]
ok('非思考模型不出现「模型正在思考」',
   not any("模型正在思考" in s for s in statuses10), statuses10)

n_pass = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
