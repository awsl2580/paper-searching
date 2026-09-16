"""多源文献检索聚合层 —— 工具层和前端只跟这里打交道。

对外三件事：
    search_papers(query, limit, source)   多源并发检索 + 跨源去重，返回 JSON
    get_detail(paper_id)                  按全局 id / 旧格式 id / 标题取详情
    by_title(title)                       按标题找一篇

source 取值：
    "all"  中文 + 英文（默认）
    "en"   仅英文
    "cn"   仅中文（OpenAlex 的 filter=language:zh）

关于中文覆盖度：OpenAlex 对中文核心期刊的覆盖不如知网/万方，而且不少中文
文献没有摘要或 DOI。这是数据源本身的限制，不是代码问题；如果以后接入了
有授权的中文源（例如万方开放平台），只要在 SOURCES 里加一行、在 GROUPS 的
"cn" 分组里排到前面即可，业务代码不用动。
"""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from providers import openalex as _openalex
from providers import semantic_scholar as _s2
from providers.base import ProviderError, dedup_key, split_id

# 注册表：新增数据源只需要在这里加一行
SOURCES = {
    _s2.provider.name: _s2.provider,
    _openalex.provider.name: _openalex.provider,
}

# 每个语种分组下，按优先级排列的 (provider 名, language 过滤)
GROUPS = {
    "en": [("semantic_scholar", None), ("openalex", None)],
    "cn": [("openalex", "zh")],
}

# 检索结果里的摘要截断长度：给模型看的，太长纯浪费 token
SEARCH_ABSTRACT_LIMIT = 300


# ═══════════════════════════════════════════════════════════════
#  排序：高被引 / 新近的排前面
# ═══════════════════════════════════════════════════════════════
#
# 系数怎么定的（都可以用下面的常量直接调）：
#
#   被引次数用 log 压缩。它的分布是重尾的：中文库里常见 0-10 次，
#   而经典论文能到几万次。用原始值归一化的话，一篇 100000 次被引会把
#   其它所有论文压成 0 分，排序完全由它一个人决定。
#   log1p 之后 10 次 -> 0.44、100 次 -> 0.74、1000 次 -> 0.90，
#   既保留了量级差异，又不会让个别高被引论文吃掉整个列表。
#
#   年份用"半衰期"衰减而不是线性。线性到 20 年就把老论文判 0 分，
#   但学术价值不会在某个年份突然归零；半衰期 6 年意味着
#   每老 6 年，新近性得分减半（6 年前 -> 0.5、12 年前 -> 0.25）。
#   今年和未来的（在线优先出版）都给满分。
#
#   0.65 / 0.35 的配比：偏向被引，因为它是学术影响力的直接证据；
#   但年份占三分之一强，这样 0 被引的新论文（中文库里非常多）
#   仍能靠新近性排到同相关度、同样 0 被引的老论文前面。
CITATIONS_WEIGHT = 0.65
RECENCY_WEIGHT = 0.35
RECENCY_HALF_LIFE = 6.0      # 年


def _citation_score(count):
    """被引次数 -> 0~1，log 压缩，避免个别高被引论文压垮整个列表。"""
    try:
        n = max(0, int(count or 0))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(10000))


def _recency_score(year, now=None):
    """年份 -> 0~1，按半衰期衰减。年份缺失或不是数字时给 0（不猜）。"""
    try:
        y = int(str(year).strip()[:4])
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if y <= 0:
        return 0.0
    now = now or date.today().year
    age = now - y
    if age <= 0:
        return 1.0
    return 0.5 ** (age / RECENCY_HALF_LIFE)


def impact_score(paper, now=None):
    """(综合分 0~1, 被引分, 新近分)"""
    c = _citation_score(paper.get("citationCount"))
    r = _recency_score(paper.get("year"), now)
    return round(CITATIONS_WEIGHT * c + RECENCY_WEIGHT * r, 4), round(c, 4), round(r, 4)


def _annotate_and_sort(papers, now=None):
    """给每篇打上排序分，然后按 相关度 -> 综合分 排序（稳定，并列保持原顺序）。

    为什么相关度优先：如果只看被引和年份，一篇高被引但跑题的论文会排到最前，
    用户反而更难用。相关度是门槛，影响力是门槛内的次序。
    """
    for p in papers:
        total, c, r = impact_score(p, now)
        p["impactScore"] = total
        p["citationScore"] = c
        p["recencyScore"] = r
    return sorted(
        papers,
        key=lambda p: (-(p.get("relevance") if isinstance(p.get("relevance"), (int, float)) else 0),
                       -p["impactScore"]),
    )


def _has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _plan_for(source, query=""):
    """决定这一轮要跑哪些源。

    中文查询下**不调用 Semantic Scholar**，这是实测结论不是偷懒：
      * S2 对中文只做字面匹配。实测查"经济"返回的是
        "童亭矿…煤层经济开采…"（采煤）和"建筑工程项目管理…施工现场管理…"（施工），
        只因为标题里恰好有"经济"两个字，与经济学毫无关系；
      * S2 公共配额经常 429，本来就时好时坏；
      * 实测同一个中文查询，OpenAlex 加不加 language:zh 过滤结果一致，
        所以去掉 S2 不损失任何中文覆盖。
    英文查询下 S2 才是强项，照旧参与。
    """
    source = (source or "all").lower()
    if source in ("cn", "zh", "chinese", "中文"):
        return "cn", list(GROUPS["cn"])
    if source in ("en", "english", "英文"):
        return "en", list(GROUPS["en"])

    if _has_cjk(query):
        # 中文查询："全部"里真正能贡献结果的只有 OpenAlex
        return "all", [("openalex", "zh")]
    # all：英文源各一路 + 中文源各一路（openalex 走 zh 过滤，不再重复跑一遍全库）
    return "all", [("semantic_scholar", None), ("openalex", "zh")]


def source_status():
    """给前端 /api/sources 用的数据源状态。"""
    out = []
    for name, p in SOURCES.items():
        out.append({
            "name": name,
            "label": p.label,
            "languages": list(p.languages),
            "available": bool(p.available()),
            "reason": p.unavailable_reason(),
        })
    return out


def clear_cache():
    """清空所有源的结果缓存。"""
    for module in (_s2, _openalex):
        module.client.clear_cache()


# ═══════════════════════════════════════════════════════════════
#  检索
# ═══════════════════════════════════════════════════════════════

def _search_one(name, language, query, limit, offset=0):
    """跑一个源。返回 (name, papers, error)；error 为 None 表示成功。"""
    provider = SOURCES[name]
    try:
        if not provider.available():
            raise ProviderError(provider.unavailable_reason(), kind="unavailable", source=name)
        return name, provider.search(query, limit, language=language, offset=offset), None
    except ProviderError as e:
        return name, [], e
    except Exception as e:  # noqa: BLE001 - 单源崩溃不该拖垮整轮检索
        return name, [], ProviderError(f"{type(e).__name__}: {e}", kind="exception", source=name)


def _merge(results, limit):
    """按来源轮流取，保证中英文都有机会进最终结果；同一篇论文只留一份。

    去重靠 DOI + 规范化标题（见 base.dedup_key）——中文文献经常没有 DOI，
    所以标题去重是必需的，否则中英文库里的同一篇会重复出现。
    """
    queues = [list(papers) for papers in results if papers]
    merged = []
    seen = set()
    while queues and len(merged) < limit:
        for q in list(queues):
            if not q:
                queues.remove(q)
                continue
            paper = q.pop(0)
            key = dedup_key(paper)
            if key in seen:
                continue
            seen.add(key)
            merged.append(paper)
            if len(merged) >= limit:
                break
    return merged


def _warning_for(error):
    """把 provider 错误翻译成一句给人看的话。"""
    label = SOURCES[error.source].label if error.source in SOURCES else (error.source or "数据源")
    if error.kind == "unavailable":
        return f"{label} 未启用（{error.message}）"
    if error.kind == "rate_limited":
        return f"{label} 被限流"
    if error.kind in ("timeout", "connection"):
        return f"{label} 网络异常"
    return f"{label} 检索失败"


# ═══════════════════════════════════════════════════════════════
#  相关度打分（"精读文献表"的筛选依据）
# ═══════════════════════════════════════════════════════════════
#
# 为什么是"查询词覆盖"而不是语义相似度：
# 这个分数要能向用户解释"为什么这篇被剔除"。语义分数看着高级，
# 但评委/用户问一句"为什么这篇 62 分"就答不上来了。覆盖率的依据是
# 可以逐词展示的（哪些词命中、哪些没命中），而且不需要额外模型调用。

def _query_terms(query):
    """把查询切成可匹配的词。

    中文没有空格，按 2-gram 切（"乡村振兴" -> 乡村/村振/振兴）。
    只取单字会把分数稀释得没法看，只取整串又太严，2-gram 是折中。
    """
    terms = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{1,}", query or ""):
        terms.append(w.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", query or ""):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(terms))


def score_relevance(paper, terms):
    """返回 (0-100 的分数, 明细)。

    计分规则（刻意保持简单可解释）：标题命中算满权，只在摘要/主题词里
    命中算半权。全部查询词都在标题里 = 100 分。
    """
    if not terms:
        return 100, {"title_hits": [], "abstract_hits": [], "missing": []}

    title = (paper.get("title") or "").lower()
    body = ((paper.get("abstract") or "") + " " +
            " ".join(paper.get("fieldsOfStudy") or [])).lower()

    title_hits = [t for t in terms if t in title]
    body_hits = [t for t in terms if t in body and t not in title_hits]
    covered = set(title_hits) | set(body_hits)

    score = round(100 * (2 * len(title_hits) + len(body_hits)) / (2 * len(terms)))
    return score, {
        "title_hits": title_hits,
        "abstract_hits": body_hits,
        "missing": [t for t in terms if t not in covered],
    }


def _apply_relevance(papers, query, min_relevance=0):
    """给结果打相关度分，并按需过滤低相关度内容。

    自动过滤只在"确实还有结果剩下"时才执行：如果一条都没命中查询词，
    说明多半是分词或跨语言问题（比如中文查询命中了英文文献），
    这时候全删掉只会让用户空手而归，不如原样给出。
    """
    terms = _query_terms(query)
    for p in papers:
        score, detail = score_relevance(p, terms)
        p["relevance"] = score
        p["relevanceDetail"] = detail

    threshold = max(0, int(min_relevance or 0))
    if threshold > 0:
        kept = [p for p in papers if p["relevance"] >= threshold]
        if kept:
            return kept, len(papers) - len(kept), threshold

    # 默认行为：剔除"标题和摘要里一个查询词都没命中"的，但必须留下东西
    kept = [p for p in papers if p["relevance"] > 0]
    if kept and len(kept) < len(papers):
        return kept, len(papers) - len(kept), 1
    return papers, 0, 0


def _merge_paged(plan, query, need, offset, min_relevance=0):
    """按来源轮流取，合并去重，返回 [offset, offset+need) 这一段。

    为什么要"按需多批"而不是一次取满：翻到第 3 页时若一次性向每个源要 100 条，
    S2 那边就是 10 个请求（它单页上限 10 条），光是节流等待就要 12 秒以上。
    这里改成按 chunk 递增着取，够用就停 —— 第 1 页通常一批就够，
    且每个 (query, limit, offset) 组合各自有缓存，往回翻页基本零成本。

    顺序很关键：**先打分、再过滤、然后排序、最后切片**。
    一开始把排序放在了最前面，那时每篇的 relevance 还都是 0，排序等于没生效
    （实测输出是"相关度 75 / 100 / 75 / 100"交错的，一眼就能看出没排上）。
    """
    chunk = max(need, 10)
    merged, seen = [], set()
    errors = []
    by_source = {}

    while len(merged) < offset + need and chunk <= 200:
        by_source = {}
        fresh = []
        with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
            futures = [pool.submit(_search_one, name, lang, query, chunk, 0)
                       for name, lang in plan]
            for fut in futures:
                name, papers, error = fut.result()
                if error:
                    fresh.append(error)
                if papers:
                    by_source[name] = papers

        # 保序：按 plan 顺序取，避免并发完成顺序影响输出顺序
        ordered = [by_source[name] for name, _lang in plan if name in by_source]
        merged, seen = [], set()
        queues = [list(p) for p in ordered if p]
        while queues and len(merged) < offset + need:
            for q in list(queues):
                if not q:
                    queues.remove(q)
                    continue
                paper = q.pop(0)
                key = dedup_key(paper)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(paper)
                if len(merged) >= offset + need:
                    break

        errors = fresh
        # 各源都没凑满一个 chunk，说明已经到底了，再多取也没用
        if all(len(v) < chunk for v in by_source.values()):
            break
        chunk *= 2

    # 排序放在切片之前：这样第 N 页就是「全局排序后的第 N 段」，
    # 各页之间不会互相重复或错位。
    merged, filtered_out, threshold = _apply_relevance(merged, query, min_relevance)
    merged = _annotate_and_sort(merged)
    return merged[offset:offset + need], merged, errors, filtered_out, threshold


def search_papers(query, limit=5, source="all", min_relevance=0, offset=0):
    """多源检索，返回给 LLM 工具直接用的 JSON 字符串。

    limit:  本次要返回的条数（1-50，翻页时是"每页条数"）
    offset: 跳过前多少条（翻页用）
    min_relevance: 相关度下限（0-100）。0 表示用默认策略（只剔除查询词
    一个都没命中的）。用户明确要求"筛出高相关的""严格一点"时可以给 40/60。

    翻页说明：取回的是合并去重后 [offset, offset+limit) 这一段。
    注意相关度过滤是在**取回窗口内**做的 —— 这是为了让"第 N 页"每次翻回来
    都是同一批结果（若先全局打分再切片，各页会互相错位）。
    """
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "请提供搜索关键词"}, ensure_ascii=False)

    try:
        limit = min(max(1, int(limit)), 50)
    except (TypeError, ValueError):
        limit = 5
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        min_relevance = min(max(0, int(min_relevance or 0)), 100)
    except (TypeError, ValueError):
        min_relevance = 0

    source_key, plan = _plan_for(source, query)

    papers, window, errors, filtered_out, threshold = _merge_paged(
        plan, query, limit, offset, min_relevance)

    # "未配置/未启用"是部署状态，不是这次检索出了故障：源本来就没参与本次检索，
    # 没必要每轮都向用户汇报一遍。只把"本该能用却失败"的情况作为 warning。
    warnings = [_warning_for(e) for e in errors if e.kind != "unavailable"]
    all_notes = [_warning_for(e) for e in errors]

    if not papers and offset == 0:
        # 全是限流/超时这类可重试错误，就告诉用户能怎么办
        if any(e.kind == "rate_limited" for e in errors):
            return json.dumps({
                "error": "rate_limited",
                "message": "数据源本次没能返回结果：" + "；".join(all_notes),
                "retryable": True,
                "suggestion": "请等待约 10 秒后重试。相同检索在缓存有效期内会直接命中本地缓存，不会再打接口。",
            }, ensure_ascii=False)
        if errors and all(e.kind == "unavailable" for e in errors):
            return json.dumps({
                "error": "no_source",
                "message": "当前没有可用的文献数据源：" + "；".join(all_notes),
                "retryable": False,
            }, ensure_ascii=False)
        return json.dumps({
            "error": "search_failed",
            "message": "；".join(all_notes) or f"未找到与「{query}」相关的论文",
            "papers": [],
            "retryable": True,
        }, ensure_ascii=False)

    # 相关度打分 + 低相关度过滤（"精读文献表"的筛选依据）
    # 打分与过滤已在 _merge_paged 里做完（必须在排序之前）

    for p in papers:
        p["abstract"] = (p.get("abstract") or "暂无摘要")[:SEARCH_ABSTRACT_LIMIT]

    used = sorted({p["sourceLabel"] for p in papers})
    payload = {
        "papers": papers,
        "total": len(papers),
        "sources": used,
        "source_scope": source_key,
        # 让模型知道"结果已经过了相关性过滤"，以及过滤掉了几篇
        "relevance_threshold": threshold,
        "filtered_out": filtered_out,
    }
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  详情
# ═══════════════════════════════════════════════════════════════

def _detail_error(e):
    return json.dumps({
        "error": e.kind,
        "message": e.message,
        "retryable": e.retryable,
    }, ensure_ascii=False)


def get_detail(paper_id):
    """按 id 取详情。

    能处理：带前缀的全局 id（semantic_scholar: / openalex:）、旧的 40 位 S2 id、
    旧的 title_xxx 假 id、以及用户直接丢过来的一个标题。
    """
    paper_id = (paper_id or "").strip()
    if not paper_id:
        return json.dumps({"error": "请提供论文ID或标题"}, ensure_ascii=False)

    source, raw = split_id(paper_id)

    # 认得出来源就直连对应的详情接口
    if source in SOURCES and raw:
        try:
            paper = SOURCES[source].detail(raw)
            if paper:
                return json.dumps(paper, ensure_ascii=False)
        except ProviderError as e:
            # 该源挂了不代表没救：继续按标题去别的源找
            fallback = by_title(raw)
            if fallback:
                return fallback
            return _detail_error(e)

    # 认不出来源：当成标题/关键词，多源找一遍
    return by_title(raw or paper_id)


def by_title(title):
    """按标题找一篇：中文标题先问 OpenAlex，英文标题先问 Semantic Scholar。"""
    title = (title or "").strip()
    if not title:
        return json.dumps({"error": "请提供论文标题"}, ensure_ascii=False)

    errors = []
    # 中文标题先问 OpenAlex（它能按中文检索），英文标题先问 Semantic Scholar
    # （它有 /paper/search/match 精确匹配端点）
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in title)
    order = ("openalex", "semantic_scholar") if has_cjk else ("semantic_scholar", "openalex")

    for name in order:
        provider = SOURCES[name]
        if not provider.available():
            continue
        try:
            # 只有 S2 有 /paper/search/match 这种精确匹配端点；
            # OpenAlex 退化成"检索第一条当命中"
            if provider.supports_title_match:
                paper = provider.detail_by_title(title)
            else:
                papers = provider.search(title, 1, language=None)
                paper = papers[0] if papers else None
            if paper:
                return json.dumps(paper, ensure_ascii=False)
        except ProviderError as e:
            errors.append(e)
        except Exception as e:  # noqa: BLE001
            errors.append(ProviderError(f"{type(e).__name__}: {e}", source=name))

    if errors:
        return _detail_error(errors[0])
    return json.dumps({"message": f"未找到标题匹配「{title}」的论文", "paper": None}, ensure_ascii=False)
