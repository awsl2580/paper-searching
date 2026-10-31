"""AI4S 闭环测试 —— 「假设—实验—计算—验证—迭代」这条链真的能跑通吗？

这是本项目最核心的一条用例。它验的不是"接口返回 200"，而是：

1. **九步都真的执行**，且每步产物都在快照里（假设/实验/计算/验证/迭代）；
2. **"算"用的是真计算**：计算结果带 `repro`，能在"验"里被**重算**并比对一致；
3. **"验"能拦住假数**：把计算结果改掉一个数，重算必然不一致 -> verdict=revise；
4. **迭代真的会回环**：审稿人说 revise 时，编排器回到"假设"重跑，且带着返工意见；
5. **迭代有上限**：到上限如实报"未收敛"，不假装通过；
6. **模型全挂也能出报告**：降级路径用文献统计事实生成假设，并标记 degraded；
7. **用户数据集优先**：给一段谱图数据，"算"会解析它并做峰检测。

全程离线：`providers.search_papers` 与 `wf._call_json` 都被替换成脚本化假实现，
MCP 走同进程传输，不碰网络。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import workflow as wf  # noqa: E402
import providers  # noqa: E402
import mcp_client  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def papers(n=10):
    out = []
    for i in range(n):
        out.append({
            "id": f"openalex:W{i}", "title": f"图神经网络遥感分割研究 {i}",
            "year": 2016 + i, "authors": ["张三", "李四"], "journal": "遥感学报",
            "docType": "journal", "doi": f"10.1/{i}", "url": f"https://x/{i}",
            "citationCount": i * 3, "openAccessPdf": "p" if i % 3 == 0 else "",
            "language": "zh", "source": "openalex", "sourceLabel": "OpenAlex",
            "relevance": 95, "abstract": f"摘要 {i}", "volume": "45", "issue": "3",
            "pages": "1-10",
        })
    return out


SEARCH_CALLS = []


def fake_search(query, limit=5, source="all", min_relevance=0, offset=0):
    SEARCH_CALLS.append(query)
    ps = papers(10)
    return json.dumps({"papers": ps, "total": len(ps), "sources": ["OpenAlex"],
                       "filtered_out": 2, "warnings": []}, ensure_ascii=False)


providers.search_papers = fake_search

# ── 脚本化模型：按提示词区分角色 ─────────────────────────────────
STATE = {"verdicts": ["pass"], "hyp_calls": [], "critic_calls": 0}


def fake_llm(prompt, content, **kw):
    if "文献信息抽取" in prompt:
        items = json.loads(content).get("papers", [])
        return {"items": [{"i": i + 1, "method": "GCN", "object": "遥感影像",
                           "data": "AID", "metric": "mIoU", "finding": "提升 3%",
                           "numbers": [{"metric": "mIoU", "value": 82.3, "unit": "%"}]}
                          for i in range(len(items))]}
    if "假设提出者" in prompt:
        STATE["hyp_calls"].append(content)
        return {
            "consensus": ["GCN 类方法在遥感分割上是主流"],
            "gaps": ["缺少高光谱条件下的验证"],
            "hypotheses": [{
                "statement": "在 AID 数据集上，GCN 的 mIoU 高于 CNN 基线",
                "basis": [1, 2], "mechanism": "推测：图结构显式建模空间关系",
                "iv": "模型类型", "dv": "mIoU", "control": "数据集与训练轮数",
                "falsify": "若 CNN 在相同设置下 mIoU 不低于 GCN，则否定本假设",
                "prediction": "GCN 组 mIoU 高出 3 个百分点",
                "confidence": "medium"}],
            "reasoning": "从'缺少高光谱验证'这一空白出发，先验证最基础的方法比较。",
        }
    if "实验设计师" in prompt:
        return {
            "summary": "2×2 全因子对照实验（模型 × 数据量）",
            "factors": [{"name": "模型", "levels": ["GCN", "CNN"], "unit": ""},
                        {"name": "数据量", "levels": ["小", "大"], "unit": "样本"}],
            "design_type": "full-factorial", "n_runs": 4,
            "power": {"effect_size": 0.6, "note": "中等效应"},
            "metrics": ["mIoU"],
            "success_criteria": ["GCN 组 mIoU 高出 ≥3 个百分点且 p<0.05"],
            "failure_criteria": ["两组差异 <1 个百分点或 p≥0.05"],
            "steps": ["构造 4 组样本", "训练并评估", "做双样本检验", "复核数值"],
            "needed_calculations": [
                {"tool": "stats_ttest",
                 "args": {"x": [82.3, 81.9, 82.8, 83.1], "y": [79.1, 78.8, 79.6, 79.9]},
                 "purpose": "比较 GCN 与 CNN 的 mIoU"}],
            "limitations": "数据量来自文献报告值，属于复现实验而非新实验",
        }
    if "审稿人" in prompt:
        STATE["critic_calls"] += 1
        idx = min(STATE["critic_calls"] - 1, len(STATE["verdicts"]) - 1)
        v = STATE["verdicts"][idx]
        fixes = ["重新估计样本量"] if v == "revise" else []
        return {"verdict": v,
                "checks": [{"name": "数值一致性", "ok": v == "pass",
                            "detail": "与工具返回一致" if v == "pass" else "样本量偏小"}],
                "fixes": fixes, "open_questions": ["高光谱数据仍未验证"],
                "confidence": "high", "summary": "复核完成"}
    if "综述撰写" in prompt:
        return {"sections": [{"heading": "研究背景", "body": "近年发展迅速 [1][2]。"},
                             {"heading": "主流方法", "body": "以图卷积为主 [3]。"}],
                "consensus": ["GCN 主流"], "gaps": ["缺少高光谱验证"],
                "caveat": "仅 10 篇，覆盖有限"}
    return {"topic": "图神经网络遥感", "query": "图神经网络 遥感", "source": "all",
            "limit": 10, "focus": "方法与指标", "plan": "先检索再归纳",
            "sections": ["研究背景", "主流方法"], "research_type": "mechanism",
            "metrics": ["mIoU"], "queries": ["图神经网络 遥感", "GNN remote sensing"]}


wf._call_json = fake_llm
wf.clear_plan_cache()

PLAN = {"topic": "图神经网络遥感分割", "query": "图神经网络 遥感", "queries": [],
        "source": "all", "limit": 10, "focus": "方法与指标",
        "sections": ["研究背景", "主流方法"], "research_type": "mechanism",
        "metrics": ["mIoU"]}

# ── 1. 多检索式合并去重（"搜"这一步的召回策略）────────────────────
out = wf.step_search({"query": "图神经网络 遥感",
                      "queries": ["图神经网络 遥感", "GNN remote sensing"],
                      "source": "all", "limit": 10}, lambda d: None)
ok("多检索式各跑了一次", len(SEARCH_CALLS) >= 2, SEARCH_CALLS[:3])
ok("跨检索式按 DOI/标题去重（10 篇不翻倍）", out["count"] == 10, out["count"])
ok("透出检索式清单", out["queries"][0] == "图神经网络 遥感", out["queries"])
ok("透出过滤数", out["filtered_out"] >= 2, out["filtered_out"])

# ── 2. 全流程：九步都跑完 ────────────────────────────────────────
seen = []
runner = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: seen.append((k, s)))
runner.run()
snap = runner.snapshot()
status = {s["key"]: s["status"] for s in snap["steps"]}
ok("九个步骤全部 done", all(v == "done" for v in status.values()), status)
ok("步骤顺序正确",
   [s["key"] for s in snap["steps"]] ==
   ["search", "extract", "analyze", "hypothesis", "experiment",
    "compute", "verify", "synthesize", "format"],
   [s["key"] for s in snap["steps"]])
ok("core 覆盖搜读想做算验写",
   [s["core"] for s in snap["steps"]] == ["搜", "读", "读", "想", "做", "算", "验", "写", "写"],
   [s["core"] for s in snap["steps"]])

# ── 3. 假设：可证伪 + 依据 + 变量 ───────────────────────────────
hyp = snap.get("hypothesis") or {}
hs = hyp.get("hypotheses") or []
ok("产出 1 条假设", len(hs) == 1, len(hs))
ok("假设带文献依据", hs[0]["basis"] == [1, 2], hs[0].get("basis") if hs else None)
ok("假设带否定判据", "否定本假设" in hs[0]["falsify"], hs[0].get("falsify"))
ok("假设带变量与预测",
   bool(hs[0]["iv"] and hs[0]["dv"] and hs[0]["prediction"]))
ok("假设不是降级产物", hyp.get("degraded") is False, hyp.get("degraded"))
ok("带共识与缺口", bool(hyp.get("consensus")) and bool(hyp.get("gaps")))

# ── 4. 实验方案：因子 + 真设计矩阵 + 真样本量 ────────────────────
exp = snap.get("experiment") or {}
ok("方案有 2 个因子", len(exp.get("factors") or []) == 2, exp.get("factors"))
ok("设计类型是全因子", exp.get("design_type") == "full-factorial", exp.get("design_type"))
ok("设计矩阵由 MCP 工具生成（2 因子 2 水平 = 4 组）",
   len((exp.get("design") or {}).get("runs") or []) == 4,
   (exp.get("design") or {}).get("runs"))
# 交叉核对：报告里的样本量必须等于设计工具独立算出来的值（不是模型编的）
_power = mcp_client.call_tool("design_power", {"effect_size": 0.6})
ok("样本量来自功效分析（与工具独立重算一致）",
   (exp.get("power") or {}).get("n_per_group") == _power["result"]["result"]["n_per_group"],
   ((exp.get("power") or {}).get("n_per_group"), _power["result"]["result"]["n_per_group"]))
ok("有成功/否定判据", bool(exp.get("success_criteria")) and bool(exp.get("failure_criteria")))
ok("有可执行步骤", len(exp.get("steps") or []) >= 3)

# ── 5. 计算：真数值 + repro ─────────────────────────────────────
comp = snap.get("computation") or {}
comps = comp.get("computations") or []
ok("至少跑了 2 项计算", len(comps) >= 2, len(comps))
by_tool = {c["tool"]: c for c in comps}
ok("假设驱动的统计检验真的执行了", "stats_ttest" in by_tool, list(by_tool))
ok("统计检验给出真实 p 值",
   by_tool["stats_ttest"]["result"]["p"] < 0.01, by_tool["stats_ttest"]["result"].get("p"))
ok("每项计算都带 repro", all(c.get("repro") and c["repro"].get("tool") for c in comps))
ok("计算标注了来源（假设驱动/文献计量）",
   {c["source"] for c in comps} <= {"hypothesis-driven", "bibliometric", "user-dataset"},
   {c["source"] for c in comps})
ok("有文献计量类计算作为交叉验证", any(c["source"] == "bibliometric" for c in comps))

# ── 6. 验证：自动检查 + 复算一致 ────────────────────────────────
ver = snap.get("verify") or {}
ok("验证结论为 pass", ver.get("verdict") == "pass", ver.get("verdict"))
ok("复算全部一致", ver.get("replay", {}).get("same") == ver.get("replay", {}).get("total")
   and ver["replay"]["total"] >= 2, ver.get("replay"))
check_names = [c["name"] for c in ver.get("checks") or []]
for must in ("数值一致性", "证据可追溯", "假设可证伪", "可移交性"):
    ok(f"验证含「{must}」检查", must in check_names, check_names)
ok("审稿人意见被采纳", ver.get("llm_verdict") == "pass", ver.get("llm_verdict"))
ok("一轮通过不产生额外迭代", snap.get("iteration") == 0, snap.get("iteration"))

# ── 7. 报告：AI4S 章节齐全 ─────────────────────────────────────
report = snap.get("report") or ""
for sec in ("## 摘要", "## 一、检索概况", "## 二、科学假设与逻辑推导",
            "## 三、实验方案设计与仿真", "## 四、科学计算结果",
            "## 五、复现验证与迭代", "## 六、综述正文", "## 七、统计与可视化",
            "## 九、参考文献（GB/T 7714）", "## 十、复现清单"):
    ok(f"报告含「{sec.strip('# ')}」", sec in report, sec)
ok("报告里有工具算出的 p 值（JSON 块里的 p 字段）", '"p":' in report,
   report[report.find("科学计算结果"):report.find("科学计算结果") + 80])
ok("报告里的 p 值与工具返回一致",
   str(round(by_tool["stats_ttest"]["result"]["p"], 6)) in report,
   round(by_tool["stats_ttest"]["result"]["p"], 6))
ok("报告含否定判据", "否定判据" in report)
ok("报告含迭代记录", "迭代记录" in report)
ok("参考文献有条目", "[1]" in report)

# ── 8. 验能拦住假数：篡改一个计算结果，重算必然不一致 ───────────
tampered = json.loads(json.dumps(comp))
tampered["computations"][0]["result"]["p"] = 0.0001   # 改成"更好看"的假 p
v2 = wf.step_verify(dict(PLAN), papers(10), hyp, exp, tampered,
                    lambda d: None, iteration=0)
ok("篡改数值后判定不再是 pass", v2["verdict"] == "revise", v2["verdict"])
ok("不一致项被点名", v2["replay"]["same"] < v2["replay"]["total"], v2["replay"])
ok("给出了返工项", bool(v2["fixes"]), v2["fixes"][:1])
ok("数值一致性检查为不通过",
   any(c["name"] == "数值一致性" and not c["ok"] for c in v2["checks"]),
   [(c["name"], c["ok"]) for c in v2["checks"]])

# ── 9. 迭代：审稿人要求返工 -> 回到"假设"并带反馈 ────────────────
STATE["verdicts"] = ["revise", "pass"]
STATE["critic_calls"] = 0
STATE["hyp_calls"] = []
seen2 = []
r2 = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: seen2.append((k, s)))
r2.max_iterations = 1
r2.run()
snap2 = r2.snapshot()
ok("发生了第二轮", snap2.get("iteration") == 1, snap2.get("iteration"))
ok("迭代记录有两轮", len(snap2.get("iterations") or []) == 2,
   len(snap2.get("iterations") or []))
ok("第一轮是 revise", (snap2["iterations"][0].get("verdict")) == "revise",
   snap2["iterations"][0])
ok("第一轮记录的动作是返工", "返工" in (snap2["iterations"][0].get("action") or ""),
   snap2["iterations"][0].get("action"))
ok("第二轮最终 pass", (snap2["iterations"][1].get("verdict")) == "pass",
   snap2["iterations"][1])
ok("假设步骤被重新执行（每轮一次，共 2 次）", len(STATE["hyp_calls"]) == 2,
   len(STATE["hyp_calls"]))
ok("第二次提假设时带上了上一轮反馈",
   "上一轮验证反馈" in STATE["hyp_calls"][1], STATE["hyp_calls"][1][:120])
ok("反馈里包含返工项", "重新估计样本量" in STATE["hyp_calls"][1])
ok("迭代时步骤状态标了轮次",
   any(s.get("iteration") == 1 for s in snap2["steps"] if s["key"] == "hypothesis"),
   [(s["key"], s.get("iteration")) for s in snap2["steps"]])
ok("最终仍全部 done",
   all(s["status"] == "done" for s in snap2["steps"]),
   {s["key"]: s["status"] for s in snap2["steps"]})

# ── 10. 迭代上限：一直 revise 就如实报"未收敛" ──────────────────
STATE["verdicts"] = ["revise"]
STATE["critic_calls"] = 0
r3 = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: None)
r3.max_iterations = 1
r3.run()
snap3 = r3.snapshot()
ok("到上限就停下（不再无限重试）", snap3.get("iteration") == 1, snap3.get("iteration"))
ok("最后一轮 verdict 是 revise", snap3["iterations"][-1].get("verdict") == "revise",
   snap3["iterations"][-1])
ok("如实标注未收敛", "未收敛" in (snap3["iterations"][-1].get("action") or ""),
   snap3["iterations"][-1].get("action"))
ok("报告里写明未收敛", "未收敛" in (snap3.get("report") or "") or
   "revise" in (snap3.get("report") or ""))

# ── 11. 降级：模型全挂也要产出可用报告 ──────────────────────────
wf._call_json = lambda *a, **k: None
r4 = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: None)
r4.run()
snap4 = r4.snapshot()
st4 = {s["key"]: s["status"] for s in snap4["steps"]}
ok("模型挂了：检索/统计/计算/验证/格式化仍 done",
   all(st4[k] == "done" for k in ("search", "analyze", "compute", "verify", "format")), st4)
ok("模型挂了：综述步骤如实失败", st4["synthesize"] == "failed", st4["synthesize"])
ok("降级假设被标记", (snap4.get("hypothesis") or {}).get("degraded") is True,
   (snap4.get("hypothesis") or {}).get("degraded"))
ok("降级假设仍带否定判据",
   all(h.get("falsify") for h in (snap4["hypothesis"]["hypotheses"])),
   snap4["hypothesis"]["hypotheses"])
ok("降级方案仍生成设计矩阵",
   bool((snap4.get("experiment") or {}).get("design")), (snap4.get("experiment") or {}).get("design"))
ok("降级时仍有真计算", (snap4.get("computation") or {}).get("count", 0) >= 1,
   (snap4.get("computation") or {}).get("count"))
ok("降级时报告仍生成", len(snap4.get("report") or "") > 1000, len(snap4.get("report") or ""))
ok("报告写明降级", "降级" in (snap4.get("report") or ""))

# ── 12. 用户数据集优先：谱图数据 -> 解析 + 峰检测 ───────────────
wf._call_json = fake_llm
DATASET = ("x,y\n" + "\n".join(
    f"{i},{round(0.05 + (1.0 if abs(i - 4) < 0.7 else 0) + (1.2 if abs(i - 12) < 0.7 else 0), 3)}"
    for i in range(20)))
r5 = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: None,
                       dataset=DATASET, dataset_kind="spectra")
r5.run()
snap5 = r5.snapshot()
tools5 = [c["tool"] for c in (snap5.get("computation") or {}).get("computations") or []]
ok("用户数据集被解析", "table_parse" in tools5, tools5)
ok("谱图数据走了峰检测", "spectra_summary" in tools5 or "spectra_peaks" in tools5, tools5)
ok("数据集来源被标注",
   any(c["source"] == "user-dataset"
       for c in (snap5.get("computation") or {}).get("computations") or []))
ok("报告里写明数据来源", "用户数据集" in (snap5.get("report") or ""))
smiles_run = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: None,
                               dataset="CCO\nc1ccccc1\nCC(=O)O", dataset_kind="molecule")
smiles_run.run()
tools6 = [c["tool"] for c in (smiles_run.snapshot().get("computation") or {}).get("computations") or []]
ok("SMILES 数据集走分子工具", "molecule_batch" in tools6 or "molecule_parse" in tools6, tools6)

# ── 13. 中断语义在新流程里仍然成立 ──────────────────────────────
calls = {"n": 0}


def stop_after_three():
    # 调用点：1 search顶 / 2 extract顶 / 3 批1 / 4 批2 / 5 analyze顶
    calls["n"] += 1
    return calls["n"] > 4


r6 = wf.WorkflowRunner(dict(PLAN), on_step=lambda k, s, d: None,
                       should_stop=stop_after_three)
r6.run()
st6 = {s["key"]: s["status"] for s in r6.snapshot()["steps"]}
ok("中断后恰好一个断点",
   sum(1 for v in st6.values() if v == "interrupted") == 1, st6)
ok("中断前的步骤保持 done", st6.get("search") == "done" and st6.get("extract") == "done", st6)
ok("断点落在后面的步骤", st6.get("analyze") == "interrupted", st6)

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
