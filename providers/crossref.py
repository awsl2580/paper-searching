"""Crossref provider —— 正式出版物元数据的权威源，免费且无需注册。

为什么值得单独接一个源（而不是靠 OpenAlex 的元数据）：

1. **卷期页码最全**。GB/T 7714 要求著录"年, 卷(期): 起止页"，而 OpenAlex 对
   很多中文/小型期刊缺 volume/issue/pages。Crossref 是 DOI 注册机构本身，
   出版商提交元数据时就带这些字段 —— 实测同一篇论文，Crossref 的卷期页比
   OpenAlex 完整。这直接影响参考文献表的准确度。
2. **作者名更规范**：Crossref 分开给 given/family，不用像 OpenAlex 那样
   从 `曾军英ZengJunying` 里剥拼音。
3. **无 key**，只要带 `mailto` 进礼貌池（polite pool）。

要点与坑（都是实测）：

* **礼貌池有并发限制**。响应头里能拿到：
      x-api-pool: polite-array / polite-single
      x-rate-limit-limit / x-rate-limit-interval
  实测检索接口（/works?query.*）是 **3 次/秒**，按 DOI 取单条是 10 次/秒，
  公开池（不带 mailto）只有 5 次/秒且并发 1。所以这里 min_interval 给 0.34s，
  并且**必须带 mailto**，否则并发一上来就 429。
* **必须用 select 收窄字段**。/works 默认返回极其臃肿（每条约 3-5 万字符，
  含全部 reference 列表），不 select 的话一次检索就能返回几 MB。
* **摘要是 JATS XML**，形如 `<jats:p>...</jats:p>`，要剥标签。
* Crossref **不提供被引次数**（is-referenced-by-count 是 Crossref 自己
  统计的"被引用"数，很多出版商没提交，实测经常是 0，不能当影响力指标用）。
  所以这里如实给 0，不假装有数据。

环境变量：
    CROSSREF_MAILTO        建议填写（进礼貌池、提高配额与稳定性）
    CROSSREF_MIN_INTERVAL  两次请求最小间隔秒数（默认 0.34 ≈ 3 次/秒）
    CROSSREF_CACHE_TTL     缓存秒数（默认 1800）
    API_TIMEOUT            单次请求超时秒数（默认 30）
"""

from __future__ import annotations

import os
import re

from providers.base import Provider, ProviderError, normalize_paper
from providers.http import DEFAULT_UA, HttpClient

CROSSREF_API = "https://api.crossref.org"

CROSSREF_MAILTO = (os.getenv("CROSSREF_MAILTO") or "").strip()
CROSSREF_MIN_INTERVAL = float(os.getenv("CROSSREF_MIN_INTERVAL", "0.34"))
CROSSREF_CACHE_TTL = int(os.getenv("CROSSREF_CACHE_TTL", "1800"))
TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))

if CROSSREF_MAILTO:
    USER_AGENT = f"{DEFAULT_UA} (mailto:{CROSSREF_MAILTO})"
else:
    USER_AGENT = DEFAULT_UA

client = HttpClient(
    "crossref",
    CROSSREF_API,
    min_interval=CROSSREF_MIN_INTERVAL,
    max_retries=3,
    backoff=1.5,
    backoff_cap=10,
    cache_ttl=CROSSREF_CACHE_TTL,
    timeout=TIMEOUT,
    user_agent=USER_AGENT,
    proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
)

# 只取我们真正用得上的字段。不 select 的话响应体能大到几 MB。
#
# 注意：**字段名写错一个，整个请求就 400**（Crossref 返回 validation-failure），
# 实测 `language` 就不在 /works 列表路由的可用 select 里 ——
# 虽然按 DOI 取单条时能用，但列表路由上带着它会让整次检索直接失败。
# 所以这里不选 language，语种交给 normalize_paper 的 guess_language 按标题判断
# （中文论文标题本身就是汉字，判断足够准）。
_SELECT = ",".join((
    "DOI", "title", "author", "issued", "published-print", "published-online",
    "container-title", "short-container-title", "abstract", "type", "volume",
    "issue", "page", "publisher", "URL", "subject",
    "is-referenced-by-count", "link", "license",
))

# Crossref type -> 统一结构的 docType（决定 [J]/[C]/[D]/[M]/[EB/OL]）
_DOC_TYPE = {
    "journal-article": "journal",
    "proceedings-article": "conference",
    "book-chapter": "book",
    "book-part": "book",
    "book-section": "book",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "dissertation": "thesis",
    "posted-content": "preprint",
    "report": "journal",
    "dataset": "preprint",
    "peer-review": "journal",
    "standard": "book",
}

_TAG_RE = re.compile(r"<[^>]+>")
_JATS_WRAP = re.compile(r"^\s*<jats:title[^>]*>.*?</jats:title>", re.S | re.I)


def _strip_jats(text):
    """Crossref 的摘要/标题是 JATS XML 片段，剥成纯文本。

    实测摘要长这样：
        <jats:title>Abstract</jats:title><jats:p>We propose ...</jats:p>
    直接塞给模型又占 token 又难看，所以剥标签、去开头的 "Abstract" 小标题。
    """
    if not text:
        return ""
    text = _JATS_WRAP.sub("", str(text))
    text = _TAG_RE.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#x2010;", "-")
    return re.sub(r"\s+", " ", text).strip()


def _first(value):
    """Crossref 很多字段是数组（title/container-title），取第一个。"""
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def _year_of(item):
    for key in ("issued", "published-print", "published-online"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            return int(parts[0])
    return "未知"


def _pages_of(item):
    page = (item.get("page") or "").strip()
    return page


def _authors_of(item):
    out = []
    for a in item.get("author") or []:
        if not isinstance(a, dict):
            continue
        # 中文作者 Crossref 常给 name（"张三"）而不是 given/family
        name = (a.get("name") or "").strip()
        if not name:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            # 中文姓名：Crossref 会给 family=张 given=三，要还原成"张三"
            if given and family:
                if re.search(r"[\u4e00-\u9fff]", given + family):
                    name = family + given
                else:
                    name = f"{given} {family}"
            else:
                name = given or family
        if name:
            out.append(name)
    return out


def _pdf_link(item):
    """从 link 里找能直接下载 PDF 的直链。

    只在 content-type 明确是 PDF 时才认，别拿 landing page 冒充全文 ——
    那正是用户抱怨"点进去只有摘要"的原因。
    """
    for link in item.get("link") or []:
        if not isinstance(link, dict):
            continue
        ctype = (link.get("content-type") or "").lower()
        url = link.get("URL") or ""
        if "pdf" in ctype and url:
            return url
    return ""


def normalize_crossref_paper(item):
    doi = (item.get("DOI") or "").strip()
    title = _strip_jats(_first(item.get("title"))) or "未知标题"
    journal = _first(item.get("container-title")) or ""
    if not journal:
        journal = _first(item.get("short-container-title")) or ""
    publisher = (item.get("publisher") or "").strip()

    # 落地页优先 DOI 链接（稳定），没有才用 Crossref 给的 URL
    url = f"https://doi.org/{doi}" if doi else (item.get("URL") or "")

    # 不给 language，交给 normalize_paper 按标题判断语种
    return normalize_paper(
        {
            "paperId": doi or (item.get("URL") or ""),
            "title": title,
            "authors": _authors_of(item),
            "year": _year_of(item),
            "abstract": _strip_jats(item.get("abstract")) or "暂无摘要",
            "url": url,
            "venue": journal or publisher,
            "journal": journal,
            "doi": doi,
            "citationCount": item.get("is-referenced-by-count") or 0,
            "fieldsOfStudy": [s for s in (item.get("subject") or []) if s][:5],
            "openAccessPdf": _pdf_link(item),
            # Crossref 不告诉我们是否 OA，有 PDF 直链就当有免费全文
            "isOa": bool(_pdf_link(item)),
            "volume": item.get("volume") or "",
            "issue": item.get("issue") or "",
            "pages": _pages_of(item),
            "docType": _DOC_TYPE.get(item.get("type") or "", "journal"),
        },
        source="crossref",
        source_label="Crossref",
    )


class CrossrefProvider(Provider):
    name = "crossref"
    label = "Crossref"
    # Crossref 覆盖全语种（中文期刊只要有 DOI 就在里面）
    languages = ("en", "zh")

    def available(self):
        return True

    def search(self, query, limit=5, language=None, offset=0):
        """检索。offset 语义与其他 provider 一致（跳过前 N 条）。

        Crossref 用 offset 参数直接分页，语义天然对齐，不用自己换算。
        """
        query = (query or "").strip()
        if not query:
            raise ProviderError("请提供搜索关键词", kind="http_error", source=self.name)

        limit = min(max(1, int(limit or 5)), 100)
        offset = max(0, int(offset or 0))

        cache_key = ("search", query.lower(), limit, offset, language or "")
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        params = {
            "query.bibliographic": query,
            "rows": limit,
            "offset": offset,
            "select": _SELECT,
        }
        # 礼貌池：没有 mailto 就会掉进公开池（5 次/秒、并发 1），很容易 429
        if CROSSREF_MAILTO:
            params["mailto"] = CROSSREF_MAILTO

        status, data = client.get("/works", params=params)
        if status is None:
            kind = (data or {}).get("error", "exception")
            raise ProviderError(
                (data or {}).get("message", "Crossref 检索失败"),
                kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
            )
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(
                f"Crossref 返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        items = ((data.get("message") or {}).get("items")) or []
        papers = [normalize_crossref_paper(it) for it in items if isinstance(it, dict)]
        client.cache_set(cache_key, papers)
        return papers

    def detail(self, raw_id):
        """按 DOI 取详情。raw_id 允许是 DOI / crossref:DOI / doi.org 链接。"""
        doi = (raw_id or "").strip()
        if not doi:
            return None
        if doi.startswith("crossref:"):
            doi = doi[len("crossref:"):]
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
        if not doi:
            return None

        cache_key = ("work", doi.lower())
        cached = client.cache_get(cache_key)
        if cached is not None:
            return cached

        params = {}
        if CROSSREF_MAILTO:
            params["mailto"] = CROSSREF_MAILTO

        # 注意：**单条路由 /works/{doi} 不支持 select 参数**（实测报
        # "This route does not support select"），所以这里不能照抄列表路由的写法。
        # 代价是响应体更大（含全部 reference），但只取一条，可以接受。
        status, data = client.get(f"/works/{doi}", params=params)
        if status == 404:
            return None
        if status is None:
            kind = (data or {}).get("error", "exception")
            raise ProviderError(
                (data or {}).get("message", "Crossref 详情获取失败"),
                kind=kind, retryable=kind in ("rate_limited", "timeout"), source=self.name,
            )
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(
                f"Crossref 返回异常状态 {status}",
                kind="http_error", source=self.name, status=status,
            )

        item = data.get("message")
        if not isinstance(item, dict):
            return None
        paper = normalize_crossref_paper(item)
        client.cache_set(cache_key, paper)
        return paper


provider = CrossrefProvider()
