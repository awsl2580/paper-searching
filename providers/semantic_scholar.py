"""Semantic Scholar provider（原 s2_client.py 的实现，迁到统一接口下）。

为什么这里有一堆"多余"的重试/兜底逻辑 —— 都是实测踩出来的，别删：

1. 不带 API Key 时，所有匿名调用者共用同一把公共 key，你能分到多少完全取决于
   别人用了多少 —— 高峰期 /paper/search 几乎必然 429。
   带 API Key 则是自己的独立配额（默认 1 QPS），从"抢公共池"变成"保底可用"。
2. /paper/search 是最容易被限流的端点；/paper/{id} 基本不限流；
   /paper/search/bulk 的限流额度与 /paper/search 相互独立，可以互相兜底。
3. 被限流时不一定返回 429，也可能是 500 —— 两者都要当"被限流"处理并退避。
4. 未做节流的客户端会在几百毫秒内把所有重试打完，然后每秒重复一次，
   这正是"重试了但永远失败"的原因：问题不在于重试次数，而在于请求间隔。

环境变量：
    SEMANTIC_SCHOLAR_API_KEY  可选，设置了就走独立配额
    S2_MIN_INTERVAL           两次请求最小间隔秒数（默认 1.2）
    S2_MAX_RETRIES            单次请求最多尝试次数（默认 4）
    S2_BACKOFF_CAP            退避上限秒数（默认 20）
    S2_CACHE_TTL              缓存有效期秒数（默认 1800）
    S2_BULK_FALLBACK          1/0，是否允许用 bulk 端点兜底（默认 1）
    API_TIMEOUT               单次请求超时秒数（默认 30）
"""

from __future__ import annotations

import os

from providers.base import Provider, ProviderError, normalize_paper
from providers.http import DEFAULT_UA, HttpClient

S2_API = "https://api.semanticscholar.org/graph/v1"

S2_API_KEY = (os.getenv("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
S2_MIN_INTERVAL = float(os.getenv("S2_MIN_INTERVAL", "1.2"))
S2_MAX_RETRIES = int(os.getenv("S2_MAX_RETRIES", "4"))
S2_BACKOFF_CAP = float(os.getenv("S2_BACKOFF_CAP", "20"))
S2_CACHE_TTL = int(os.getenv("S2_CACHE_TTL", "1800"))
S2_BULK_FALLBACK = os.getenv("S2_BULK_FALLBACK", "1") != "0"
TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
USER_AGENT = os.getenv("S2_USER_AGENT", DEFAULT_UA)

# externalIds 用于拿 DOI（跨源去重主要靠它）；
# journal 用于拿卷期页码（GB/T 7714 引用格式需要）
SEARCH_FIELDS = ("paperId,title,authors,year,abstract,url,venue,citationCount,"
                 "fieldsOfStudy,publicationTypes,externalIds,journal")
DETAIL_FIELDS = SEARCH_FIELDS + ",referenceCount,tldr,openAccessPdf,journal"

_headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}

client = HttpClient(
    "semantic_scholar",
    S2_API,
    min_interval=S2_MIN_INTERVAL,
    max_retries=S2_MAX_RETRIES,
    backoff=2.0,
    backoff_cap=S2_BACKOFF_CAP,
    cache_ttl=S2_CACHE_TTL,
    timeout=TIMEOUT,
    user_agent=USER_AGENT,
    default_headers=_headers,
    proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
)


def rate_limit_message():
    """被限流时对用户说的话：讲清原因和真正管用的解决办法。"""
    if S2_API_KEY:
        hint = "已配置 API Key，但此刻仍被限流，等几秒再试即可。"
    else:
        hint = ("当前未配置 Semantic Scholar API Key，请求走的是所有人共用的公共通道，"
                "高峰期很容易被限流。在 .env 里设置 SEMANTIC_SCHOLAR_API_KEY 可获得独立配额。")
    return f"Semantic Scholar 正在限流（429/5xx），本次没能取回结果。{hint}"


def _s2_doc_type(publication_types):
    """S2 的 publicationTypes -> 统一结构里的 docType（决定引用里的 [J]/[C]/[D]）"""
    types = [str(t).lower() for t in (publication_types or [])]
    joined = " ".join(types)
    if "conference" in joined or "proceedings" in joined:
        return "conference"
    if "thesis" in joined or "dissertation" in joined:
        return "thesis"
    if "book" in joined:
        return "book"
    return "journal"


def normalize_s2_paper(p):
    """把接口返回的一篇论文规格化成统一结构。"""
    tldr = p.get("tldr")
    journal = p.get("journal") or {}
    return normalize_paper(
        {
            "paperId": p.get("paperId", ""),
            "title": p.get("title", "未知标题"),
            "authors": [a.get("name", "") for a in (p.get("authors") or [])],
            "year": p.get("year", "未知"),
            "abstract": p.get("abstract") or "暂无摘要",
            "tldr": tldr.get("text", "") if tldr else "",
            "url": p.get("url", ""),
            "venue": p.get("venue", "") or journal.get("name", "") or "",
            "journal": journal.get("name", ""),
            "citationCount": p.get("citationCount", 0),
            "referenceCount": p.get("referenceCount"),
            "fieldsOfStudy": p.get("fieldsOfStudy") or [],
            "openAccessPdf": (p.get("openAccessPdf") or {}).get("url", ""),
            "doi": (p.get("externalIds") or {}).get("DOI", ""),
            "volume": journal.get("volume") or "",
            "issue": "",
            "pages": journal.get("pages") or "",
            "docType": _s2_doc_type(p.get("publicationTypes")),
        },
        source="semantic_scholar",
        source_label="Semantic Scholar",
        # 不写死 "en"：S2 也收录中文期刊，交给 normalize_paper 里的
        # guess_language 按标题判断，避免把中文论文标成英文
        language="",
    )


def _bulk_search(query, limit, fields=SEARCH_FIELDS):
    """兜底方案：/paper/search/bulk 与 /paper/search 限流额度独立，一次能取更多结果。
    代价是排序不按相关性，所以只在前者失败时使用。"""
    status, data = client.get("/paper/search/bulk", params={"query": query, "fields": fields})
    if status != 200 or not isinstance(data, dict):
        return None
    return [normalize_s2_paper(p) for p in (data.get("data") or [])[:limit]]


class SemanticScholarProvider(Provider):
    name = "semantic_scholar"
    label = "Semantic Scholar"
    languages = ("en",)
    supports_title_match = True

    def search(self, query, limit=5, language=None, offset=0):
        """检索。offset 用于翻页。

        /paper/search 单次 limit 上限 10、offset+limit 上限 1000，所以这里
        自己按 10 条一页拼够需要的条数（每页各有自己的缓存，翻页时基本都命中）。
        """
        query = (query or "").strip()
        if not query:
            raise ProviderError("请提供搜索关键词", kind="http_error", source=self.name)

        limit = min(max(1, int(limit or 5)), 100)
        offset = max(0, int(offset or 0))

        S2_PAGE = 10
        S2_MAX_OFFSET = 1000
        out, skipped = [], offset
        while len(out) < limit:
            if skipped >= S2_MAX_OFFSET:
                break                      # 这个端点翻不了更远，如实截断
            want = min(S2_PAGE, limit - len(out), S2_MAX_OFFSET - skipped)
            page = self._search_page(query, want, skipped)
            if page is None:               # 被限流
                if out:
                    break                  # 已经有一部分了，就先用着
                raise ProviderError(
                    rate_limit_message(), kind="rate_limited", retryable=True, source=self.name,
                )
            if not page:
                break                      # 没有更多
            out.extend(page)
            skipped += len(page)
            if len(page) < want:
                break                      # 服务端已到底
        return out

    def _search_page(self, query, limit, offset):
        """取一页（带缓存）。返回 list；被限流返回 None。"""
        cache_key = ("search", query.lower(), limit, offset)
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        status, data = client.get(
            "/paper/search",
            params={"query": query, "limit": limit, "offset": offset, "fields": SEARCH_FIELDS},
        )

        papers = None
        throttled = False
        if status == 200 and isinstance(data, dict):
            papers = [normalize_s2_paper(p) for p in (data.get("data") or [])]
        elif status is None:
            if (data or {}).get("error") == "rate_limited":
                throttled = True
            else:
                raise ProviderError(
                    (data or {}).get("message", "检索失败"),
                    kind=(data or {}).get("error", "exception"),
                    retryable=True,
                    source=self.name,
                )
        else:
            raise ProviderError(
                f"检索接口返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        if throttled:
            # 相关性检索被限流时用 bulk 端点兜底（两者限流额度独立）。
            # bulk 不支持 offset，所以只有第一页能兜底；翻后续页时返回 None。
            if S2_BULK_FALLBACK and offset == 0:
                bulk = _bulk_search(query, limit)
                if bulk:
                    client.cache_set(cache_key, bulk)
                    return bulk
            return None

        papers = papers or []
        client.cache_set(cache_key, papers)
        return papers

    def detail(self, raw_id):
        cache_key = ("paper", raw_id)
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        # /paper/{id} 是公认最稳的端点，但公共池拥挤时一样会 429，
        # 所以这里多给几次机会（每次尝试本身已被全局节流拉开间隔）
        status, data = client.get(
            f"/paper/{raw_id}",
            params={"fields": DETAIL_FIELDS},
            timeout=max(TIMEOUT, 60),
            retries=max(S2_MAX_RETRIES, 4),
        )

        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            if status is None and (data or {}).get("error") == "rate_limited":
                raise ProviderError(rate_limit_message(), kind="rate_limited",
                                    retryable=True, source=self.name)
            raise ProviderError(
                (data or {}).get("message", f"获取论文详情失败（状态 {status}）"),
                kind=(data or {}).get("error") or "http_error",
                retryable=True, source=self.name, status=status,
            )

        paper = normalize_s2_paper(data)
        client.cache_set(cache_key, paper)
        return paper

    def detail_by_title(self, title):
        """按标题取详情：用 /paper/search/match（专用端点，只打一个请求），
        被限流再退回普通检索。"""
        cache_key = ("match", title.lower())
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        status, data = client.get("/paper/search/match",
                                  params={"query": title, "fields": DETAIL_FIELDS})
        paper = None

        if status == 200 and isinstance(data, dict) and data.get("data"):
            paper = data["data"][0]
        elif status == 404:
            return None
        elif status is None:
            if (data or {}).get("error") != "rate_limited":
                raise ProviderError(
                    (data or {}).get("message", "检索失败"),
                    kind=(data or {}).get("error", "exception"),
                    retryable=True, source=self.name,
                )
            status2, data2 = client.get("/paper/search",
                                        params={"query": title, "limit": 1, "fields": DETAIL_FIELDS})
            if status2 == 200 and isinstance(data2, dict) and data2.get("data"):
                paper = data2["data"][0]
            elif status2 is None:
                raise ProviderError(rate_limit_message(), kind="rate_limited",
                                    retryable=True, source=self.name)

        if not paper:
            return None
        result = normalize_s2_paper(paper)
        client.cache_set(cache_key, result)
        return result


provider = SemanticScholarProvider()
