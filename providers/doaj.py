"""DOAJ provider —— 开放获取期刊的权威索引，免费且无需注册。

它补的是别的源给不了的东西：**"这篇能不能免费读到全文"**。
DOAJ 只收录经过审核的开放获取期刊，所以命中即意味着全文开放，
而且 bibjson.link 里直接带 fulltext 链接。

代价（要如实说，别让人以为它很全）：
* **只有 OA 期刊**，订阅制期刊一概没有 —— 所以它不能当主源，只适合做补充。
* **不提供被引次数**。实测返回结构里没有 citationCount 字段，
  这里如实给 0，而不是编一个或从别处凑。
* 覆盖以英文 OA 期刊为主，中文 OA 期刊很少。

接口要点：
* 检索走 `/api/search/articles/{query}`，query 直接拼在路径里（要做 URL 编码）。
* 分页用 page/pageSize，pageSize 上限 100。
* 返回的 `bibjson.journal` 里带 volume/number，页码在 `start_page`/`end_page`。

环境变量：
    DOAJ_MIN_INTERVAL  两次请求最小间隔秒数（默认 0.5）
    DOAJ_CACHE_TTL     缓存秒数（默认 1800）
    API_TIMEOUT        单次请求超时秒数（默认 30）
"""

from __future__ import annotations

import os
from urllib.parse import quote

from providers.base import Provider, ProviderError, normalize_paper
from providers.http import DEFAULT_UA, HttpClient

DOAJ_API = "https://doaj.org"

DOAJ_MIN_INTERVAL = float(os.getenv("DOAJ_MIN_INTERVAL", "0.5"))
DOAJ_CACHE_TTL = int(os.getenv("DOAJ_CACHE_TTL", "1800"))
TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))

client = HttpClient(
    "doaj",
    DOAJ_API,
    min_interval=DOAJ_MIN_INTERVAL,
    max_retries=3,
    backoff=1.5,
    backoff_cap=10,
    cache_ttl=DOAJ_CACHE_TTL,
    timeout=TIMEOUT,
    user_agent=DEFAULT_UA,
    proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
)


def _identifiers(bib):
    """bibjson.identifier 是 [{type, id}]，转成 {type: id} 方便取用。"""
    out = {}
    for item in bib.get("identifier") or []:
        if isinstance(item, dict) and item.get("type") and item.get("id"):
            out[str(item["type"]).lower()] = str(item["id"])
    return out


def _authors(bib):
    out = []
    for a in bib.get("author") or []:
        if isinstance(a, dict):
            name = (a.get("name") or "").strip()
            if name:
                out.append(name)
    return out


def _links(bib):
    """返回 (全文链接, 落地页)。

    DOAJ 的 link.type 实测有 fulltext / homepage 等。
    顺序上优先 fulltext（那是能读到全文的地方）。
    """
    fulltext, homepage = "", ""
    for link in bib.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = (link.get("url") or "").strip()
        kind = (link.get("type") or "").lower()
        if not url:
            continue
        if kind == "fulltext" and not fulltext:
            fulltext = url
        elif kind != "fulltext" and not homepage:
            homepage = url
    return fulltext, homepage


def _pages(bib):
    start = (bib.get("start_page") or "").strip()
    end = (bib.get("end_page") or "").strip()
    if start and end and start != end:
        return f"{start}-{end}"
    return start


def normalize_doaj_paper(item):
    bib = item.get("bibjson") or {}
    journal = bib.get("journal") or {}
    ids = _identifiers(bib)
    doi = ids.get("doi", "")
    fulltext, homepage = _links(bib)

    url = homepage or (f"https://doi.org/{doi}" if doi else "")
    if not url:
        url = fulltext

    return normalize_paper(
        {
            "paperId": doi or (item.get("id") or ""),
            "title": (bib.get("title") or "未知标题").strip(),
            "authors": _authors(bib),
            "year": bib.get("year") or "未知",
            "abstract": (bib.get("abstract") or "").strip() or "暂无摘要",
            "url": url,
            "venue": journal.get("title") or "",
            "journal": journal.get("title") or "",
            "doi": doi,
            # DOAJ 不提供被引次数，如实给 0
            "citationCount": 0,
            "fieldsOfStudy": [s for s in (bib.get("subject") or []) if s][:5],
            # DOAJ 全是 OA 期刊；有 fulltext 链接就当作能直接读全文
            "openAccessPdf": fulltext if fulltext.lower().endswith(".pdf") else "",
            "isOa": True,
            "oaStatus": "gold",
            "volume": journal.get("volume") or "",
            "issue": journal.get("number") or "",
            "pages": _pages(bib),
            "docType": "journal",
        },
        source="doaj",
        source_label="DOAJ",
        language=(bib.get("language") or [""])[0][:2] if bib.get("language") else "",
    )


class DoajProvider(Provider):
    name = "doaj"
    label = "DOAJ"
    languages = ("en",)

    def search(self, query, limit=5, language=None, offset=0):
        query = (query or "").strip()
        if not query:
            raise ProviderError("请提供搜索关键词", kind="http_error", source=self.name)

        limit = min(max(1, int(limit or 5)), 100)
        offset = max(0, int(offset or 0))
        # DOAJ 用 page/pageSize，换算成统一的 offset 语义
        page = (offset // limit) + 1

        cache_key = ("search", query.lower(), limit, page)
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        status, data = client.get(
            f"/api/search/articles/{quote(query, safe='')}",
            params={"pageSize": limit, "page": page},
        )
        if status is None:
            kind = (data or {}).get("error", "exception")
            raise ProviderError(
                (data or {}).get("message", "DOAJ 检索失败"),
                kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
            )
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(
                f"DOAJ 返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        papers = [normalize_doaj_paper(it) for it in (data.get("results") or [])
                  if isinstance(it, dict)]
        client.cache_set(cache_key, papers)
        return papers

    def detail(self, raw_id):
        """按 DOAJ 记录 id 或 DOI 取详情。

        DOAJ 没有"按 DOI 查单条"的接口，只能检索 DOI 再取第一条，
        所以这里退化成检索匹配（这也是 base.Provider 里 supports_title_match
        那类降级做法的同类处理）。
        """
        key = (raw_id or "").strip()
        if not key:
            return None
        if key.startswith("doaj:"):
            key = key[len("doaj:"):]

        cache_key = ("detail", key.lower())
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        # 不限制 language，否则按 DOI 找详情会被语言过滤挡住
        results = self.search(key, limit=1)
        paper = results[0] if results else None
        client.cache_set(cache_key, paper)
        return paper


provider = DoajProvider()
