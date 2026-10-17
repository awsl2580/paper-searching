"""科研工作流编排器 —— 命题书要求的"通用智能体工作流框架"落地。

命题要求的能力与这里的对应关系：

    ① 文献自动检索与筛选          step: search
    ② 文献信息抽取与综述生成      step: extract -> synthesize
    ③ 实验数据统计与可视化        step: analyze
    ④ 参考文献格式自动化          step: format（复用前端的 GB/T 7714 等格式）
    自规划 / 任务移交             plan_workflow()：由模型读研究目标产出步骤计划
    工作流状态管理、中断/恢复      WorkflowRunner：每步落库，可暂停、跳过、续跑

设计取舍：

* **确定性编排，而非让模型自由调工具。** 步骤由 PLANNER 提示词规划出来，
  但每一步做什么是代码写死的（检索就是检索、抽取就是抽取）。
  模型只在"读文本"的地方发挥（抽取、综述），这样耗时和费用可预期，
  也才能做到"中断后从第 N 步续跑"——自由工具循环是没法恢复的。

* **步骤状态落库。** 每步开始/结束都写库，所以刷新页面能看到进度，
  中断后也能接着跑。这是命题里"工作流状态管理"的实际含义。

* **失败不炸整条流水线。** 单步失败标记为 failed 并记下原因，
  后续步骤照跑（综述会如实说明哪一步没成功），最后给出部分成果。
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import providers

# ── 步骤定义 ────────────────────────────────────────────────
# key          步骤标识（落库、前端状态用）
# title        给人看的中文名
# core         对应命题书的哪个"核心功能"（前端用来标注）
STEPS = [
    {"key": "search",     "title": "文献检索与筛选", "core": "①",
     "detail": "多源并发检索，按相关度过滤并排序"},
    {"key": "extract",    "title": "信息抽取",       "core": "②",
     "detail": "逐篇抽取研究对象、方法、数据、结论"},
    {"key": "synthesize", "title": "综述生成",       "core": "②",
     "detail": "归纳研究现状、分歧与空白，产出带引用的综述"},
    {"key": "analyze",    "title": "统计与可视化",   "core": "③",
     "detail": "年份分布、被引分布、文献类型与来源统计"},
    {"key": "format",     "title": "参考文献格式化", "core": "④",
     "detail": "生成 GB/T 7714 参考文献表并整合成报告"},
]

STEP_KEYS = [s["key"] for s in STEPS]

# ── 提示词 ──────────────────────────────────────────────────

PLANNER_PROMPT = """你是科研工作流规划器。用户给出一个研究目标，你要产出一份可执行的检索研究计划。

只输出 JSON：
- "topic"：把研究目标凝练成一个检索主题（中文主题用中文，英文主题用英文），
  去掉"帮我研究""写一篇"这类祈使语，保留专业术语。
- "query"：实际送去检索的关键词，可以比 topic 更精炼（用空格分隔的 2-4 个词）。
- "source"："all" | "cn" | "en"。用户提到中文文献/国内研究给 "cn"，
  英文/外文/SCI 给 "en"，否则 "all"。
- "limit"：检索条数，10-30 的整数，默认 20。
- "focus"：一句话说明这个综述应重点回答什么（15-40 字）。
- "plan"：一句话说明你的检索思路（给用户看，30 字以内）。
- "sections"：综述的 3-5 个章节标题（字符串数组），要贴合 topic，
  例如 ["研究背景与问题", "主流方法", "数据集与评价指标", "存在问题与展望"]。

只输出 JSON，不要解释文字或 markdown 代码块。"""

EXTRACT_PROMPT = """你在做文献信息抽取。下面给你若干篇论文的标题和摘要。

对**每一篇**抽取：
- "i"：论文序号（从 1 开始，与输入顺序一致）
- "object"：研究对象 / 应用场景（10 字以内，没有就空字符串）
- "method"：核心方法或模型名（15 字以内）
- "data"：使用的数据集或数据来源（20 字以内，没有就空字符串）
- "metric"：评价指标（20 字以内，没有就空字符串）
- "finding"：主要结论（30 字以内）

严格约束：**只依据给出的标题和摘要**，没有的信息留空字符串，不要猜测、不要编造。
输出 JSON：{"items": [ {...}, ... ]}。只输出 JSON。"""

SYNTH_PROMPT = """你是学术综述撰写助手。根据给定的文献列表（已抽取过要点），写一篇中文mini综述。

硬性约束：
1. **只能依据给定文献**。不得引入列表之外的论文、作者、年份、数字。
2. 引用文献时用 [序号] 标注，序号必须对应给定列表的编号。不要编造序号。
3. 如果文献数量少或质量参差，要**如实说明**局限，不要硬凑成"研究充分"。
4. 不要逐篇复述摘要 —— 要按主题/方法归类，指出共识、分歧与空白。
5. 中文输出，学术但平实。

输出 JSON：
- "sections"：[{"heading": "章节标题", "body": "该节正文（可含 [序号] 引用）"}]
  章节标题请采用给定的章节列表；某节确实无内容可写时可以省略该节。
- "consensus"：2-4 条当前文献的共识或主流做法（每条一句话）
- "gaps"：2-3 条研究空白或不足（每条一句话）
- "caveat"：一句话说明本篇综述的局限（文献量、来源覆盖等）

只输出 JSON。"""


# ── 小工具 ──────────────────────────────────────────────────

# 默认模型名。与 app.py 的 DEEPSEEK_MODEL 保持同一个值 ——
# 原来这里写死 "deepseek-chat"、app.py 也写死 "deepseek-chat"，
# 一旦 .env 里没设 DEEPSEEK_MODEL，两边就各用各的默认值；
# 如果只改了 app.py 而忘了这里，聊天用一个模型、工作流用另一个，
# 排查起来会非常费解。所以两边都从这里/那里读同一个环境变量。
DEFAULT_MODEL = (os.getenv("DEEPSEEK_MODEL") or "").strip() or "deepseek-flash"


def _call_json(prompt, user_content, max_tokens=1200, temperature=0):
    """调一次模型并解析 JSON；失败返回 None（由调用方决定降级）。"""
    try:
        from langchain_deepseek import ChatDeepSeek
        llm = ChatDeepSeek(
            model=DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        resp = llm.invoke([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        return _loads_lenient(raw)
    except Exception:
        return None


def _loads_lenient(raw):
    """模型偶尔包 markdown 代码块或前后带话，这里容错取出 JSON。"""
    t = (raw or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 退一步：抓第一个平衡的 {...}
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def _compact(papers, title_chars=110, abstract_chars=260):
    out = []
    for i, p in enumerate(papers, 1):
        out.append({
            "i": i,
            "title": (p.get("title") or "")[:title_chars],
            "abstract": (p.get("abstract") or "")[:abstract_chars],
            "year": p.get("year"),
            "venue": (p.get("journal") or p.get("venue") or "")[:40],
        })
    return out


# ── ① 检索与筛选 ────────────────────────────────────────────

def step_search(cfg, emit):
    query = cfg.get("query") or cfg.get("topic") or ""
    source = cfg.get("source") or "all"
    limit = int(cfg.get("limit") or 20)
    emit(f"检索「{query}」（范围 {source}，取 {limit} 条）")

    payload = json.loads(providers.search_papers(query, limit, source, 0))
    if payload.get("error"):
        raise RuntimeError(payload.get("message") or payload["error"])

    papers = payload.get("papers") or []
    if not papers:
        raise RuntimeError(f"未检索到与「{query}」相关的文献")

    warnings = payload.get("warnings") or []
    dropped = payload.get("filtered_out") or 0
    emit(f"取回 {len(papers)} 篇" + (f"，过滤掉 {dropped} 篇低相关度结果" if dropped else ""))

    return {
        "papers": papers,
        "count": len(papers),
        "sources": payload.get("sources") or [],
        "filtered_out": dropped,
        "warnings": warnings,
    }


# ── ② 信息抽取 ──────────────────────────────────────────────

def step_extract(cfg, papers, emit, stop=None):
    """分批抽取，**多批并发**。

    为什么要并发：摘要里说得很清楚，实测 18 篇要 3 批、每批约 1.8~2.5s，
    串行跑就是 6-7s 纯等；而每批之间**没有任何依赖**（各自的输入是文献切片，
    输出按 id 回填），天然可并行。并发后这一段的耗时≈最慢的那一批。

    并发度限制在 3：再高对模型接口和本机都没好处，而且论文数通常就那么几篇，
    批次本来就不多。中断语义保持"批次边界"：已发出的批次会跑完（结果照收），
    不再发新的批次。
    """
    if not papers:
        raise RuntimeError("上一步没有取到文献，无法抽取")
    batches = [papers[i:i + 8] for i in range(0, len(papers), 8)]
    items = {}
    emit(f"分 {len(batches)} 批抽取 {len(papers)} 篇的研究对象/方法/数据/指标/结论")

    def one_batch(bi, batch):
        """跑一批，返回 (编号, 解析结果)。异常不抛出，交给调用方记账。"""
        try:
            parsed = _call_json(EXTRACT_PROMPT,
                                json.dumps({"papers": _compact(batch)}, ensure_ascii=False),
                                max_tokens=1600)
            return bi, (parsed or {}).get("items") or [], None
        except Exception as e:  # noqa: BLE001 - 单批失败不该拖垮整步
            return bi, [], f"{type(e).__name__}: {e}"

    interrupted = False
    failures = []
    done_count = 0

    if len(batches) == 1:
        # 只有一批时不必开线程池（省掉调度开销，也让中断检查更直接）
        if stop and stop():
            emit("第 1/1 批开始前收到中断")
            return {"extracted": 0, "total": len(papers), "interrupted": True}
        bi, got, err = one_batch(1, batches[0])
        results = [(bi, got, err)]
    else:
        # 先按中断状态决定"发几批"：已经在跑的不打断，没发的就不发了
        to_run = []
        for bi, batch in enumerate(batches, 1):
            if stop and stop():
                interrupted = True
                break
            to_run.append((bi, batch))
        if not to_run:
            emit(f"第 1/{len(batches)} 批开始前收到中断")
            return {"extracted": 0, "total": len(papers), "interrupted": True}
        if interrupted:
            emit(f"收到中断，只跑前 {len(to_run)}/{len(batches)} 批")
        with ThreadPoolExecutor(max_workers=min(3, len(to_run))) as pool:
            futures = [pool.submit(one_batch, bi, b) for bi, b in to_run]
            results = []
            for fut in as_completed(futures):
                bi, got, err = fut.result()
                results.append((bi, got, err))
                done_count += 1
                emit(f"第 {done_count}/{len(to_run)} 批完成")

    for bi, got, err in results:
        if err:
            failures.append(f"第 {bi} 批: {err}")
            continue
        batch = batches[bi - 1]
        for it in got:
            if not isinstance(it, dict):
                continue
            try:
                # 批次内序号要换算成全局序号
                gi = int(it.get("i")) - 1
                if 0 <= gi < len(batch):
                    items[id(batch[gi])] = it
            except (TypeError, ValueError):
                continue
    if len(batches) == 1 and not failures:
        emit("抽取完成")

    filled = 0
    for p in papers:
        it = items.get(id(p)) or {}
        p["extract"] = {
            "object": str(it.get("object") or "").strip(),
            "method": str(it.get("method") or "").strip(),
            "data": str(it.get("data") or "").strip(),
            "metric": str(it.get("metric") or "").strip(),
            "finding": str(it.get("finding") or "").strip(),
        }
        if any(p["extract"].values()):
            filled += 1

    if failures:
        # 部分批次失败：已抽到的照常用，并如实说明
        emit(f"{len(failures)}/{len(batches)} 批抽取失败，其余批次照常使用")
    if filled == 0:
        emit("模型未能抽出结构化字段，后续综述将只依据标题与摘要")
    else:
        emit(f"{filled}/{len(papers)} 篇抽取到结构化信息")
    return {"extracted": filled, "total": len(papers), "interrupted": interrupted}


# ── ③ 统计与可视化 ──────────────────────────────────────────

def _bar(value, max_value, width=28):
    """纯文本条形图。不引第三方绘图库：报告要能直接贴进 Word/Markdown。"""
    if not max_value:
        return ""
    n = max(1, round(value / max_value * width))
    return "█" * n


def step_analyze(cfg, papers, emit):
    if not papers:
        raise RuntimeError("没有文献可统计")

    years, cites, types, srcs, langs = {}, {}, {}, {}, {}
    for p in papers:
        y = str(p.get("year") or "未知")
        years[y] = years.get(y, 0) + 1
        c = p.get("citationCount") or 0
        bucket = "0" if c == 0 else ("1-9" if c < 10 else ("10-99" if c < 100 else "100+"))
        cites[bucket] = cites.get(bucket, 0) + 1
        types[p.get("docType") or "journal"] = types.get(p.get("docType") or "journal", 0) + 1
        srcs[p.get("sourceLabel") or p.get("source") or "未知"] = \
            srcs.get(p.get("sourceLabel") or p.get("source") or "未知", 0) + 1
        langs[p.get("language") or "未知"] = langs.get(p.get("language") or "未知", 0) + 1

    def mk(d):
        # 年份按时间排序，其余按数量倒序
        items = sorted(d.items(), key=lambda kv: kv[0]) if d is years else \
            sorted(d.items(), key=lambda kv: -kv[1])
        top = max(v for _, v in items) if items else 0
        return [{"label": k, "count": v, "bar": _bar(v, top)} for k, v in items]

    stats = {
        "total": len(papers),
        "years": mk(years),
        "citations": mk(cites),
        "docTypes": mk(types),
        "sources": mk(srcs),
        "languages": mk(langs),
        "withFulltext": sum(1 for p in papers if p.get("openAccessPdf")),
        "withDoi": sum(1 for p in papers if p.get("doi")),
        "medianCitations": sorted((p.get("citationCount") or 0) for p in papers)[len(papers) // 2],
    }
    emit(f"统计完成：{stats['total']} 篇，{len(years)} 个年份，"
         f"{stats['withFulltext']} 篇有全文直链")
    return stats


# ── ④ 综述生成 ──────────────────────────────────────────────

def step_synthesize(cfg, papers, emit, sections=None, stop=None):
    if not papers:
        raise RuntimeError("没有文献，无法生成综述")
    if stop and stop():
        emit("收到中断，未开始撰写综述")
        return {"sections": [], "consensus": [], "gaps": [], "caveat": "", "interrupted": True}
    emit(f"阅读 {len(papers)} 篇并撰写综述")

    brief = []
    for i, p in enumerate(papers, 1):
        ex = p.get("extract") or {}
        brief.append({
            "i": i,
            "title": p.get("title"),
            "year": p.get("year"),
            "venue": p.get("journal") or p.get("venue"),
            "object": ex.get("object", ""),
            "method": ex.get("method", ""),
            "data": ex.get("data", ""),
            "metric": ex.get("metric", ""),
            "finding": ex.get("finding", ""),
            "abstract": (p.get("abstract") or "")[:200],
        })

    parsed = _call_json(SYNTH_PROMPT, json.dumps({
        "研究目标": cfg.get("topic"),
        "重点关注": cfg.get("focus"),
        "章节": sections or [],
        "文献": brief,
    }, ensure_ascii=False), max_tokens=3000, temperature=0.2)

    if not parsed:
        raise RuntimeError("综述生成失败（模型未返回可用内容）")

    def slist(key, cap):
        raw = parsed.get(key)
        out = []
        if isinstance(raw, list):
            for x in raw:
                if isinstance(x, str) and x.strip():
                    out.append(x.strip())
                if len(out) >= cap:
                    break
        return out

    secs = []
    if isinstance(parsed.get("sections"), list):
        for s in parsed["sections"]:
            if isinstance(s, dict) and str(s.get("heading") or "").strip():
                secs.append({
                    "heading": str(s["heading"]).strip(),
                    "body": str(s.get("body") or "").strip(),
                })

    if not secs:
        raise RuntimeError("综述生成失败（没有可用章节）")

    emit(f"综述完成：{len(secs)} 节")
    return {
        "sections": secs,
        "consensus": slist("consensus", 4),
        "gaps": slist("gaps", 3),
        "caveat": str(parsed.get("caveat") or "").strip(),
    }


# ── ⑤ 参考文献格式化 + 报告 ─────────────────────────────────

def gbt_author(name):
    """GB/T 7714：中文作者原样、英文作者"姓全大写 + 名缩写"。

    这里**不做**"从拼音判断哪个是中文姓"的猜测。试过一版按常见姓氏表判断，
    结果把 "Zhang San" 判成 "SAN Z"（"三"的拼音被当成了姓），
    因为姓氏表里的字同时也能拼出英文词。数据源给的作者顺序本来就是对的
    （中文姓名在前、英文姓名在后），照最后一个词当姓即可。

    但有个数据源的坑要处理：OpenAlex 对中文论文给的是**汉字+拼音拼在一起**的
    字符串（`曾军英ZengJunying`），不是两个字段。GB/T 7714 的中文著录只要汉字，
    拼音是多余的，所以中英混排时只保留汉字部分。
    （若只留拼音会得到 `曾军英ZENGJUNYING` 这种四不像。）
    """
    s = str(name or "").strip()
    if not s:
        return ""
    han = "".join(re.findall(r"[\u4e00-\u9fff]+", s))
    if han:
        # 纯汉字（"张三"）原样返回；汉字+拼音（"曾军英ZengJunying"，OpenAlex 的拼法）
        # 只保留汉字部分 —— GB/T 7714 的中文著录要的是汉字，拼音是多余的。
        return re.sub(r"\s+", "", han)
    parts = s.split()
    if len(parts) == 1:
        return parts[0].upper()
    family = parts[-1].upper()
    initials = " ".join(
        " ".join(x[0].upper() for x in w.split("-") if x) for w in parts[:-1])
    return f"{family} {initials}"


def gbt_authors(authors):
    lst = [gbta for gbta in (gbt_author(a) for a in (authors or [])) if gbta]
    if not lst:
        return "佚名"
    if len(lst) <= 3:
        return ", ".join(lst)
    zh = any(re.search(r"[\u4e00-\u9fff]", a) for a in lst)
    return ", ".join(lst[:3]) + ("等" if zh else ", et al")


DOC_MARK = {"journal": "J", "conference": "C", "thesis": "D", "book": "M", "preprint": "EB/OL"}


def format_gbt7714(p, idx=None):
    """GB/T 7714-2015 著录格式（与前端保持同一套规则）。"""
    seg = [gbt_authors(p.get("authors")) + "."]
    seg.append((p.get("title") or "") + "[" + DOC_MARK.get(p.get("docType") or "journal", "J") + "].")
    src = (p.get("journal") or p.get("venue") or "").strip() or "[刊名不详]"
    year = p.get("year")
    if year and year != "未知":
        src += ", " + str(year)
    if p.get("volume"):
        src += ", " + str(p["volume"]) + (f"({p['issue']})" if p.get("issue") else "")
    if p.get("pages"):
        src += ": " + str(p["pages"])
    seg.append(src + ".")
    if p.get("doi"):
        seg.append("DOI: " + p["doi"] + ".")
    text = re.sub(r"\s+", " ", " ".join(seg)).strip()
    return text if idx is None else f"[{idx}] {text}"


def step_format(cfg, papers, plan, emit, synth=None, stats=None):
    """④ 参考文献格式化 + 整合报告。

    注意两点（都是踩过的坑）：
    1. 要把 synth / stats 传进 build_report —— 否则报告里不会有综述正文和统计图表，
       只剩一张文献表和参考文献。
    2. **没有文献也要出报告**。前面的步骤失败时（例如检索被限流），这里再抛错的话
       用户就拿不到任何产物；不如生成一份说明了失败原因的报告，
       至少能看到流程跑到哪、为什么没结果。
    """
    refs = [format_gbt7714(p, i) for i, p in enumerate(papers, 1)] if papers else []
    if refs:
        emit(f"生成 {len(refs)} 条 GB/T 7714 参考文献")
    else:
        emit("没有可用文献，生成一份说明性的报告")
    md = build_report(cfg, plan, papers, refs, synth=synth, stats=stats)
    emit(f"报告已生成（{len(md)} 字）")
    return {"references": refs, "report": md, "chars": len(md)}


def short_authors(authors, limit=2):
    """报告表格里的作者列：常见中文人名的展示形式。

    只取前 limit 位，超过就加"等"。名字里的拼音要去掉 ——
    OpenAlex 给的是 `曾军英ZengJunying`，直接显示又长又难看，
    而参考资料一节里的 GB/T 7714 著录本来就不带拼音，
    两处不一致会让人以为是两个人。
    """
    names = []
    for a in (authors or [])[:limit]:
        han = "".join(re.findall(r"[\u4e00-\u9fff]+", str(a)))
        names.append(han or str(a).strip())
    names = [n for n in names if n]
    if not names:
        return "佚名"
    return "、".join(names) + ("等" if len(authors or []) > limit else "")


def build_report(cfg, plan, papers, refs, synth=None, stats=None):
    """把各步产物拼成一份可直接导出的 Markdown 报告。"""
    L = []
    topic = cfg.get("topic") or cfg.get("query") or "研究主题"
    L.append(f"# {topic}：文献综述")
    L.append("")
    L.append(f"> 由 AI 科研工作流自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if cfg.get("focus"):
        L.append(f"> 重点关注：{cfg['focus']}")
    L.append("")

    L.append("## 一、检索概况")
    L.append("")
    L.append(f"- 检索词：`{cfg.get('query')}`")
    L.append(f"- 来源范围：{cfg.get('source')}")
    L.append(f"- 命中文献：{len(papers)} 篇")
    if stats:
        L.append(f"- 其中开放获取且有全文直链：{stats.get('withFulltext', 0)} 篇")
        L.append(f"- 有 DOI：{stats.get('withDoi', 0)} 篇")
    L.append("")
    L.append("| # | 标题 | 作者 | 年份 | 来源 | 被引 |")
    L.append("|---|------|------|------|------|------|")
    for i, p in enumerate(papers, 1):
        au = short_authors(p.get("authors"))
        L.append("| %d | %s | %s | %s | %s | %s |" % (
            i, (p.get("title") or "").replace("|", "/"), au, p.get("year"),
            p.get("sourceLabel") or p.get("source") or "", p.get("citationCount") or 0))
    L.append("")

    if synth:
        L.append("## 二、综述正文")
        L.append("")
        for s in synth.get("sections") or []:
            L.append(f"### {s['heading']}")
            L.append("")
            L.append(s["body"])
            L.append("")
        if synth.get("consensus"):
            L.append("### 主要共识")
            L.append("")
            for x in synth["consensus"]:
                L.append(f"- {x}")
            L.append("")
        if synth.get("gaps"):
            L.append("### 研究空白")
            L.append("")
            for x in synth["gaps"]:
                L.append(f"- {x}")
            L.append("")
        if synth.get("caveat"):
            L.append(f"> ⚠️ 局限：{synth['caveat']}")
            L.append("")

    if stats:
        L.append("## 三、统计与可视化")
        L.append("")
        for title, key in (("年份分布", "years"), ("被引分布", "citations"),
                           ("文献类型", "docTypes"), ("数据来源", "sources")):
            items = stats.get(key) or []
            if not items:
                continue
            L.append(f"**{title}**")
            L.append("")
            L.append("```")
            for it in items:
                L.append("%-10s %s %d" % (it["label"], it["bar"], it["count"]))
            L.append("```")
            L.append("")

    extract_rows = [(i, p) for i, p in enumerate(papers, 1) if any((p.get("extract") or {}).values())]
    if extract_rows:
        L.append("## 四、信息抽取明细")
        L.append("")
        L.append("| # | 研究对象 | 方法 | 数据 | 指标 | 主要结论 |")
        L.append("|---|----------|------|------|------|----------|")
        for i, p in extract_rows:
            e = p["extract"]
            cell = lambda v: str(v or "").replace("|", "/")
            L.append("| %d | %s | %s | %s | %s | %s |" % (
                i, cell(e.get("object")), cell(e.get("method")), cell(e.get("data")),
                cell(e.get("metric")), cell(e.get("finding"))))
        L.append("")

    L.append("## 五、参考文献（GB/T 7714）")
    L.append("")
    for r in refs:
        L.append(r)
    L.append("")

    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════
#  编排器
# ═══════════════════════════════════════════════════════════════

# 规划结果缓存：同一个研究目标只让模型规划一次。
# 为什么值得缓存：规划要 1.5-2s（实测），而**每次提交都会调用它** ——
# 包括"中断后从断点续跑"和"重复点开始执行"，这些场景下目标字符串一模一样，
# 再调一次模型纯属白等。缓存只按目标文本命中，不跨目标复用。
_PLAN_CACHE = {}
_PLAN_CACHE_MAX = 64


def plan_workflow(goal):
    """自规划：把研究目标变成一份检索研究计划。

    模型不可用时退化成"目标原样当检索词"，保证流程仍能跑。
    """
    goal = (goal or "").strip()
    fallback = {
        "topic": goal, "query": goal, "source": "all", "limit": 20,
        "focus": "", "plan": "按研究目标直接检索并归纳",
        "sections": ["研究背景与问题", "主流方法", "数据集与评价指标", "存在问题与展望"],
    }
    if not goal:
        return fallback

    cached = _PLAN_CACHE.get(goal)
    if cached is not None:
        return dict(cached)

    parsed = _call_json(PLANNER_PROMPT, goal, max_tokens=600)
    if not isinstance(parsed, dict):
        return fallback

    def s(k, d):
        v = str(parsed.get(k) or "").strip()
        return v or d

    try:
        limit = int(parsed.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    secs = [str(x).strip() for x in (parsed.get("sections") or []) if str(x).strip()][:5]

    src = str(parsed.get("source") or "all").strip().lower()
    plan = {
        "topic": s("topic", goal),
        "query": s("query", goal),
        "source": src if src in ("all", "cn", "en") else "all",
        "limit": min(max(5, limit), 30),
        "focus": s("focus", ""),
        "plan": s("plan", "按研究目标检索并归纳"),
        "sections": secs or fallback["sections"],
    }
    # 简单容量控制：满了就丢最早的一条（不值得为它引 OrderedDict）
    if len(_PLAN_CACHE) >= _PLAN_CACHE_MAX:
        _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
    _PLAN_CACHE[goal] = plan
    return dict(plan)


def clear_plan_cache():
    """清空规划缓存（测试用，避免用例之间互相影响）。"""
    _PLAN_CACHE.clear()


class WorkflowRunner:
    """按步骤执行工作流。每一步开始/结束都通过 emit 回调报出去，
    调用方（app.py）负责落库与转成 SSE；这样编排逻辑可以脱离 Flask 单测。
    """

    def __init__(self, plan, on_step=None, should_stop=None):
        self.plan = plan
        self.on_step = on_step or (lambda *a, **k: None)
        self.should_stop = should_stop or (lambda: False)
        self.ctx = {}          # 步骤之间传递的产物
        self.state = {}        # key -> {status, detail, error, output}

    def run(self, start_from="search", skip=None, previous=None, previous_state=None):
        """从 start_from 开始跑。

        previous       之前跑出来的上下文（papers/stats/synth），续跑时直接复用
        previous_state 之前各步骤的状态，续跑时铺回去，
                       否则快照里"上一轮已经跑完的步骤"会退回 pending，
                       前端看到的时间线会突然少掉半截。
        """
        skip = set(skip or [])
        self.ctx.update(previous or {})
        for k, v in (previous_state or {}).items():
            if isinstance(v, dict) and v.get("status") not in (None, "pending"):
                self.state[k] = dict(v)
        start_idx = STEP_KEYS.index(start_from) if start_from in STEP_KEYS else 0

        for idx, step in enumerate(STEPS):
            key = step["key"]
            if idx < start_idx:
                continue
            if key in skip:
                self.state[key] = {"status": "skipped", "detail": "已跳过"}
                self.on_step(key, "skipped", "已跳过")
                continue
            if self.should_stop():
                # 中断语义（全流程只认一个断点）：
                #   * 已经跑完的步骤保持 done —— 成果不丢；
                #   * 断点就是"本来要开始、但被拦下"的这一步。
                # 续跑时 start_from 取的就是这个 key。
                self.state[key] = {"status": "interrupted", "detail": "用户中断，从此处继续"}
                self.on_step(key, "interrupted", "用户中断，从此处继续")
                return self.state

            self.on_step(key, "running", "开始")
            broke_here = False

            def emit(detail, _key=key):
                # emit 只负责"汇报进度"，不负责判断中断 ——
                # 早期版本在这里顺手查 should_stop 并报 interrupted，
                # 结果同一次中断被报了两次（步骤自己一次、循环顶部一次），
                # 库里出现两个 interrupted 步骤，续跑起点也跟着错。
                self.state.setdefault(_key, {})["detail"] = detail
                self.on_step(_key, "running", detail)

            try:
                if key == "search":
                    out = step_search(self.plan, emit)
                    self.ctx["papers"] = out["papers"]
                    self.ctx["search"] = out
                elif key == "extract":
                    out = step_extract(self.plan, self.ctx.get("papers") or [], emit,
                                       stop=self.should_stop)
                    self.ctx["extract"] = out
                elif key == "synthesize":
                    out = step_synthesize(self.plan, self.ctx.get("papers") or [], emit,
                                          sections=self.plan.get("sections"),
                                          stop=self.should_stop)
                    self.ctx["synth"] = out
                elif key == "analyze":
                    out = step_analyze(self.plan, self.ctx.get("papers") or [], emit)
                    self.ctx["stats"] = out
                elif key == "format":
                    out = step_format(self.plan, self.ctx.get("papers") or [], self.plan, emit,
                                      synth=self.ctx.get("synth"), stats=self.ctx.get("stats"))
                    self.ctx["format"] = out
                else:
                    continue
            except Exception as e:  # noqa: BLE001 - 单步失败不拖垮整条流水线
                self.state[key] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                self.on_step(key, "failed", str(e))
            else:
                # 步骤自己在批次边界发现中断时，会在产物里带 interrupted=True。
                # 这时断点就是这一步本身（它确实开始了但没跑完），
                # 直接返回，不要再多标一个"下一步" —— 否则库里会出现两个断点。
                if isinstance(out, dict) and out.get("interrupted"):
                    broke_here = True
                    self.state[key] = {"status": "interrupted", "detail": "用户中断，从此处继续"}
                    self.on_step(key, "interrupted", "用户中断，从此处继续")
                else:
                    detail = self.state.get(key, {}).get("detail", "")
                    self.state[key] = {"status": "done", "detail": detail, "output": out}
                    # 一定要报 done：外部（app.py/前端/数据库）只认 on_step 的回调，
                    # 不报的话这一步在库里永远停在 running，续跑也会从错误的位置开始。
                    self.on_step(key, "done", detail)

            if broke_here:
                return self.state

        return self.state

    def snapshot(self):
        """给前端/数据库用的可序列化状态。papers 只保留必要的展示字段，避免过大。"""
        out = {
            "plan": self.plan,
            "steps": [],
        }
        papers = self.ctx.get("papers") or []
        for step in STEPS:
            key = step["key"]
            st = self.state.get(key) or {"status": "pending"}
            item = {
                "key": key, "title": step["title"], "core": step["core"],
                "detail_text": step["detail"],
                "status": st.get("status", "pending"),
            }
            if st.get("detail"):
                item["progress"] = st["detail"][:200]
            if st.get("error"):
                item["error"] = st["error"][:300]
            out["steps"].append(item)

        out["paperCount"] = len(papers)
        out["papers"] = [{
            "id": p.get("id"), "title": p.get("title"), "authors": (p.get("authors") or [])[:6],
            "year": p.get("year"), "venue": p.get("journal") or p.get("venue"),
            "sourceLabel": p.get("sourceLabel"), "citationCount": p.get("citationCount"),
            "doi": p.get("doi"), "url": p.get("url"), "openAccessPdf": p.get("openAccessPdf"),
            "relevance": p.get("relevance"), "extract": p.get("extract") or {},
        } for p in papers]
        if self.ctx.get("stats"):
            out["stats"] = self.ctx["stats"]
        if self.ctx.get("synth"):
            out["synth"] = self.ctx["synth"]
        if self.ctx.get("format"):
            out["references"] = self.ctx["format"]["references"]
            out["report"] = self.ctx["format"]["report"]
        return out
