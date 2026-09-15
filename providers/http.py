"""带节流 / 指数退避 / 结果缓存的 HTTP 基座。

这套逻辑原本在 s2_client.py 里，只服务 Semantic Scholar。抽出来是因为
每个源都需要同样的三件事，而每个源的限制又不一样：

    节流      S2 等源有 QPS 限制，OpenAlex 相对宽松但要求带上 mailto 进"礼貌池"
    退避      被限流时不一定返回 429，S2 高峰期返回 500，两者都要当限流处理
    缓存      同一次检索在会话里重复出现很常见，缓存能省掉绝大部分出站请求

约定：request() 不抛异常，统一返回 (status, payload)：

    (200, dict)   成功
    (404, None)   资源不存在
    (其它, dict)  服务端明确的错误状态
    (None, dict)  彻底失败，payload["error"] ∈
                  rate_limited / timeout / connection / exception / bad_json
"""

from __future__ import annotations

import threading
import time

import requests
from requests.adapters import HTTPAdapter

DEFAULT_UA = "LiteratureSearchAssistant/1.0 (personal research tool)"


def new_session(proxy=None):
    """建一个 Session。重试全部由 request() 自己控制，所以这里 max_retries=0，
    避免 urllib3 的自动重试和我们的退避叠加、把请求量放大数倍。"""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0, pool_connections=5, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


class HttpClient:
    def __init__(
        self,
        name,
        base_url,
        *,
        min_interval=0.0,
        max_retries=3,
        backoff=2.0,
        backoff_cap=20.0,
        cache_ttl=1800,
        timeout=30,
        user_agent=DEFAULT_UA,
        default_headers=None,
        proxy=None,
        max_cache=512,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.min_interval = float(min_interval)
        self.max_retries = int(max_retries)
        self.backoff = float(backoff)
        self.backoff_cap = float(backoff_cap)
        self.cache_ttl = int(cache_ttl)
        self.timeout = int(timeout)
        self.user_agent = user_agent
        self.default_headers = dict(default_headers or {})
        self.max_cache = max_cache

        self.session = new_session(proxy)

        # 节流：实例内所有出站请求串行排队，间隔不小于 min_interval
        self._pace_lock = threading.Lock()
        self._next_call_at = 0.0

        # 缓存：key -> (value, expire_at)
        self._cache_lock = threading.Lock()
        self._cache = {}

    # ── 节流 ──────────────────────────────────────────────
    def _pace(self):
        if self.min_interval <= 0:
            return
        with self._pace_lock:
            now = time.monotonic()
            wait = self._next_call_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_call_at = now + self.min_interval

    # ── 缓存 ──────────────────────────────────────────────
    def cache_get(self, key):
        if self.cache_ttl <= 0:
            return None
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            value, expire_at = hit
            if expire_at < now:
                self._cache.pop(key, None)
                return None
            return value

    def cache_set(self, key, value):
        if self.cache_ttl <= 0:
            return
        with self._cache_lock:
            if len(self._cache) > self.max_cache:
                self._cache.clear()
            self._cache[key] = (value, time.time() + self.cache_ttl)

    def clear_cache(self):
        with self._cache_lock:
            self._cache.clear()

    # ── 请求 ──────────────────────────────────────────────
    def request(
        self,
        method,
        path,
        *,
        params=None,
        json_body=None,
        headers=None,
        timeout=None,
        retries=None,
    ):
        """带节流 + 指数退避的请求。返回值见模块文档。"""
        timeout = self.timeout if timeout is None else timeout
        retries = self.max_retries if retries is None else retries

        merged = {"Accept": "application/json", "User-Agent": self.user_agent}
        merged.update(self.default_headers)
        if headers:
            merged.update(headers)

        url = f"{self.base_url}{path}"
        wait = self.backoff
        last = "未知错误"

        for attempt in range(1, retries + 1):
            self._pace()
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=merged, timeout=timeout,
                )
            except requests.Timeout:
                last = "请求超时"
                if attempt < retries:
                    time.sleep(min(wait, self.backoff_cap))
                    wait *= 2
                    continue
                return None, {"error": "timeout", "message": "请求超时（接口响应慢或网络不稳定）"}
            except requests.ConnectionError:
                return None, {
                    "error": "connection",
                    "message": "网络连接失败，请检查网络或配置代理（HTTPS_PROXY）",
                }
            except Exception as e:  # noqa: BLE001 - 任何底层异常都不该中断对话
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
                    delay = min(wait, self.backoff_cap)
                    wait *= 2
                last = f"HTTP {resp.status_code}"
                if attempt < retries:
                    time.sleep(delay)
                    continue
                return None, {"error": "rate_limited", "status": resp.status_code, "detail": last}

            # 其它 4xx：请求本身的问题，重试没有意义。
            # 尽量把响应体也带上——有的平台把真正的原因放在 JSON body 里
            # （例如鉴权失败是 appKey 无效还是参数写错），只给状态码根本看不出问题
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:300]
            return resp.status_code, {
                "error": "http_error",
                "status": resp.status_code,
                "detail": resp.text[:200],
                "body": body,
            }

        return None, {"error": "rate_limited", "detail": last}

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)
