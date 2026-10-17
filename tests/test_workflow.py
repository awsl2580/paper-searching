"""Test the workflow orchestrator with mocked provider + LLM."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
os.environ["S2_MIN_INTERVAL"] = "0"

import workflow as wf  # noqa: E402

R = []
def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def papers(n=6):
    out = []
    for i in range(n):
        out.append({
            "id": f"openalex:W{i}", "title": f"图神经网络研究 {i}", "year": 2018 + i,
            "authors": ["张三", "李四"], "journal": "计算机学报", "docType": "journal",
            "doi": f"10.1/{i}", "url": "u", "citationCount": i * 5,
            "openAccessPdf": "p" if i % 2 == 0 else "", "language": "zh",
            "source": "openalex", "sourceLabel": "OpenAlex", "relevance": 100,
            "abstract": f"摘要 {i}", "volume": "45", "issue": "3", "pages": "1-10",
        })
    return out


# ── 1. 自规划 ────────────────────────────────────────────────────────────
wf._call_json = lambda *a, **k: {
    "topic": "图神经网络", "query": "图神经网络 综述", "source": "cn", "limit": 15,
    "focus": "梳理主流方法与数据集", "plan": "先中文检索再归纳",
    "sections": ["研究背景", "主流方法", "存在问题"],
}
plan = wf.plan_workflow("帮我研究图神经网络的进展")
ok("规划产出 topic", plan["topic"] == "图神经网络", plan)
ok("规划产出检索词", plan["query"] == "图神经网络 综述")
ok("规划产出范围", plan["source"] == "cn")
ok("规划产出条数", plan["limit"] == 15)
ok("规划产出章节", plan["sections"] == ["研究背景", "主流方法", "存在问题"])

# 模型挂了要能降级
wf._call_json = lambda *a, **k: None
bad = wf.plan_workflow("图神经网络")
ok("规划失败时降级为原样检索", bad["query"] == "图神经网络" and bad["sections"], bad)
ok("空目标不会崩", wf.plan_workflow("")["topic"] == "")

# ── 2. 各步骤单独跑 ──────────────────────────────────────────────────────
import providers  # noqa: E402
providers.search_papers = lambda q, l=5, s="all", m=0, off=0: json.dumps(
    {"papers": papers(6), "total": 6, "sources": ["OpenAlex"], "filtered_out": 2,
     "warnings": ["Semantic Scholar 被限流"]}, ensure_ascii=False)

emit = lambda d: None
out = wf.step_search({"query": "gnn", "source": "cn", "limit": 20}, emit)
ok("检索步骤返回文献", len(out["papers"]) == 6, len(out["papers"]))
ok("检索步骤带来源与过滤数", out["sources"] == ["OpenAlex"] and out["filtered_out"] == 2)
ok("检索步骤透出 warnings", out["warnings"] == ["Semantic Scholar 被限流"])

# 检索为空要抛错
providers.search_papers = lambda q, l=5, s="all", m=0, off=0: json.dumps(
    {"papers": [], "total": 0, "sources": [], "filtered_out": 0}, ensure_ascii=False)
try:
    wf.step_search({"query": "x"}, emit)
    ok("空结果抛错", False)
except RuntimeError as e:
    ok("空结果抛错", "未检索到" in str(e), str(e))

# ── 3. 抽取（分批 + 全局序号换算）────────────────────────────────────────
providers.search_papers = lambda q, l=5, s="all", m=0, off=0: json.dumps(
    {"papers": papers(10), "total": 10, "sources": ["OpenAlex"], "filtered_out": 0}, ensure_ascii=False)
ps = papers(10)
calls = []
def fake_extract(prompt, content, **k):
    payload = json.loads(content)
    calls.append(len(payload["papers"]))
    # 每批都从 1 开始编号，模拟真实模型行为
    return {"items": [{"i": i + 1, "method": f"M{j}", "object": "O", "data": "D",
                       "metric": "acc", "finding": "F"} for j, i in
                      enumerate(range(len(payload["papers"])))]}
wf._call_json = fake_extract
ex = wf.step_extract({}, ps, emit)
ok("抽取按 8 篇一批", calls == [8, 2], calls)
ok("抽取覆盖全部文献", ex["extracted"] == 10, ex)
ok("批次内序号正确换算到全局", ps[7]["extract"]["method"] == "M7" and ps[8]["extract"]["method"] == "M0",
   (ps[7]["extract"]["method"], ps[8]["extract"]["method"]))

# 模型什么都不给时不崩、如实报告
wf._call_json = lambda *a, **k: {"items": []}
ps2 = papers(3)
ex2 = wf.step_extract({}, ps2, emit)
ok("抽取全空时不崩且计数为 0", ex2["extracted"] == 0, ex2)

# ── 4. 统计与可视化 ──────────────────────────────────────────────────────
st = wf.step_analyze({}, papers(6), emit)
ok("统计总数", st["total"] == 6)
ok("年份分布按时间排序", [y["label"] for y in st["years"]] == sorted(y["label"] for y in st["years"]),
   [y["label"] for y in st["years"]])
ok("被引分布分桶", {c["label"] for c in st["citations"]} <= {"0", "1-9", "10-99", "100+"},
   st["citations"])
ok("条形图非空", all(c["bar"] for c in st["years"]))
ok("统计全文与 DOI 数", st["withFulltext"] == 3 and st["withDoi"] == 6, (st["withFulltext"], st["withDoi"]))

# ── 5. 综述 ──────────────────────────────────────────────────────────────
wf._call_json = lambda *a, **k: {
    "sections": [{"heading": "研究背景", "body": "近年发展迅速 [1][2]。"},
                 {"heading": "主流方法", "body": "以图卷积为主 [3]。"}],
    "consensus": ["多数工作在标准数据集上验证"],
    "gaps": ["缺少跨领域迁移研究"],
    "caveat": "仅 6 篇，覆盖有限",
}
syn = wf.step_synthesize({"topic": "GNN"}, papers(6), emit, sections=["研究背景", "主流方法"])
ok("综述有章节", len(syn["sections"]) == 2)
ok("综述有共识/空白/局限", syn["consensus"] and syn["gaps"] and syn["caveat"])

wf._call_json = lambda *a, **k: None
try:
    wf.step_synthesize({}, papers(2), emit)
    ok("综述失败时抛错", False)
except RuntimeError as e:
    ok("综述失败时抛错", "综述生成失败" in str(e), str(e))

# ── 6. 参考文献与报告 ────────────────────────────────────────────────────
f = wf.format_gbt7714({"authors": ["San Zhang", "Si Li"], "title": "A Study",
                       "journal": "J", "year": 2024, "volume": "1", "issue": "2",
                       "pages": "3-4", "doi": "10.1/x", "docType": "journal"}, 1)
ok("GB/T 7714 英文作者姓前名缩写", f.startswith("[1] ZHANG S, LI S."), f[:40])
ok("GB/T 7714 含 [J] 标识", "[J]" in f)
ok("GB/T 7714 含卷期页", "1(2): 3-4" in f, f)
ok("GB/T 7714 含 DOI", "DOI: 10.1/x" in f)

f2 = wf.format_gbt7714({"authors": ["张三", "李四", "王五", "赵六"], "title": "中文标题",
                        "journal": "", "year": "未知", "docType": "conference"}, 2)
ok("中文 4 作者用「等」", "等" in f2, f2)
ok("缺刊名用占位", "[刊名不详]" in f2, f2)
ok("会议用 [C]", "[C]" in f2, f2)

# OpenAlex 对中文论文给的是「汉字+拼音」拼在一起的字符串（曾军英ZengJunying），
# GB/T 7714 的中文著录只要汉字，拼音必须去掉
ok("汉字+拼音只留汉字", wf.gbt_author("曾军英ZengJunying") == "曾军英",
   wf.gbt_author("曾军英ZengJunying"))
ok("纯汉字原样", wf.gbt_author("张三") == "张三", wf.gbt_author("张三"))
ok("汉字段内有空格也去掉", wf.gbt_author("张 三") == "张三", wf.gbt_author("张 三"))
ok("英文名不受影响", wf.gbt_author("Ashish Vaswani") == "VASWANI A",
   wf.gbt_author("Ashish Vaswani"))
ok("单段英文名照旧", wf.gbt_author("Vaswani") == "VASWANI", wf.gbt_author("Vaswani"))
f3 = wf.format_gbt7714({"authors": ["曾军英ZengJunying", "邓森耀DengSenyao",
                                    "秦传波QinChuanbo", "翟懿奎ZhaiYikui"],
                        "title": "多层次分支跨尺度融合的遥感图像语义分割网络",
                        "journal": "Laser & Optoelectronics Progress", "year": 2025,
                        "volume": "62", "issue": "4", "pages": "0428003",
                        "doi": "10.3788/lop241148", "docType": "journal"}, 1)
ok("中文参考文献不含拼音", "ZengJunying" not in f3, f3[:70])
ok("中文参考文献用「等」", "曾军英, 邓森耀, 秦传波等" in f3, f3[:70])

ok("报告作者列去掉拼音", wf.short_authors(["曾军英ZengJunying", "邓森耀DengSenyao"]) == "曾军英、邓森耀",
   wf.short_authors(["曾军英ZengJunying", "邓森耀DengSenyao"]))
ok("报告作者列超两位加等",
   wf.short_authors(["曾军英ZengJunying", "邓森耀DengSenyao", "秦传波QinChuanbo"]) == "曾军英、邓森耀等",
   wf.short_authors(["曾军英ZengJunying", "邓森耀DengSenyao", "秦传波QinChuanbo"]))
ok("报告作者列英文照旧", wf.short_authors(["Ashish Vaswani", "Noam Shazeer"]) == "Ashish Vaswani、Noam Shazeer",
   wf.short_authors(["Ashish Vaswani", "Noam Shazeer"]))
ok("报告无作者用佚名", wf.short_authors([]) == "佚名", wf.short_authors([]))

# ── 6b. 规划缓存 ────────────────────────────────────────────────────────
# 规划一次要 1.5-2s，而"中断后从断点续跑""重复点开始执行"用的目标字符串
# 完全一样，每次都调模型纯属白等。这里锁住缓存行为：命中、不跨目标复用、
# 返回副本（否则调用方就地改会污染缓存）。
calls = {"n": 0}
_orig_plan_call = wf._call_json


def counting_plan(prompt, content, **kw):
    # 判据用 PLANNER_PROMPT 里真实存在的短语（"科研工作流规划器"）。
    # 第一版按"研究方案"匹配，而那个词只出现在 SYNTH_PROMPT 里，
    # 于是这个 mock 一次都没命中、调用计数全是 0 —— 测试自己写错了。
    if "科研工作流规划器" in prompt:
        calls["n"] += 1
        return {"topic": content, "query": "q-" + str(calls["n"]), "source": "cn",
                "limit": 10, "focus": "f", "plan": "p", "sections": ["A", "B"]}
    return _orig_plan_call(prompt, content, **kw)


wf._call_json = counting_plan
wf.clear_plan_cache()

p1 = wf.plan_workflow("缓存测试目标甲")
ok("首次规划会调模型", calls["n"] == 1, calls["n"])
p2 = wf.plan_workflow("缓存测试目标甲")
ok("同一目标第二次不再调模型", calls["n"] == 1, calls["n"])
ok("命中缓存后结果一致", p1 == p2, (p1, p2))

wf.plan_workflow("缓存测试目标乙")
ok("不同目标会各自调模型", calls["n"] == 2, calls["n"])

p3 = wf.plan_workflow("缓存测试目标甲")
p3["query"] = "被改坏了"
ok("缓存返回的是副本（改动不回流）",
   wf.plan_workflow("缓存测试目标甲")["query"] == "q-1",
   wf.plan_workflow("缓存测试目标甲")["query"])

wf.clear_plan_cache()
wf.plan_workflow("缓存测试目标甲")
ok("清缓存后会重新规划", calls["n"] == 3, calls["n"])

wf._call_json = _orig_plan_call
wf.clear_plan_cache()

# ── 7. 编排器：全流程 ────────────────────────────────────────────────────
providers.search_papers = lambda q, l=5, s="all", m=0, off=0: json.dumps(
    {"papers": papers(6), "total": 6, "sources": ["OpenAlex"], "filtered_out": 0}, ensure_ascii=False)
wf._call_json = lambda prompt, content, **k: (
    {"items": [{"i": i + 1, "method": "M", "object": "O", "data": "D", "metric": "A", "finding": "F"}
               for i in range(len(json.loads(content).get("papers", [])))]}
    if "文献信息抽取" in prompt else
    {"sections": [{"heading": "H", "body": "B [1]"}], "consensus": ["c"], "gaps": ["g"], "caveat": "cv"}
)
wf.plan_workflow = lambda goal: {"topic": "GNN", "query": "gnn", "source": "all",
                                 "limit": 6, "focus": "", "plan": "p", "sections": ["H"]}

steps_seen = []
runner = wf.WorkflowRunner({"topic": "GNN", "query": "gnn", "source": "all", "limit": 6,
                            "sections": ["H"]},
                           on_step=lambda k, s, d: steps_seen.append((k, s)))
state = runner.run()
snap = runner.snapshot()
ok("五步全部执行", len(steps_seen) >= 10, steps_seen)
ok("所有步骤状态为 done", all(s["status"] == "done" for s in snap["steps"]),
   [(s["key"], s["status"]) for s in snap["steps"]])
ok("快照含文献", len(snap["papers"]) == 6)
ok("快照含统计", snap.get("stats", {}).get("total") == 6)
ok("快照含综述", bool(snap.get("synth")))
ok("快照含参考文献与报告", snap.get("references") and "# GNN" in snap.get("report", ""))
ok("报告含 GB/T 7714 章节", "参考文献（GB/T 7714）" in snap["report"])
ok("报告含统计代码块", "```" in snap["report"])

# ── 8. 中断：在指定步骤后停 ─────────────────────────────────────────────
stop_after = {"n": 0}
def should_stop():
    stop_after["n"] += 1
    return stop_after["n"] > 4
r2 = wf.WorkflowRunner({"topic": "GNN", "query": "gnn", "source": "all", "limit": 6, "sections": ["H"]},
                       on_step=lambda k, s, d: None, should_stop=should_stop)
s2 = r2.run()
snap2 = r2.snapshot()
statuses = [s["status"] for s in snap2["steps"]]
ok("中断后存在 interrupted 状态", "interrupted" in statuses, statuses)
ok("中断前的步骤保留 done", "done" in statuses, statuses)

# ── 9. 续跑：从指定步骤开始 + 复用上下文 ─────────────────────────────────
steps3 = []
r3 = wf.WorkflowRunner({"topic": "GNN", "query": "gnn", "source": "all", "limit": 6, "sections": ["H"]},
                       on_step=lambda k, s, d: steps3.append(k))
r3.run(start_from="synthesize", previous={"papers": papers(4)})
snap3 = r3.snapshot()
ok("续跑跳过了 search/extract", "search" not in steps3 and "extract" not in steps3, steps3)
ok("续跑从 synthesize 开始", steps3[:3] == ["synthesize", "synthesize", "analyze"] or
   steps3[0] == "synthesize", steps3[:4])
ok("续跑复用了传入的文献", len(snap3["papers"]) == 4, len(snap3["papers"]))

# ── 10. 跳过步骤 ────────────────────────────────────────────────────────
r4 = wf.WorkflowRunner({"topic": "GNN", "query": "gnn", "source": "all", "limit": 6, "sections": ["H"]})
r4.run(skip=["extract"])
snap4 = r4.snapshot()
st4 = {s["key"]: s["status"] for s in snap4["steps"]}
ok("跳过的步骤标记为 skipped", st4["extract"] == "skipped", st4)

# ── 11. 单步失败不拖垮全流程 ─────────────────────────────────────────────
providers.search_papers = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
r5 = wf.WorkflowRunner({"topic": "GNN", "query": "gnn", "source": "all", "limit": 6, "sections": ["H"]})
r5.run()
snap5 = r5.snapshot()
st5 = {s["key"]: s["status"] for s in snap5["steps"]}
ok("检索失败标记 failed 且后续仍执行", st5["search"] == "failed" and st5["format"] == "done", st5)
ok("失败步骤带错误信息", any(s.get("error") for s in snap5["steps"] if s["key"] == "search"))

failed = [x for x in R if not x[1]]
for name, passed, got in R:
    print(("PASS  " if passed else "FAIL  ") + name + ("" if passed else "  -> got: " + repr(got)))
print("\n%d/%d checks passed" % (len(R) - len(failed), len(R)))
sys.exit(1 if failed else 0)
