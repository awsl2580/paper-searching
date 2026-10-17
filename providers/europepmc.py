"""Europe PMC provider —— 生命科学/生物医学，免费且无需注册。

它补的是 PubMed 之外的两种能力：

1. **一次调用就拿到摘要 + 作者 + 被引次数**。实测 `resultType=core` 时，
   返回里直接有 abstractText、authorList、citedByCount、journalInfo、
   fullTextUrlList，不用像 PubMed E-utilities 那样先 esearch 再 efetch（XML）。
2. **覆盖面比 PubMed 宽**：除 MEDLINE 外还索引 PMC、预印本（PPR）、
    Agricola、专利等，而且**带 citedByCount**（PubMed 本身不提供被引数）。

实测的取舍：
* 标题/摘要以英文为主。对中国期刊，它给的也多是英文版元数据 ——
  所以它**不能当中文源用**（中文覆盖仍然只有 OpenAlex）。这里 languages
  只声明 "en"，就是不让它去顶中文的检索位置。
* 有些记录没有 journalTitle/卷期页（预印本、会议摘要），GB/T 7714 那部分
  只能给占位，由上层兜底。

接口要点：
* `/search?query=...&format=json&pageSize=N&resultType=core`
* 分页用 `cursorMark`（游标），不是 page 偏移；上层要的是 offset，
  所以这里用 pageSize+游标逐页推进来模拟（见 search 里的实现）。
* 无障碍全文链接在 `fullTextUrlList.fullTextUrl[]`，带 availabilityCode：
  OA = 开放获取；S = 需要订阅。只有 OA 才算"能免费读到全文"。

环境变量：
    EUROPEPMC_MIN_INTERVAL  两次请求最小间隔秒数（默认 0.4）
    EUROPEPMC_CACHE_TTL     缓存秒数（默认 1800）
    API_TIMEOUT             单次请求超时秒数（默认 30）
"""

from __future__ import annotations

import os

from providers.base import Provider, ProviderError, normalize_paper
from providers.http import DEFAULT_UA, HttpClient

EPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest"

EPMC_MIN_INTERVAL = float(os.getenv("EUROPEPMC_MIN_INTERVAL", "0.4"))
EPMC_CACHE_TTL = int(os.getenv("EUROPEPMC_CACHE_TTL", "1800"))
TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))

client = HttpClient(
    "europepmc",
    EPMC_API,
    min_interval=EPMC_MIN_INTERVAL,
    max_retries=3,
    backoff=1.5,
    backoff_cap=10,
    cache_ttl=EPMC_CACHE_TTL,
    timeout=TIMEOUT,
    user_agent=DEFAULT_UA,
    proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
)

# Europe PMC 的 pubTypeList -> 统一 docType
_DOC_TYPE_HINTS = (
    ("preprint", "preprint"),
    ("dissertation", "thesis"),
    ("thesis", "thesis"),
    ("book", "book"),
    ("conference", "conference"),
    ("proceedings", "conference"),
)


def _authors_of(record):
    """authorList.author[] 里 fullName 已经是我们想要的形状（"Mancini K"）。"""
    out = []
    for a in ((record.get("authorList") or {}).get("author") or []):
        if isinstance(a, dict):
            name = (a.get("fullName") or "").strip()
            if not name:
                last = (a.get("lastName") or "").strip()
                initials = (a.get("initials") or "").strip()
                name = (f"{last} {initials}").strip()
            if name:
                out.append(name)
    if not out and record.get("authorString"):
        # 退化：authorString 是 "A B, C D." 形式
        raw = record["authorString"].rstrip(".")
        out = [p.strip() for p in raw.split(",") if p.strip()]
    return out


def _journal_of(record):
    info = record.get("journalInfo") or {}
    journal = info.get("journal") or {}
    return (journal.get("title") or journal.get("medlineAbbreviation")
            or record.get("journalTitle") or "")


def _pages_of(record):
    info = record.get("journalInfo") or {}
    page = (info.get("pageInfo") or record.get("pageInfo") or "").strip()
    return page


def _oa_pdf(record):
    """找开放获取的全文/PDF 链接。

    availabilityCode: "OA" = 开放获取，"F" = 免费，"S" = 需订阅。
    只有 OA/F 才当成免费全文；S 的链接（通常就是 DOI 页）不能冒充全文。
    """
    urls = ((record.get("fullTextUrlList") or {}).get("fullTextUrl")) or []
    best = ""
    for item in urls:
        if not isinstance(item, dict):
            continue
        code = (item.get("availabilityCode") or "").upper()
        url = (item.get("url") or "").strip()
        if not url or code not in ("OA", "F"):
            continue
        style = (item.get("documentStyle") or "").lower()
        if style in ("pdf", "html") and not best:
            best = url
        if style == "pdf":
            return url
    return best


def _doc_type(record):
    types = " ".join(str(t).lower() for t in ((record.get("pubTypeList") or {}).get("pubType") or []))
    for needle, mapped in _DOC_TYPE_HINTS:
        if needle in types:
            return mapped
    return "journal"


def normalize_epmc_paper(record):
    pmid = (record.get("pmid") or "").strip()
    doi = (record.get("doi") or "").strip()
    raw_id = pmid or (record.get("id") or "")

    url = f"https://europepmc.org/article/{record.get('source') or 'MED'}/{raw_id}" if raw_id else ""
    if doi:
        url = f"https://doi.org/{doi}"

    is_oa = str(record.get("isOpenAccess") or "").upper() == "Y"
    pdf = _oa_pdf(record)

    return normalize_paper(
        {
            "paperId": raw_id,
            "title": (record.get("title") or "未知标题").strip().rstrip("."),
            "authors": _authors_of(record),
            "year": record.get("pubYear") or "未知",
            "abstract": (record.get("abstractText") or "").strip() or "暂无摘要",
            "url": url,
            "venue": _journal_of(record),
            "journal": _journal_of(record),
            "doi": doi,
            # Europe PMC 提供被引次数，这是它比 PubMed 强的地方
            "citationCount": record.get("citedByCount") or 0,
            "fieldsOfStudy": [t for t in ((record.get("pubTypeList") or {}).get("pubType") or [])][:5],
            "openAccessPdf": pdf,
            "isOa": is_oa,
            "oaStatus": "gold" if (is_oa and pdf) else ("green" if is_oa else ""),
            "volume": ((record.get("journalInfo") or {}).get("volume") or ""),
            "issue": str((record.get("journalInfo") or {}).get("issue") or ""),
            "pages": _pages_of(record),
            "docType": _doc_type(record),
        },
        source="europepmc",
        source_label="Europe PMC",
        # 这个源的元数据以英文为主，不声明 zh
        language=(record.get("language") or "eng")[:2] if record.get("language") else "en",
    )


class EuropePmcProvider(Provider):
    name = "europepmc"
    label = "Europe PMC"
    languages = ("en",)

    def search(self, query, limit=5, language=None, offset=0):
        """检索。

        Europe PMC 用 cursorMark 游标分页，没有 offset 参数。
        为了对上层保持统一的 offset 语义，这里从第一页开始翻，
        一直翻到覆盖 [offset, offset+limit) 为止（同一次查询内结果顺序稳定）。
        翻页成本很低：只在真需要深翻时才多发请求，浅翻只发一次。
        """
        query = (query or "").strip()
        if not query:
            raise ProviderError("请提供搜索关键词", kind="http_error", source=self.name)

        limit = min(max(1, int(limit or 5)), 100)
        offset = max(0, int(offset or 0))

        cache_key = ("search", query.lower(), limit, offset)
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        collected = []
        cursor = "*"
        # 最多翻 5 页，避免深 offset 时无限打接口
        for _ in range(5):
            params = {
                "query": query,
                "format": "json",
                "pageSize": limit,
                "resultType": "core",
                "cursorMark": cursor,
            }
            status, data = client.get("/search", params=params)
            if status is None:
                kind = (data or {}).get("error", "exception")
                raise ProviderError(
                    (data or {}).get("message", "Europe PMC 检索失败"),
                    kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
                )
            if status != 200 or not isinstance(data, dict):
                raise ProviderError(
                    f"Europe PMC 返回异常状态 {status}",
                    kind="http_error", source=self.name, status=status,
                )

            results = ((data.get("resultList") or {}).get("result")) or []
            collected.extend(r for r in results if isinstance(r, dict))
            if len(collected) >= offset + limit or not results:
                break
            next_cursor = data.get("nextCursorMark")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        window = collected[offset:offset + limit]
        papers = [normalize_epmc_paper(r) for r in window]
        client.cache_set(cache_key, papers)
        return papers

    def detail(self, raw_id):
        """按 PMID / PMCID / DOI 取详情。"""
        key = (raw_id or "").strip()
        if not key:
            return None
        if key.startswith("europepmc:"):
            key = key[len("europepmc:"):]

        cache_key = ("detail", key.lower())
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        # Europe PMC 的 query 语法：按 pmid / pmcid / doi 精确查
        if key.upper().startswith("PMC"):
            query = f"PMCID:{key}"
        elif key.isdigit():
            query = f"EXT_ID:{key}"
        else:
            query = f'DOI:"{key}"'

        status, data = client.get("/search", params={
            "query": query, "format": "json", "pageSize": 1, "resultType": "core",
        })
        if status is None:
            kind = (data or {}).get("error", "exception")
            raise ProviderError(
                (data or {}).get("message", "Europe PMC 详情获取失败"),
                kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
            )
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(
                f"Europe PMC 返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        results = ((data.get("resultList") or {}).get("result")) or []
        paper = normalize_epmc_paper(results[0]) if results else None
        client.cache_set(cache_key, paper)
        return paper


provider = EuropePmcProvider()
