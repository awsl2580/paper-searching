"""Round-trip test: interrupt -> (new client, only the id) -> resume -> done.

This mirrors exactly what the new frontend resume button does: it posts only
{workflow_id}, with no session-scoped in-memory state carried over.
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
os.environ["S2_MIN_INTERVAL"] = "0"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
from _writable_temp import temp_db_path  # noqa: E402

import database as db
DB = temp_db_path("_tmp_resume.db")
db.DB_PATH = DB

import app as appmod
import workflow as wf
import providers

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def papers(n=8):
    return [{"id": f"openalex:W{i}", "title": f"图神经网络研究 {i}", "year": 2015 + i,
             "authors": ["张三", "李四"], "journal": "计算机学报", "docType": "journal",
             "doi": f"10.1/{i}", "url": "u", "citationCount": i * 7,
             "openAccessPdf": "p" if i % 3 == 0 else "", "language": "zh",
             "source": "openalex", "sourceLabel": "OpenAlex", "relevance": 100 - i,
             "abstract": f"摘要 {i}"} for i in range(n)]


providers.search_papers = lambda q, l=5, s="all", m=0, off=0: json.dumps(
    {"papers": papers(8), "total": 8, "sources": ["OpenAlex"], "filtered_out": 2},
    ensure_ascii=False)

EXTRACT_BATCHES = []


def mock_llm(prompt, content, **k):
    if "文献信息抽取" in prompt:
        batch = json.loads(content).get("papers", [])
        EXTRACT_BATCHES.append(len(batch))
        return {"items": [{"i": i + 1, "method": "GCN", "object": "遥感", "data": "D",
                           "metric": "OA", "finding": "有效"} for i in range(len(batch))]}
    if "综述撰写" in prompt:
        return {"sections": [{"heading": "研究背景", "body": "近年发展迅速 [1]。"},
                             {"heading": "主流方法", "body": "以图卷积为主 [2]。"}],
                "consensus": ["c"], "gaps": ["g"], "caveat": "x"}
    return {"topic": "图神经网络遥感", "query": "图神经网络 遥感", "source": "cn",
            "limit": 8, "focus": "方法与数据集", "plan": "先中文检索再归纳",
            "sections": ["研究背景", "主流方法"]}


wf._call_json = mock_llm

client = appmod.app.test_client()
client.post("/api/register", json={"username": "tester", "password": "Passw0rd1"})
client.post("/api/login", json={"username": "tester", "password": "Passw0rd1"})


def drive(payload, c=client):
    resp = c.post("/api/workflow", json=payload)
    evs = [json.loads(l[6:]) for l in resp.get_data(as_text=True).splitlines()
           if l.startswith("data: ")]
    return resp.status_code, evs


# 中断在第一批抽取之后（一次抽取一批，8 篇刚好 1 批）
# ── A. 在步骤内部（批次边界）中断 ────────────────────────────────
# should_stop 的调用点：每步循环顶部 1 次；extract 每批之前 1 次；synthesize 开始前 1 次。
#   1 search 顶部 -> 2 extract 顶部 -> 3 extract 第 0 批之前（此时真的开始跑批次了）
# 所以阈值取 2：第 3 次问（extract 内部、批次边界）返回 True，
# extract 一步都没抽就报中断 —— 正是"长步骤也能被打断"要验的行为。
calls = {"n": 0}
def stop_after_extract():
    calls["n"] += 1
    return calls["n"] > 2

orig = wf.WorkflowRunner
class Stopping(orig):
    def __init__(self, plan, **kw):
        kw["should_stop"] = stop_after_extract
        super().__init__(plan, **kw)
wf.WorkflowRunner = Stopping
_, evs1 = drive({"goal": "图神经网络在遥感图像中的应用进展"})
wf.WorkflowRunner = orig

wid = next(e for e in evs1 if e.get("type") == "plan")["workflow_id"]
snap1 = next(e for e in evs1 if e.get("type") == "snapshot")
st1 = {s["key"]: s["status"] for s in snap1["steps"]}
ok("首次运行被中断", evs1[-1]["status"] == "interrupted", evs1[-1])
row1 = db.get_workflow(wid, user_id=1)
broken = [k for k, v in row1["steps"].items() if v["status"] == "interrupted"]
ok("恰有一个断点步骤", len(broken) == 1, {k: v["status"] for k, v in row1["steps"].items()})
ok("断点是下一步", broken and broken[0] == "extract", broken)
ok("首轮在 extract 内部被打断", "extract" in {
    e.get("key") for e in evs1 if e.get("status") == "interrupted"}, st1)
ok("抽取未跑完不写结果", not any(
    e.get("key") == "extract" and e.get("status") == "done" for e in evs1 if e.get("type") == "step"),
   [(e.get("key"), e.get("status")) for e in evs1 if e.get("type") == "step"])
ok("检索结果已留存", len((row1.get("context") or {}).get("papers") or []) == 8,
   len((row1.get("context") or {}).get("papers") or []))

# 关键：换一个全新客户端（模拟刷新页面），只带 workflow_id 续跑
fresh = appmod.app.test_client()
fresh.post("/api/login", json={"username": "tester", "password": "Passw0rd1"})
EXTRACT_BATCHES.clear()
code2, evs2 = drive({"workflow_id": wid}, c=fresh)
plan2 = next(e for e in evs2 if e.get("type") == "plan")
ok("新会话也能续跑", code2 == 200, code2)
ok("续跑回报 resumed", plan2.get("resumed") is True, plan2.get("resumed"))
ok("续跑起点是断点 extract", plan2.get("start_from") == "extract", plan2.get("start_from"))
ok("续跑确实重跑了抽取", EXTRACT_BATCHES == [8], EXTRACT_BATCHES)
ok("续跑没有重跑检索", "search" not in [e.get("key") for e in evs2
                                    if e.get("type") == "step" and e.get("status") == "running"])

snap2 = next(e for e in evs2 if e.get("type") == "snapshot")
st2 = {s["key"]: s["status"] for s in snap2["steps"]}
ok("续跑后五步全 done", all(v == "done" for v in st2.values()), st2)
ok("续跑快照保留断点前的步骤", st2.get("search") == "done", st2)
ok("续跑最终状态 done", evs2[-1]["status"] == "done", evs2[-1])

row2 = db.get_workflow(wid, user_id=1)
ok("库里五步全 done", all(v["status"] == "done" for v in row2["steps"].values()),
   {k: v["status"] for k, v in row2["steps"].items()})
ok("报告含综述正文", "研究背景" in (row2["report_md"] or ""))
ok("报告含参考文献", "GB/T 7714" in (row2["report_md"] or ""))
ok("快照沿用同一条工作流", snap2.get("paperCount") == 8, snap2.get("paperCount"))

# 人工修正文献后续跑
small = papers(3)
code3, evs3 = drive({"workflow_id": wid, "papers": small}, c=fresh)
snap3 = next(e for e in evs3 if e.get("type") == "snapshot")
ok("人工修正后用 3 篇", snap3["paperCount"] == 3, snap3["paperCount"])
row3 = db.get_workflow(wid, user_id=1)
ok("人工修正已落库", len((row3.get("context") or {}).get("papers") or []) == 3,
   len((row3.get("context") or {}).get("papers") or []))

# ── B. 在两个步骤之间中断 ───────────────────────────────────────
# should_stop 的调用点：1 search顶 / 2 extract顶 / 3 extract批 / 4 synth顶 / 5 ana顶 / 6 fmt顶
# （analyze、format 是纯计算，没有内部检查点）
# 阈值 5 -> 第 6 次问（analyze 顶部）返回 True：前三步 done，断点落在 analyze。
calls["n"] = 0
def stop_before_analyze():
    calls["n"] += 1
    return calls["n"] > 5

class Stopping2(orig):
    def __init__(self, plan, **kw):
        kw["should_stop"] = stop_before_analyze
        super().__init__(plan, **kw)
wf.WorkflowRunner = Stopping2
_, evs4 = drive({"goal": "遥感图像语义分割综述"}, c=fresh)
wf.WorkflowRunner = orig

wid4 = next(e for e in evs4 if e.get("type") == "plan")["workflow_id"]
row4 = db.get_workflow(wid4, user_id=1)
st4 = {k: v["status"] for k, v in row4["steps"].items()}
ok("步间中断：断点落在还没开始的 analyze", st4.get("analyze") == "interrupted", st4)
ok("步间中断：跑完的步骤保持 done",
   all(st4.get(k) == "done" for k in ("search", "extract", "synthesize")), st4)
ok("步间中断：恰一个断点",
   sum(1 for v in st4.values() if v == "interrupted") == 1, st4)
ok("步间中断：库状态为 interrupted", row4["status"] == "interrupted", row4["status"])
ok("步间中断：综述正文已留存", bool((row4.get("context") or {}).get("synth")),
   bool((row4.get("context") or {}).get("synth")))

_, evs5 = drive({"workflow_id": wid4}, c=fresh)
plan5 = next(e for e in evs5 if e.get("type") == "plan")
ok("步间续跑：从 analyze 接着跑", plan5.get("start_from") == "analyze", plan5.get("start_from"))
snap5 = next(e for e in evs5 if e.get("type") == "snapshot")
st5 = {s["key"]: s["status"] for s in snap5["steps"]}
ok("步间续跑：五步全 done", all(v == "done" for v in st5.values()), st5)
ok("步间续跑：最终 done", evs5[-1]["status"] == "done", evs5[-1])

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
