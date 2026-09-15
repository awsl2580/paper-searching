"""向后兼容层 —— 真正的实现已经搬到 providers/semantic_scholar.py。

保留这个模块名只是为了不让历史 import（或别人的脚本）直接报错。
新代码请直接 `import providers`，它统一管理多个数据源。

原文件顶部那段"为什么需要节流/退避/缓存"的实测结论，已完整搬到
providers/semantic_scholar.py 的模块 docstring 里。
"""

from __future__ import annotations

import json

from providers import clear_cache  # noqa: F401  (旧代码会调 s2_client.clear_cache())
from providers.base import ProviderError
from providers.semantic_scholar import (  # noqa: F401
    S2_API_KEY,
    client,
    normalize_s2_paper as normalize_paper,
    rate_limit_message,
)
from providers.semantic_scholar import provider as _provider


def rate_limit_payload():
    """给 Agent/前端用的限流结果（JSON 字符串）。"""
    return json.dumps({
        "error": "rate_limited",
        "message": rate_limit_message(),
        "retryable": True,
        "suggestion": "请等待约 10 秒后重试。相同检索在缓存有效期内会直接命中本地缓存，不会再打接口。",
    }, ensure_ascii=False)


def search_papers(query, limit=5):
    """检索论文，返回 JSON 字符串（保持与旧版一致的返回契约）。"""
    if not query or not query.strip():
        return json.dumps({"error": "请提供搜索关键词"}, ensure_ascii=False)

    try:
        limit = min(max(1, int(limit)), 10)
    except (TypeError, ValueError):
        limit = 5

    try:
        papers = _provider.search(query.strip(), limit)
    except ProviderError as e:
        if e.kind == "rate_limited":
            return rate_limit_payload()
        return json.dumps({"error": e.message, "retryable": e.retryable}, ensure_ascii=False)

    if not papers:
        return json.dumps({
            "message": f"未找到与「{query}」相关的论文，请尝试其他关键词（建议使用英文）",
            "papers": [],
        }, ensure_ascii=False)

    for p in papers:
        p["abstract"] = (p.get("abstract") or "暂无摘要")[:300]
    return json.dumps({"papers": papers, "total": len(papers)}, ensure_ascii=False)


def get_paper_by_id(paper_id):
    """按 paperId 取详情（这个端点基本不被限流）。"""
    try:
        paper = _provider.detail(paper_id)
    except ProviderError as e:
        if e.kind == "rate_limited":
            return rate_limit_payload()
        return json.dumps({"error": e.message, "retryable": e.retryable}, ensure_ascii=False)

    if not paper:
        return json.dumps({"error": "未找到该论文，请检查ID是否正确"}, ensure_ascii=False)
    return json.dumps(paper, ensure_ascii=False)


def get_paper_by_title(title):
    """按标题取详情（走 /paper/search/match）。"""
    try:
        paper = _provider.detail_by_title(title)
    except ProviderError as e:
        if e.kind == "rate_limited":
            return rate_limit_payload()
        return json.dumps({"error": e.message, "retryable": e.retryable}, ensure_ascii=False)

    if not paper:
        return json.dumps({"message": f"未找到标题匹配「{title}」的论文", "paper": None},
                          ensure_ascii=False)
    return json.dumps(paper, ensure_ascii=False)
