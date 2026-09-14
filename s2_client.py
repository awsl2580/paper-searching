"""Semantic Scholar 访问层：全局节流 + 指数退避 + 结果缓存 + 可选 API Key。

为什么需要这个模块（实测结论，不是猜测）：

1. 不带 API Key 时，所有匿名调用者共用同一把公共 key（1000 QPS），
   你能分到多少完全取决于别人用了多少 —— 高峰期 /paper/search 几乎必然 429。
   带 API Key 则是自己的独立配额（默认 1 QPS），从"抢公共池"变成"保底可用"。
2. /paper/search 是最容易被限流的端点；/paper/{id} 基本不限流；
   /paper/search/bulk 的限流额度与 /paper/search 相互独立，可以互相兜底。
3. 被限流时不一定返回 429，也可能是 500 —— 两者都要当"被限流"处理并退避。
4. 429 不带 Retry-After 头，所以只能自己指数退避。
5. 未做节流的客户端会在几百毫秒内把所有重试打完，然后每秒重复一次，
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

import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter

S2_API = "https://api.semanticscholar.org/graph/v1"

S2_API_KEY = (os.getenv("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
S2_MIN_INTERVAL = float(os.getenv("S2_MIN_INTERVAL", "1.2"))
S2_MAX_RETRIES = int(os.getenv("S2_MAX_RETRIES", "4"))
S2_BACKOFF_CAP = float(os.getenv("S2_BACKOFF_CAP", "20"))
S2_CACHE_TTL = int(os.getenv("S2_CACHE_TTL", "1800"))
S2_BULK_FALLBACK = os.getenv("S2_BULK_FALLBACK", "1") != "0"
TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
USER_AGENT = os.getenv("S2_USER_AGENT", "LiteratureSearchAssistant/1.0 (personal research tool)")

# 全局节流：所有出站请求串行排队，间隔不小于 S2_MIN_INTERVAL
_pace_lock = threading.Lock()
_next_call_at = 0.0

# 结果缓存：相同检索 / 相同论文不再重复打接口
_cache_lock = threading.Lock()
_cache = {}


def _pace():
    """阻塞到允许发起下一次请求为止（跨线程共享）"""
    global _next_call_at
    if S2_MIN_INTERVAL <= 0:
        return
    with _pace_lock:
        now = time.monotonic()
        wait = _next_call_at - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _next_call_at = now + S2_MIN_INTERVAL


def cache_get(key):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        value, expire_at = hit
        if expire_at < now:
            _cache.pop(key, None)
            return None
        return value


def cache_set(key, value):
    if S2_CACHE_TTL <= 0:
        return
    with _cache_lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (value, time.time() + S2_CACHE_TTL)


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _make_session() -> requests.Session:
    """创建 Session。重试全部交给 request()，所以这里 max_retries=0，
    避免 urllib3 的自动重试和我们的退避叠加、把请求量放大数倍。"""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0, pool_connections=5, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


SESSION = _make_session()


def request(path, params, timeout=None, retries=None):
    """带节流 + 指数退避的 Semantic Scholar GET 请求。

    返回 (status_code, payload)：
        (200, dict)  成功
        (404, None)  资源不存在
        (其他, dict) 明确的服务端错误
        (None, dict) 彻底失败，payload["error"] 取值：
                     timeout / connection / exception / bad_json / rate_limited
    """
    timeout = TIMEOUT if timeout is None else timeout
    retries = S2_MAX_RETRIES if retries is None else retries

    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if S2_API_KEY:
        headers["x-api-key"] = S2_API_KEY

    url = f"{S2_API}{path}"
    wait = 2.0
    last = "未知错误"

    for attempt in range(1, retries + 1):
        _pace()
        try:
            resp = SESSION.get(url, params=params, headers=headers, timeout=timeout)
        except requests.Timeout:
            last = "请求超时"
            if attempt < retries:
                time.sleep(min(wait, S2_BACKOFF_CAP))
                wait *= 2
                continue
            return None, {"error": "timeout", "message": "请求超时（学术接口响应慢或网络不稳定）"}
        except requests.ConnectionError:
            return None, {"error": "connection", "message": "网络连接失败，请检查网络或配置代理（HTTPS_PROXY）"}
        except Exception as e:
            return None, {"error": "exception", "message": f"{type(e).__name__}: {e}"}

        if resp.status_code == 200:
            try:
                return 200, resp.json()
            except ValueError:
                return None, {"error": "bad_json", "message": "接口返回了无法解析的内容"}

        if resp.status_code == 404:
            return 404, None

        # 429 = 明确限流；5xx 在高峰期同样是限流信号
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = float(retry_after)
            else:
                delay = min(wait, S2_BACKOFF_CAP)
                wait *= 2
            last = f"HTTP {resp.status_code}"
            if attempt < retries:
                time.sleep(delay)
                continue
            return None, {"error": "rate_limited", "status": resp.status_code, "detail": last}

        # 其它 4xx：请求本身的问题，重试没有意义
        return resp.status_code, {"error": "http_error", "status": resp.status_code, "detail": resp.text[:200]}

    return None, {"error": "rate_limited", "detail": last}


def rate_limit_message():
    """被限流时对用户说的话：讲清原因和真正管用的解决办法"""
    if S2_API_KEY:
        hint = "已配置 API Key，但此刻仍被限流，等几秒再试即可。"
    else:
        hint = ("当前未配置 Semantic Scholar API Key，请求走的是所有人共用的公共通道，"
                "高峰期很容易被限流。在 .env 里设置 SEMANTIC_SCHOLAR_API_KEY 可获得独立配额。")
    return f"学术检索接口正在限流（429/5xx），本次没能取回结果。{hint}"


def rate_limit_payload():
    """给 Agent/前端用的限流结果（JSON 字符串）"""
    import json
    return json.dumps({
        "error": "rate_limited",
        "message": rate_limit_message(),
        "retryable": True,
        "suggestion": "请等待约 10 秒后重试。相同检索在缓存有效期内会直接命中本地缓存，不会再打接口。",
    }, ensure_ascii=False)


def normalize_paper(p):
    """把接口返回的一篇论文规格化成前端要的精简结构"""
    tldr = p.get("tldr")
    return {
        "paperId": p.get("paperId", ""),
        "title": p.get("title", "未知标题"),
        "authors": [a.get("name", "") for a in (p.get("authors") or [])],
        "year": p.get("year", "未知"),
        "abstract": p.get("abstract") or "暂无摘要",
        "tldr": tldr.get("text", "") if tldr else "",
        "url": p.get("url", ""),
        "venue": p.get("venue", "") or "",
        "journal": (p.get("journal") or {}).get("name", ""),
        "citationCount": p.get("citationCount", 0),
        "referenceCount": p.get("referenceCount", 0),
        "fieldsOfStudy": p.get("fieldsOfStudy") or [],
        "openAccessPdf": (p.get("openAccessPdf") or {}).get("url", ""),
    }


SEARCH_FIELDS = "paperId,title,authors,year,abstract,url,venue,citationCount,fieldsOfStudy,publicationTypes"
DETAIL_FIELDS = SEARCH_FIELDS + ",referenceCount,tldr,openAccessPdf,journal"


def _bulk_search(query, limit, fields=SEARCH_FIELDS):
    """兜底方案：/paper/search/bulk 与 /paper/search 限流额度独立，一次能取更多结果。
    代价是排序不按相关性，所以只在前者失败时使用。"""
    status, data = request("/paper/search/bulk", {"query": query, "fields": fields})
    if status != 200 or not isinstance(data, dict):
        return None
    return [normalize_paper(p) for p in (data.get("data") or [])[:limit]]


def search_papers(query, limit=5):
    """检索论文，返回 JSON 字符串（供 LLM 工具直接返回）"""
    import json

    if not query or not query.strip():
        return json.dumps({"error": "请提供搜索关键词"}, ensure_ascii=False)

    query = query.strip()
    try:
        limit = min(max(1, int(limit)), 10)
    except (TypeError, ValueError):
        limit = 5

    cache_key = ("search", query.lower(), limit)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    status, data = request("/paper/search", {"query": query, "limit": limit, "fields": SEARCH_FIELDS})

    papers = None
    throttled = False
    if status == 200 and isinstance(data, dict):
        papers = [normalize_paper(p) for p in (data.get("data") or [])]
    elif status is None:
        if (data or {}).get("error") == "rate_limited":
            throttled = True
        else:
            return json.dumps({"error": (data or {}).get("message", "检索失败"), "retryable": True}, ensure_ascii=False)
    else:
        return json.dumps({"error": f"检索接口返回异常状态 {status}", "retryable": False}, ensure_ascii=False)

    # 相关性检索被限流或没结果时，用 bulk 端点兜底
    if (throttled or papers == []) and S2_BULK_FALLBACK:
        bulk = _bulk_search(query, limit)
        if bulk:
            papers = bulk

    if throttled and not papers:
        return rate_limit_payload()

    if not papers:
        result = json.dumps({
            "message": f"未找到与「{query}」相关的论文，请尝试其他关键词（建议使用英文）",
            "papers": [],
        }, ensure_ascii=False)
    else:
        for p in papers:
            p["abstract"] = (p.get("abstract") or "暂无摘要")[:300]
        result = json.dumps({"papers": papers, "total": len(papers)}, ensure_ascii=False)

    cache_set(cache_key, result)
    return result


def get_paper_by_id(paper_id):
    """按 paperId 取详情（这个端点基本不被限流）"""
    import json

    cache_key = ("paper", paper_id)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # /paper/{id} 是公认最稳的端点，但公共池拥挤时一样会 429，
    # 所以这里多给几次机会（每次尝试本身已被全局节流拉开间隔）
    status, data = request(f"/paper/{paper_id}", {"fields": DETAIL_FIELDS},
                           timeout=max(TIMEOUT, 60), retries=max(S2_MAX_RETRIES, 4))

    if status == 404:
        return json.dumps({"error": "未找到该论文，请检查ID是否正确"}, ensure_ascii=False)
    if status != 200 or not isinstance(data, dict):
        if status is None and (data or {}).get("error") == "rate_limited":
            return rate_limit_payload()
        return json.dumps({"error": (data or {}).get("message", f"获取论文详情失败（状态 {status}）"),
                           "retryable": True}, ensure_ascii=False)

    result = json.dumps(normalize_paper(data), ensure_ascii=False)
    cache_set(cache_key, result)
    return result


def get_paper_by_title(title):
    """按标题取详情：用 /paper/search/match（专用端点，只打一个请求），被限流再退回普通检索"""
    import json

    cache_key = ("match", title.lower())
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    status, data = request("/paper/search/match", {"query": title, "fields": DETAIL_FIELDS})
    paper = None

    if status == 200 and isinstance(data, dict) and data.get("data"):
        paper = data["data"][0]
    elif status == 404:
        return json.dumps({"message": f"未找到标题匹配「{title}」的论文", "paper": None}, ensure_ascii=False)
    elif status is None:
        if (data or {}).get("error") != "rate_limited":
            return json.dumps({"error": (data or {}).get("message", "检索失败"), "retryable": True}, ensure_ascii=False)
        status2, data2 = request("/paper/search", {"query": title, "limit": 1, "fields": DETAIL_FIELDS})
        if status2 == 200 and isinstance(data2, dict) and data2.get("data"):
            paper = data2["data"][0]
        elif status2 is None:
            return rate_limit_payload()

    if not paper:
        result = json.dumps({"message": f"未找到标题匹配「{title}」的论文", "paper": None}, ensure_ascii=False)
    else:
        result = json.dumps(normalize_paper(paper), ensure_ascii=False)

    cache_set(cache_key, result)
    return result
