"""End-to-end test of the workflow API (SSE, persistence, interrupt, resume)."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
os.environ["S2_MIN_INTERVAL"] = "0"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402
DB = temp_db_path("_tmp_api.db")
db.DB_PATH = DB

import app as appmod  # noqa: E402
import workflow as wf  # noqa: E402
import providers  # noqa: E402

db.init_db()
R = []
def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def papers(n=6):
    return [{"id": f"openalex:W{i}", "title": f"图神经网络研究 {i}", "year": 2018 + i,
             "authors": ["张三"], "journal": "计算机学报", "docType": "journal",
             "doi": f"10.1/{i}", "url": "u", "citationCount": i * 5,
             "openAccessPdf": "p" if i % 2 == 0 else "", "language": "zh",
             "source": "openalex", "sourceLabel": "OpenAlex", "relevance": 100,
             "abstract": f"摘要 {i}"} for i in range(n)]


providers.search_papers = lambda q, l=5, s="all", m=0, off=0: json.dumps(
    {"papers": papers(6), "total": 6, "sources": ["OpenAlex"], "filtered_out": 1},
    ensure_ascii=False)


def mock_llm(prompt, content, **k):
    if "文献信息抽取" in prompt:
        n = len(json.loads(content).get("papers", []))
        return {"items": [{"i": i + 1, "method": "GCN", "object": "遥感", "data": "D",
                           "metric": "OA", "finding": "有效"} for i in range(n)]}
    if "综述撰写" in prompt:
        return {"sections": [{"heading": "研究背景", "body": "近年发展迅速 [1]。"},
                             {"heading": "主流方法", "body": "以图卷积为主 [2]。"}],
                "consensus": ["多数在标准数据集上验证"],
                "gaps": ["缺跨领域迁移"], "caveat": "仅 6 篇，覆盖有限"}
    return {"topic": "图神经网络遥感应用", "query": "图神经网络 遥感", "source": "cn",
            "limit": 6, "focus": "方法与数据集", "plan": "先中文检索再归纳",
            "sections": ["研究背景", "主流方法"]}


wf._call_json = mock_llm
client = appmod.app.test_client()
client.post("/api/register", json={"username": "tester", "password": "Passw0rd1"})
client.post("/api/login", json={"username": "tester", "password": "Passw0rd1"})


def drive(body):
    resp = client.post("/api/workflow", json=body)
    evs = []
    for line in resp.get_data(as_text=True).split("\n\n"):
        if line.startswith("data: "):
            try:
                evs.append(json.loads(line[6:]))
            except Exception:
                pass
    return resp, evs


# ── 1. 完整跑一遍 ────────────────────────────────────────────────────────
resp, evs = drive({"goal": "研究图神经网络在遥感图像中的应用进展"})
ok("返回 SSE", resp.status_code == 200 and "text/event-stream" in resp.headers.get("Content-Type", ""))
kinds = [e.get("type") for e in evs]
ok("有 plan 事件", "plan" in kinds, kinds[:4])
ok("有多个 step 事件", kinds.count("step") >= 8, kinds.count("step"))
ok("有 snapshot 事件", "snapshot" in kinds)
ok("有 done 事件", kinds[-1] == "done", kinds[-1])

plan_ev = next(e for e in evs if e.get("type") == "plan")
ok("plan 带 workflow_id", isinstance(plan_ev.get("workflow_id"), int))
wid = plan_ev["workflow_id"]

snap = next(e for e in evs if e.get("type") == "snapshot")
ok("snapshot 含 5 个步骤", len(snap["steps"]) == 5, len(snap["steps"]))
ok("步骤都标了核心功能编号",
   [s["core"] for s in snap["steps"]] == ["①", "②", "②", "③", "④"],
   [s["core"] for s in snap["steps"]])
ok("全部步骤 done", all(s["status"] == "done" for s in snap["steps"]),
   [(s["key"], s["status"]) for s in snap["steps"]])
ok("snapshot 含 6 篇文献", snap["paperCount"] == 6)
ok("snapshot 含统计", snap["stats"]["total"] == 6)
ok("snapshot 含综述两节", len(snap["synth"]["sections"]) == 2)
ok("snapshot 含 6 条参考文献", len(snap["references"]) == 6)
ok("报告含综述正文", "研究背景" in snap["report"])
ok("报告含统计图表", "```" in snap["report"] and "年份分布" in snap["report"])
ok("报告含参考文献章节", "参考文献（GB/T 7714）" in snap["report"])
ok("报告含信息抽取明细", "信息抽取明细" in snap["report"])
ok("done 状态为 done", evs[-1]["status"] == "done", evs[-1])

# ── 2. 状态落库 ──────────────────────────────────────────────────────────
row = db.get_workflow(wid, user_id=1)
ok("工作流已落库", row is not None)
ok("状态为 done", row["status"] == "done", row["status"])
ok("五步状态都存了", len(row["steps"]) == 5, list(row["steps"].keys()))
ok("上下文含 papers/stats/synth", all(k in (row["context"] or {}) for k in ("papers", "stats", "synth")))
ok("报告已落库", len(row["report_md"] or "") > 200)

# ── 3. GET 读回 ─────────────────────────────────────────────────────────
r = client.get(f"/api/workflow/{wid}")
ok("GET 工作流 200", r.status_code == 200, r.status_code)
got = r.json["workflow"]
ok("GET 返回步骤与产物", len(got["steps"]) == 5 and got.get("report"))
ok("GET 不存在的 id -> 404", client.get("/api/workflow/99999").status_code == 404)

# ── 4. 中断 + 续跑 ───────────────────────────────────────────────────────
calls = {"n": 0}
def stop_after_two():
    calls["n"] += 1
    return calls["n"] > 3
orig_runner = wf.WorkflowRunner
class Stopping(orig_runner):
    def __init__(self, plan, **kw):
        kw["should_stop"] = stop_after_two
        super().__init__(plan, **kw)
wf.WorkflowRunner = Stopping
_, evs2 = drive({"goal": "研究图神经网络在遥感图像中的应用进展"})
wf.WorkflowRunner = orig_runner
ok("中断时 done 事件状态为 interrupted", evs2[-1]["status"] == "interrupted", evs2[-1])
snap2 = next(e for e in evs2 if e.get("type") == "snapshot")
st2 = {s["key"]: s["status"] for s in snap2["steps"]}
ok("中断后存在 interrupted 步骤", "interrupted" in st2.values(), st2)
ok("中断前的步骤保留 done", "done" in st2.values(), st2)
wid2 = next(e for e in evs2 if e.get("type") == "plan")["workflow_id"]
row2 = db.get_workflow(wid2, user_id=1)
ok("中断事件也落库", row2["status"] == "interrupted", row2["status"])
ok("中断时已完成的步骤落库", any(v["status"] == "done" for v in row2["steps"].values()))
ok("每步都发了 done 事件", {"done"} <= {e["status"] for e in evs2 if e.get("type") == "step"})
ok("中断掉的步骤落库为 interrupted",
   any(v["status"] == "interrupted" for v in row2["steps"].values()),
   {k: v["status"] for k, v in row2["steps"].items()})
ok("已完成步骤在库里不是 running",
   "running" not in {v["status"] for v in row2["steps"].values()},
   {k: v["status"] for k, v in row2["steps"].items()})

# 续跑：从断点接着跑
_, evs3 = drive({"goal": "", "workflow_id": wid2})
ok("续跑返回 resumed 标记", any(e.get("resumed") for e in evs3 if e.get("type") == "plan"))
snap3 = next(e for e in evs3 if e.get("type") == "snapshot")
st3 = {s["key"]: s["status"] for s in snap3["steps"]}
ok("续跑后全部完成", all(v == "done" for v in st3.values()), st3)
ok("续跑快照保留上一轮已完成的步骤", st3.get("search") == "done", st3)
ok("续跑最终状态 done", evs3[-1]["status"] == "done", evs3[-1])
row3 = db.get_workflow(wid2, user_id=1)
ok("续跑报告完整", "参考文献（GB/T 7714）" in (row3["report_md"] or ""))

# ── 5. 人工修正文献后续跑 ────────────────────────────────────────────────
_, evs4 = drive({"goal": "", "workflow_id": wid2, "papers": papers(3)})
snap4 = next(e for e in evs4 if e.get("type") == "snapshot")
ok("人工传入 3 篇后续跑只用这 3 篇", snap4["paperCount"] == 3, snap4["paperCount"])

# ── 6. 鉴权与校验 ───────────────────────────────────────────────────────
anon = appmod.app.test_client()
ok("未登录启动工作流 -> 401", anon.post("/api/workflow", json={"goal": "x"}).status_code == 401)
ok("未登录中断 -> 401", anon.post(f"/api/workflow/{wid}/interrupt").status_code == 401)
ok("未登录删除 -> 401", anon.delete(f"/api/workflow/{wid}").status_code == 401)
ok("空目标 -> 400", client.post("/api/workflow", json={"goal": ""}).status_code == 400)

# ── 7. 删除 ─────────────────────────────────────────────────────────────
ok("删除自己的 -> 200", client.delete(f"/api/workflow/{wid}").status_code == 200)
ok("删除后读不到", client.get(f"/api/workflow/{wid}").status_code == 404)

# ── 8. 单步失败仍出报告 ──────────────────────────────────────────────────
providers.search_papers = lambda *a, **k: json.dumps(
    {"error": "rate_limited", "message": "被限流", "retryable": True}, ensure_ascii=False)
_, evs5 = drive({"goal": "限流测试"})
snap5 = next((e for e in evs5 if e.get("type") == "snapshot"), None)
ok("检索被限流时仍有 snapshot", snap5 is not None)
if snap5:
    st5 = {s["key"]: s["status"] for s in snap5["steps"]}
    ok("检索标记 failed", st5["search"] == "failed", st5)
    ok("仍生成了报告", bool(snap5.get("report")))
    ok("done 状态为 partial", evs5[-1]["status"] == "partial", evs5[-1])

failed = [x for x in R if not x[1]]
for name, passed, got in R:
    print(("PASS  " if passed else "FAIL  ") + name + ("" if passed else "  -> got: " + repr(got)))
print("\n%d/%d checks passed" % (len(R) - len(failed), len(R)))
sys.exit(1 if failed else 0)
