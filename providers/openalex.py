"""OpenAlex provider —— 免费、开放、无需 API Key。

为什么选它做"保底的中文源"：

1. 完全免费、无配额谈判、不用注册 key，`mailto` 只是进"礼貌池"的礼貌做法。
2. 元数据质量稳定，而且**带 language 字段** —— 所以可以用 filter=language:zh
   直接拿中文文献。Semantic Scholar 基本只覆盖英文，这正是缺的那一块。
3. 摘要以 abstract_inverted_index（词 -> 位置数组）形式给出，需要自己还原成
   文本；还原算法就是按位置排序再拼回去，见 _abstract_from_inverted_index。

局限（要如实告诉用户，别让人以为它能替代知网）：
    中文期刊的覆盖率远不如知网/万方，很多中文核心期刊没有 DOI、没有摘要，
    年份也可能缺失。它是"能用的免费方案"，不是"等价替代"。

环境变量：
    OPENALEX_MAILTO         可选，填邮箱进礼貌池（强烈建议，能明显提升稳定性）
    OPENALEX_MIN_INTERVAL   两次请求最小间隔秒数（默认 0.2）
    OPENALEX_CACHE_TTL      缓存秒数（默认 1800）
    API_TIMEOUT             单次请求超时秒数（默认 30）

关于"全文链接"：OpenAlex 的 best_oa_location.pdf_url 是能直接下载完整论文的
直链（实测中文 OA 期刊命中率很高）。它和落地页（通常只是 DOI 摘要页）是两回事，
所以统一结构里分开成两个字段：openAccessPdf（全文）与 url（来源/摘要页）。
"""

from __future__ import annotations

import os

from providers.base import Provider, ProviderError, normalize_paper
from providers.http import DEFAULT_UA, HttpClient

OPENALEX_API = "https://api.openalex.org"

OPENALEX_MAILTO = (os.getenv("OPENALEX_MAILTO") or "").strip()
OPENALEX_MIN_INTERVAL = float(os.getenv("OPENALEX_MIN_INTERVAL", "0.2"))
OPENALEX_CACHE_TTL = int(os.getenv("OPENALEX_CACHE_TTL", "1800"))
TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))

# 礼貌池：带上邮箱，OpenAlex 会给你更稳的服务；没有就退化成普通 UA
if OPENALEX_MAILTO:
    USER_AGENT = f"{DEFAULT_UA} (mailto:{OPENALEX_MAILTO})"
else:
    USER_AGENT = DEFAULT_UA

client = HttpClient(
    "openalex",
    OPENALEX_API,
    min_interval=OPENALEX_MIN_INTERVAL,
    max_retries=3,
    backoff=1.5,
    backoff_cap=10,
    cache_ttl=OPENALEX_CACHE_TTL,
    timeout=TIMEOUT,
    user_agent=USER_AGENT,
    proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
)


def _abstract_from_inverted_index(inverted):
    """OpenAlex 把摘要存成 {词: [位置, ...]}，按位置排序还原成文本。

    摘要缺失在中文文献里非常常见，所以这里返回空串而不是 "暂无摘要"，
    由上层统一兜底。
    """
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions = []
    for word, idxs in inverted.items():
        if not isinstance(idxs, (list, tuple)):
            continue
        for i in idxs:
            if isinstance(i, int):
                positions.append((i, word))
    if not positions:
        return ""
    positions.sort(key=lambda x: x[0])
    return " ".join(word for _, word in positions)


def _year_of(p):
    year = p.get("publication_year")
    if year:
        return year
    date = p.get("publication_date") or ""
    return date[:4] if len(date) >= 4 else "未知"


def _topics_of(p):
    """学科标签取一个够用的字段。

    优先 keywords/concepts 而不是 topics：实测 topics 的层级太粗，对中文文献
    经常给出明显错配的学科（一篇"人工智能在医学影像中的应用"被标成
    "Military Technology and Strategies"），拿来展示还不如不给。
    """
    out = []
    for key in ("keywords", "concepts", "topics"):
        for item in (p.get(key) or []):
            name = item.get("display_name") if isinstance(item, dict) else None
            if name and name not in out:
                out.append(name)
        if out:
            break
    return out[:5]


def _fulltext_pdf(p, primary, best_oa, locations):
    """找出"能直接读到完整论文"的 PDF 直链，找不到返回空串。

    顺序是有讲究的：best_oa_location 是 OpenAlex 自己算出来的最佳位置，
    最可靠；它没标 pdf_url 时再翻 locations 列表（同一篇论文可能有多个副本，
    比如出版商站 + 机构仓储）；最后才看 primary_location。
    注意不要拿 open_access.oa_url 冒充全文——它有时就是个 DOI 链接，
    点进去只有摘要页，那正是用户抱怨的情况。
    """
    for loc in (best_oa, primary):
        if loc.get("pdf_url"):
            return loc["pdf_url"]
    for loc in locations:
        if isinstance(loc, dict) and loc.get("pdf_url"):
            return loc["pdf_url"]
    return ""


# OpenAlex 的 type 字段 -> 统一结构里的 docType
# （决定参考文献里的 [J]/[C]/[D]/[M]/[EB/OL] 标识）
_DOC_TYPE = {
    "article": "journal", "review": "journal", "letter": "journal",
    "editorial": "journal", "erratum": "journal", "report": "journal",
    "proceedings-article": "conference",
    "dissertation": "thesis",
    "preprint": "preprint", "posted-content": "preprint",
    "book": "book", "book-chapter": "book", "reference-entry": "book",
}


def _pages_of(biblio):
    """把 biblio 的首页/末页拼成 GB/T 7714 要的页码串。

    起止相同（有些电子刊只有单页）时只给一个数字，别输出 "14-14"。
    """
    first = (biblio or {}).get("first_page")
    last = (biblio or {}).get("last_page")
    if first and last and str(first) != str(last):
        return f"{first}-{last}"
    if first:
        return str(first)
    return ""


def normalize_openalex_paper(p):
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (p.get("authorships") or [])
    ]
    primary = p.get("primary_location") or {}
    source = primary.get("source") or {}
    best_oa = p.get("best_oa_location") or {}
    oa = p.get("open_access") or {}
    biblio = p.get("biblio") or {}

    doi = p.get("doi") or ""
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")

    # 落地页优先用 primary_location，没有就用 DOI 链接
    url = primary.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else "")

    raw_id = (p.get("id") or "").rsplit("/", 1)[-1]

    return normalize_paper(
        {
            "paperId": raw_id,
            "title": p.get("display_name") or p.get("title") or "未知标题",
            "authors": [a for a in authors if a],
            "year": _year_of(p),
            "abstract": _abstract_from_inverted_index(p.get("abstract_inverted_index")) or "暂无摘要",
            "url": url,
            "venue": source.get("display_name") or "",
            "journal": source.get("display_name") or "",
            "doi": doi,
            "citationCount": p.get("cited_by_count") or 0,
            "referenceCount": len(p.get("referenced_works") or []) or None,
            "fieldsOfStudy": _topics_of(p),
            "openAccessPdf": _fulltext_pdf(p, primary, best_oa, p.get("locations") or []),
            "isOa": bool(oa.get("is_oa")),
            "oaStatus": oa.get("oa_status") or "",
            "volume": biblio.get("volume") or "",
            "issue": biblio.get("issue") or "",
            "pages": _pages_of(biblio),
            "docType": _DOC_TYPE.get(p.get("type") or "", "journal"),
        },
        source="openalex",
        source_label="OpenAlex",
        language=(p.get("language") or "")[:2],
    )


class OpenAlexProvider(Provider):
    name = "openalex"
    label = "OpenAlex"
    languages = ("en", "zh")

    def search(self, query, limit=5, language=None, offset=0):
        """检索。offset 是相对本次查询的偏移，用于翻页。

        OpenAlex 用 page/per_page 分页（page 从 1 开始）。为了对上层暴露
        统一的 offset 语义，这里自己换算：目标窗口落在第几页就取那一页。
        per_page 上限 200，所以单次最多能拿 200 条。
        """
        query = (query or "").strip()
        if not query:
            raise ProviderError("请提供搜索关键词", kind="http_error", source=self.name)

        limit = min(max(1, int(limit or 5)), 200)
        offset = max(0, int(offset or 0))

        # OpenAlex 单次 per_page 上限 200。把 per_page 对齐成"本页条数"，
        # page 就能直接换算 —— offset=10 / limit=10 就是 page=2，只取 10 条，
        # 不会为了第 2 页把第 1 页也重取一遍。
        # limit > 200 时才退化成"一大页里切片"（per_page 封顶 200，
        # page 按 200 一档算），这时返回的窗口必然覆盖 [offset, offset+limit)。
        per_page = min(200, limit)
        page = (offset // per_page) + 1

        cache_key = ("search", query.lower(), per_page, page, language or "")
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        params = {"search": query, "per_page": per_page, "page": page}
        if OPENALEX_MAILTO:
            params["mailto"] = OPENALEX_MAILTO
        # language="zh" 时只要中文文献；不传则不过滤（英文库里本身就是英文为主）
        if language == "zh":
            params["filter"] = "language:zh"

        status, data = client.get("/works", params=params)
        if status is None:
            kind = (data or {}).get("error", "exception")
            raise ProviderError(
                (data or {}).get("message", "OpenAlex 检索失败"),
                kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
            )
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(
                f"OpenAlex 返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        papers = [normalize_openalex_paper(w) for w in (data.get("results") or [])]
        # 这一页里真正落在请求窗口内的部分
        start = offset - (page - 1) * per_page
        papers = papers[start:start + limit]
        client.cache_set(cache_key, papers)
        return papers

    def detail(self, raw_id):
        raw_id = (raw_id or "").strip()
        if not raw_id:
            return None

        # 允许传 W123 / openalex:W123 / doi:10.xxx / 完整 URL
        lookup = raw_id
        if lookup.startswith("openalex:"):
            lookup = lookup[len("openalex:"):]
        if lookup.startswith("https://openalex.org/"):
            lookup = lookup.rsplit("/", 1)[-1]
        if not lookup.startswith(("W", "doi:", "http")):
            lookup = "doi:" + lookup if "/" in lookup or "." in lookup else lookup

        cache_key = ("work", lookup)
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        params = {}
        if OPENALEX_MAILTO:
            params["mailto"] = OPENALEX_MAILTO

        status, data = client.get(f"/works/{lookup}", params=params)
        if status == 404:
            return None
        if status is None:
            kind = (data or {}).get("error", "exception")
            raise ProviderError(
                (data or {}).get("message", "OpenAlex 详情获取失败"),
                kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
            )
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(
                f"OpenAlex 返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        paper = normalize_openalex_paper(data)
        client.cache_set(cache_key, paper)
        return paper


provider = OpenAlexProvider()
