"""DeepSeek 大模型接入层 —— 全项目唯一的大模型出口。

## 为什么单独一个模块

`app.py` / `workflow.py` / `Agent.py` 都需要模型调用，但各自读环境变量、
各自写死模型名的话，很容易出现"聊天用这个模型、综述用那个模型"的混乱。
所以统一收到这里：

    llm.call_json(...)      给工作流/科研团队用（纯 HTTP，可控）
    llm.chat_model(kind)    给 LangChain agent 用（要 tool calling + 真流式）
    llm.model_info()        给 /api/models、前端徽章、README 用

## DeepSeek API 的事实

* base_url：`https://api.deepseek.com/v1`（兼容 OpenAI 的 chat/completions）
* 鉴权：`Authorization: Bearer <key>`，key 在
  https://platform.deepseek.com/api_keys 生成
* 本项目只使用两个模型，界面上也只能选这两个：
    - `deepseek-V4.1-flash`：快速通用模型，负责对话、工具调用、批量抽取、读图；
    - `deepseek-v4-pro`    ：深度推理模型，负责科学假设、复现校验、独立审稿。
* **不要给 DeepSeek 传 `thinking_mode`**：它不是 DeepSeek 的参数（那是别的厂商的
  扩展字段），DeepSeek 的"思考"由模型本身决定 —— `deepseek-v4-pro` 会先输出
  `reasoning_content` 再输出正文，`flash` 直接回答。界面上的「深度思考」开关
  因此实现为**切到 `deepseek-v4-pro`**，而不是往请求体里塞一个不存在的字段。
* 官方接口支持 `response_format`，但为保持两个模型行为一致、也避免某些兼容镜像
  不认这个字段，这里仍然用宽松 JSON 解析兜住模型偶尔包 markdown 的情况。
* 限流：DeepSeek 没有公开固定的 RPM 上限（按账号动态限流）。这里默认按
  60 次/分钟保守起步，可用 `DEEPSEEK_RPM` 调整；工作流一次要跑 8-12 次模型调用，
  多智能体还会并发，所以做了**进程级令牌桶**，别让限流把整条流水线打断。
* 单次 max_tokens 默认 2048，上限 8192；思考内容与正文共用这份预算。
"""

from __future__ import annotations

import json
import os
import random
import threading
import time

# 自己加载 .env：本模块的配置全部来自环境变量，而调用方不一定先 load_dotenv
# （`python -c "import llm; llm.check_models()"` 就是典型情况 —— 不加载就读不到 key）。
from env_boot import load_project_env

load_project_env()

# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-V4.1-flash"
DEFAULT_FAST_MODEL = "deepseek-V4.1-flash"
DEFAULT_VISION_MODEL = "deepseek-V4.1-flash"
DEFAULT_REASON_MODEL = "deepseek-v4-pro"

# 深度推理模型：界面上的「深度思考」开关最终落到这个模型上。
THINKING_MODEL = DEFAULT_REASON_MODEL

# 单次请求的 max_tokens 上限。DeepSeek 对话模型单次输出上限为 8K；
# 再大没有意义，而且思考内容（reasoning）和正文共用这份预算。
MAX_TOKENS_CAP = 8192


def _env(name, default=""):
    v = os.getenv(name)
    return default if v is None else str(v).strip()


def _env_int(name, default):
    try:
        return int(_env(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y")


def _normalize_base(raw):
    """把用户可能写成的几种 base_url 都收敛成 `.../v1`（无尾斜杠）。

    DeepSeek 官方两种写法都可用：`https://api.deepseek.com` 与
    `https://api.deepseek.com/v1`。这里统一成带 `/v1` 的形式，
    这样下面拼 `/chat/completions` 不会出现 `/v1/v1/...`。
    """
    base = (raw or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/v1") or base.endswith("/beta"):
        return base
    # 允许只写域名（https://api.deepseek.com），补上官方路径
    return base + "/v1"


BASE_URL = _normalize_base(_env("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL))

# DeepSeek API Key。不要求带 "Bearer " 前缀（代码自己会加）。
# API_KEY 是 import 时的快照（测试/旧调用会直接改它）；
# 真正发请求时走 api_key()，每次重新读环境变量 —— 这样网页上填完 Key
# 立刻生效，不用重启，也不会被 import 时的空值锁死。
API_KEY = _env("DEEPSEEK_API_KEY")


def api_key():
    """当前生效的 DeepSeek API Key（没有就返回空串）。"""
    return _env("DEEPSEEK_API_KEY") or API_KEY

# 限流：默认 60 次/分钟（DeepSeek 按账号动态限流，这里保守起步，留余量给重试）
RPM = max(1, _env_int("DEEPSEEK_RPM", 60))
TIMEOUT = max(10, _env_int("DEEPSEEK_TIMEOUT", 120))

# 深度思考："auto" 按用途/模型自动判断；"1"/"0" 强制开关。
# 注意：这只影响"是否切到 deepseek-v4-pro"和界面展示，不会往请求体塞字段。
THINKING_SETTING = _env("DEEPSEEK_THINKING_MODE", "auto").lower()


def _model_env(env_name, default):
    return _env(env_name, "") or default


# 每种用途对应一个模型。两个模型按任务特点分工：
#   * 检索/抽取/读图这类"读短文出 JSON / 看图"的活，用 flash 又快又省；
#   * 假设生成、复现校验、独立审稿这类需要推理的活，用 pro（先推理再回答）。
PURPOSES = {
    "chat":   {"env": "DEEPSEEK_MODEL",        "default": DEFAULT_MODEL,
               "label": "对话主模型",   "why": "智能体主循环、工具调用、多轮对话"},
    "fast":   {"env": "DEEPSEEK_FAST_MODEL",   "default": DEFAULT_FAST_MODEL,
               "label": "轻量批处理",   "why": "分批信息抽取、字段归一化等高频小任务"},
    "vision": {"env": "DEEPSEEK_VISION_MODEL", "default": DEFAULT_VISION_MODEL,
               "label": "多模态读图",   "why": "论文截图/谱图/分子结构/实验照片理解"},
    "reason": {"env": "DEEPSEEK_REASON_MODEL", "default": DEFAULT_REASON_MODEL,
               "label": "深度推理",     "why": "科学假设生成、复现校验、审稿"},
}

# ── 可在界面上切换的模型清单 ──────────────────────────
# 按需求，下拉列表**只允许这两个模型**。服务端还有一层白名单校验：
# 请求体里传任何别的名字都会被回落到默认模型，不会把任意字符串送进 API。
MODEL_CATALOG = [
    # id/name 是界面上显示与选择的标识；api_model 才是真正发给 DeepSeek 的
    # 模型 ID。DeepSeek 当前实际接受的是 `deepseek-flash`（它的展示名才是
    # "DeepSeek-V4.1-Flash"），所以这里必须做一次映射，否则接口会直接 400：
    #   "The supported API model names are deepseek-flash, deepseek-v4-pro"
    {"id": "deepseek-V4.1-flash", "api_model": "deepseek-flash",
     "name": "DeepSeek-V4.1-Flash", "badge": "NEW",
     "desc": "快速通用模型：对话、工具调用、批量抽取与读图，首字快、成本低",
     "purpose": "chat", "verified": True, "default": True, "multimodal": True},
    {"id": "deepseek-v4-pro", "api_model": "deepseek-v4-pro",
     "name": "DeepSeek-V4-Pro",
     "desc": "深度推理模型：科学假设、复现校验与独立审稿，先推理再回答",
     "purpose": "reason", "verified": True, "thinking": True},
]

SELECTABLE_MODELS = tuple(m["id"] for m in MODEL_CATALOG)
API_MODEL_ALIASES = {m["id"]: m.get("api_model") or m["id"] for m in MODEL_CATALOG}


def api_model_for(model):
    """界面/配置里的模型名 -> 真正发给 DeepSeek 的 API 模型 ID。

    `deepseek-V4.1-flash` 是界面显示名，API 实际只认 `deepseek-flash`；
    `deepseek-v4-pro` 两边同名。把映射收在这一个函数里，请求体、
    ChatOpenAI、check_models 都走它，避免某处漏改又变成 400。
    """
    return API_MODEL_ALIASES.get(model, model)


def is_selectable(model):
    """这个模型名能不能被前端选中？（白名单，防止任意字符串进请求体）"""
    return bool(model) and model in SELECTABLE_MODELS


def _resolve_model(model, kind="chat"):
    """把"用户选的模型"落到一个确定的名字上。

    * 没选 / 不在白名单 -> 该用途的默认模型（这样旧请求体、旧前端都照常工作）；
    * 选了 -> 原样使用（两个模型都允许跨用途使用）。
    """
    if is_selectable(model):
        return model
    return model_for(kind)


def model_catalog():
    """给前端下拉列表：默认项 + 每项的说明 + 是否"本项目已配置"。

    `configured` 为 True 表示它正好是某个用途的默认模型（PURPOSES 里配了）。
    当前两个模型都配置了，所以都是 True。
    """
    configured = {model_for(kind) for kind in PURPOSES}
    models = []
    for m in MODEL_CATALOG:
        item = dict(m)
        item["configured"] = m["id"] in configured
        # 前端据此决定"深度思考"开关是否可点（两个 DeepSeek 模型都支持切到 pro）
        item["supports_thinking"] = _supports_thinking(m["id"])
        models.append(item)
    return {
        "default": _resolve_model(None, "chat"),
        "models": models,
        "note": "默认 deepseek-V4.1-flash；深度思考会切到 deepseek-v4-pro。",
    }


def model_for(kind="chat"):
    """该用途用哪个模型。**只允许清单里的两个模型**。

    即使有人在 .env 里把 `DEEPSEEK_MODEL` 手写成一个清单外的名字，
    这里也会回落到该用途的默认模型 —— 需求是"只能选这两个"，
    不能让环境变量绕过白名单。
    """
    meta = PURPOSES.get(kind) or PURPOSES["chat"]
    name = _model_env(meta["env"], meta["default"])
    return name if name in SELECTABLE_MODELS else meta["default"]


def backend():
    """当前模型后端：'deepseek' / None。

    配了 DeepSeek API Key 就走 DeepSeek；没配就明确返回 None，
    由 `require_llm()` 给出可照做的配置提示，不做"假装能跑"的降级。
    """
    if api_key():
        return "deepseek"
    return None


def available():
    return backend() is not None


def require_llm():
    """启动/请求前的自检，抛出的消息要能直接指导操作。"""
    if available():
        return
    raise RuntimeError(
        "未配置 DeepSeek API Key。\n"
        "1) 打开 https://platform.deepseek.com/api_keys 生成 API Key；\n"
        "2) 在项目根目录的 .env 里写入：\n"
        "     DEEPSEEK_API_KEY=<你的 key>\n"
        "3) 重新启动 app.py。\n"
        "（模型只使用 deepseek-V4.1-flash 与 deepseek-v4-pro，不需要配置别家 token。）"
    )


def _supports_thinking(model):
    """这个模型能不能用「深度思考」？（DeepSeek 的两个模型都可以）

    选中 flash 时打开开关，服务端会自动改用 `deepseek-v4-pro`；
    选中 pro 时开关本来就是"开"的语义。所以这里统一返回 True，
    真正决定用哪个模型的是 `THINKING_MODEL` 与 `thinking_mode_for`。
    """
    return is_selectable(model) or model == THINKING_MODEL


def thinking_mode_for(kind, model=None):
    """该用不用深度思考。返回 True / False（仅用于选模型与界面展示）。

    规则：
      * `DEEPSEEK_THINKING_MODE=1/true/on`     -> 全开（一律用 pro）
      * `DEEPSEEK_THINKING_MODE=0/false/off`   -> 全关（一律用所选模型）
      * `DEEPSEEK_THINKING_MODE=reason`        -> 只给深度推理用途开
      * `auto`（默认）                          -> 只有 reason 用途 / pro 模型算"开"
    """
    if THINKING_SETTING in ("1", "true", "on", "yes", "all"):
        return True
    if THINKING_SETTING in ("0", "false", "off", "no", "none"):
        return False
    model = model or model_for(kind)
    if THINKING_SETTING in ("reason", "reasoning", "推理"):
        return kind == "reason" or model == THINKING_MODEL
    return model == THINKING_MODEL or kind == "reason"


# ═══════════════════════════════════════════════════════════════
#  令牌桶：把并发压到限流以下
# ═══════════════════════════════════════════════════════════════

class RateLimiter:
    """最小间隔式限流（RPM -> 每 min_interval 秒放一个）。

    为什么不用"统计每分钟次数"的窗口：窗口法在时间窗边界会突然允许
    双倍突发，服务端按滑窗算账时照样 429。这里按最小间隔匀速放行，
    实际速率稳定在 RPM/60，不会踩线。
    """

    def __init__(self, rpm=RPM):
        self.min_interval = 60.0 / max(1, rpm)
        self._last = 0.0
        self._lock = threading.Lock()
        self.waits = 0          # 被限流阻塞过多少次（诊断用）

    def acquire(self):
        with self._lock:
            now = time.time()
            wait = self._last + self.min_interval - now
            if wait > 0:
                self.waits += 1
                time.sleep(wait)
                now = time.time()
            self._last = now

    def reconfigure(self, rpm):
        with self._lock:
            self.min_interval = 60.0 / max(1, rpm)


_LIMITER = RateLimiter(RPM)

# 传输层统计：被限流（429）多少次、退避了多少次、发起多少次请求。
# 排查"为什么这一步要 60s"时，这三个数字能立刻区分"模型慢"还是"被限流"。
_HTTP_STATS = {"calls": 0, "http_429": 0, "http_5xx": 0, "backoffs": 0}

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


def http_stats():
    return dict(_HTTP_STATS)

# 最近一次失败的原因（给 /api/models 和日志看，不抛给最终用户）
_LAST_ERROR = {"text": "", "at": 0.0}
_ERROR_LOCK = threading.Lock()


def _note_error(text):
    with _ERROR_LOCK:
        _LAST_ERROR["text"] = str(text)[:400]
        _LAST_ERROR["at"] = time.time()


def last_error():
    with _ERROR_LOCK:
        return dict(_LAST_ERROR)


# ═══════════════════════════════════════════════════════════════
#  宽松 JSON 解析
# ═══════════════════════════════════════════════════════════════

def _json_spans(text):
    """扫出所有"顶层括号平衡"的片段（`{...}` 与 `[...]`），按出现顺序。

    为什么要全部扫一遍：推理模型会先把推理写进正文，推理里经常夹着
    半截 JSON 示例（`{"topic": ...}` 那种）—— 只取"第一个平衡片段"
    会抓到推理里的示例对象，而不是最终答案。
    """
    spans = []
    stack = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append((ch, i))
        elif ch in "}]":
            if not stack:
                continue
            opener, start = stack[-1]
            if (opener == "{" and ch == "}") or (opener == "[" and ch == "]"):
                stack.pop()
                if not stack:
                    spans.append(text[start:i + 1])
    return spans


def loads_json(raw):
    """把模型输出尽量解析成对象。

    模型偶尔会包 ```json 代码块、前后加一句"好的，结果如下："，或者（推理模型）
    在正文里先写一大段推理。这里按逐级退让解析：

        直接解析 -> 去代码块 -> 抓**最后一个**平衡的 {} / []

    "取最后一个"是关键：推理模型会在推理里夹 JSON 示例 ——
    最后一个平衡片段才是最终答案。
    """
    t = (raw or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    for frag in reversed(_json_spans(t)):
        try:
            return json.loads(frag)
        except Exception:
            continue
    return None


def _post_chat(payload, timeout=TIMEOUT):
    """发一次 chat/completions，带限流 + 指数退避重试。返回 (ok, data_or_error)。"""
    import requests

    # 复制一份再改，别动调用方手里的 payload：_chat_impl 还要拿它比较
    # "用户选的模型" 与 "用途默认模型"（那边用的是界面名）。
    payload = dict(payload)
    payload["model"] = api_model_for(payload.get("model"))

    url = BASE_URL + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key(),
    }

    attempts = max(1, _env_int("DEEPSEEK_MAX_RETRIES", 4))
    backoff = 1.0
    for attempt in range(1, attempts + 1):
        _HTTP_STATS["calls"] += 1
        _LIMITER.acquire()
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(payload),
                                 timeout=timeout)
        except Exception as e:  # noqa: BLE001 - 网络异常一律按可重试处理
            if attempt >= attempts:
                msg = f"{type(e).__name__}: {e}"
                _note_error(msg)
                return False, msg
            time.sleep(backoff + random.uniform(0, 0.4))
            backoff = min(backoff * 2, 20)
            continue

        if resp.status_code == 200:
            try:
                return True, resp.json()
            except Exception:
                msg = "接口返回的不是 JSON"
                _note_error(msg)
                return False, msg

        if resp.status_code in (401, 403):
            msg = ("DeepSeek API Key 无效或无权限（HTTP %d）。请检查 .env 里的 "
                   "DEEPSEEK_API_KEY，key 可在 https://platform.deepseek.com/api_keys 生成"
                   % resp.status_code)
            _note_error(msg)
            return False, msg

        if resp.status_code == 429:
            _HTTP_STATS["http_429"] += 1
        elif resp.status_code in _RETRY_STATUS:
            _HTTP_STATS["http_5xx"] += 1

        if resp.status_code in _RETRY_STATUS and attempt < attempts:
            _HTTP_STATS["backoffs"] += 1
            # 429 的 Retry-After 优先；没有就指数退避
            wait = backoff
            try:
                wait = max(wait, float(resp.headers.get("Retry-After") or 0))
            except (TypeError, ValueError):
                pass
            time.sleep(min(wait + random.uniform(0, 0.5), 20))
            backoff = min(backoff * 2, 20)
            continue

        detail = (resp.text or "")[:300]
        msg = f"接口返回 HTTP {resp.status_code}：{detail}"
        _note_error(msg)
        return False, msg

    msg = "重试次数用尽"
    _note_error(msg)
    return False, msg


def _pick_message(data):
    """从 chat/completions 的响应里取出 (正文, 思考内容)。"""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "", ""
    content = msg.get("content")
    if isinstance(content, list):
        # 少数实现会把 content 给成 [{"type":"text","text":...}]
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict))
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return (content or ""), (reasoning or "")


def chat(messages, kind="chat", max_tokens=None, temperature=0.0, model=None):
    """一次纯文本补全。返回 (正文, 思考内容)；失败返回 ("", "")。

    内部只有一层 `_chat_impl`，`call_json` 的重试会用它做"换成 flash"的第二次尝试。
    """
    return _chat_impl(messages, kind=kind, max_tokens=max_tokens,
                      temperature=temperature, model=model)


def _chat_impl(messages, kind="chat", max_tokens=None, temperature=0.0, model=None,
               force_no_thinking=False):
    """chat 的实现。force_no_thinking=True 时强制改用快速模型（不推理）。

    ## 为什么要做"模型级降级"

    `deepseek-v4-pro` 在"大输入 + 要严格 JSON"的场景里，思考可能吃掉大量
    max_tokens，导致正文/JSON 被截断。遇到这种情况，除了抬高额度，还可以
    退回 `deepseek-V4.1-flash` 再试一次：同样的提示词，flash 不产生推理内容，
    通常几秒就能返回干净 JSON。发生过的降级会记在 `_MODEL_FALLBACKS` 里，
    `/api/models` 能查到，不会让人以为配置的模型都生效了。
    """
    kind = kind if kind in PURPOSES else "chat"
    picked = model if is_selectable(model) else None
    model = picked or model_for(kind)
    if force_no_thinking and model == THINKING_MODEL:
        model = model_for("fast")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": min(MAX_TOKENS_CAP, int(max_tokens or _env_int("DEEPSEEK_MAX_TOKENS", 2048))),
        "stream": False,
    }
    ok, data = _post_chat(payload)
    # 用户在下拉列表里选的模型如果调不通（例如账号还没开通 pro）：
    # 不把 404 直接甩给用户，而是退回该用途的默认模型重试一次，
    # 并把这次降级记下来（/api/models 能看到），界面据此提示"已自动退回"。
    if not ok and picked and picked != model_for(kind):
        payload["model"] = model_for(kind)
        retry_ok, retry_data = _post_chat(payload)
        if retry_ok:
            _note_model_fallback(kind, picked, payload["model"])
            model = payload["model"]
            ok, data = retry_ok, retry_data
    text, reasoning = _pick_message(data) if ok else ("", "")

    # 推理把 max_tokens 吃光，导致 HTTP 200 但正文为空。
    # 这种情况不该判"模型失败"，而是改用快速模型、把额度给正文再试一次。
    if ok and not text.strip() and model == THINKING_MODEL:
        payload["model"] = model_for("fast")
        payload["max_tokens"] = min(MAX_TOKENS_CAP,
                                    max(int(payload["max_tokens"]), _env_int("DEEPSEEK_TEXT_FLOOR", 1024)))
        ok2, data2 = _post_chat(payload)
        if ok2:
            t2, r2 = _pick_message(data2)
            if t2.strip():
                _note_thinking_retry(kind, model)
                _note_model_fallback(kind, model, payload["model"])
                return t2, reasoning + r2
    if ok:
        return text, reasoning

    fallback = _fallback_model_for(kind, model)
    if fallback:
        payload["model"] = fallback
        ok, data = _post_chat(payload)
        if ok:
            _note_model_fallback(kind, model, fallback)
            return _pick_message(data)
    return "", ""


# 记录发生过哪些"用途模型 -> 快速模型"的降级（只留最近 20 条）
_MODEL_FALLBACKS = []
_FALLBACK_LOCK = threading.Lock()


def _fallback_model_for(kind, model):
    """该用途失败了，能不能退回快速模型（flash）再试一次？

    快速模型自己失败就不再退（退了也是同一个）；pro 失败时退到 flash。
    """
    fast_model = model_for("fast")
    if model == fast_model:
        return None
    return fast_model


def _note_model_fallback(kind, model, fallback):
    with _FALLBACK_LOCK:
        _MODEL_FALLBACKS.append({"kind": kind, "from": model, "to": fallback,
                                 "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        del _MODEL_FALLBACKS[:-20]


def model_fallbacks():
    with _FALLBACK_LOCK:
        return list(_MODEL_FALLBACKS)


# 「推理把额度吃光 -> 换 flash 重试」的计数（诊断用）
_THINKING_RETRIES = {"count": 0, "last": ""}


def _note_thinking_retry(kind, model):
    with _FALLBACK_LOCK:
        _THINKING_RETRIES["count"] += 1
        _THINKING_RETRIES["last"] = f"{kind}/{model} @ {time.strftime('%H:%M:%S')}"


def thinking_retries():
    with _FALLBACK_LOCK:
        return dict(_THINKING_RETRIES)


# ═══════════════════════════════════════════════════════════════
#  给工作流/科研团队用的两个高层入口
# ═══════════════════════════════════════════════════════════════

def call_text(prompt, user_content, kind="chat", max_tokens=2048,
              temperature=0.0, model=None):
    """system + user 一次补全，返回正文（失败返回空串）。"""
    return chat(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": user_content}],
        kind=kind, max_tokens=max_tokens, temperature=temperature, model=model,
    )[0]


def call_json(prompt, user_content, kind="fast", max_tokens=1200,
              temperature=0.0, retries=2, model=None):
    """要一段 JSON 回来。失败返回 None，由调用方决定降级。

    为什么要 retries：模型偶尔会回一段散文而不是 JSON（抽取这种要求格式的
    任务上大约 5% 一次不成）。重试一次的成本远低于整步失败。

    第二次重试会**换策略**：如果当前是 `deepseek-v4-pro`，改用 flash 并抬高额度。
    推理模型在"大输入 + 严格 JSON"场景里容易被推理内容截断，换 flash 通常能
    直接拿到干净 JSON。
    """
    for attempt in range(max(1, retries)):
        # 第一次按配置来；之后换策略：强制不推理 + 抬高额度。
        force_plain = attempt > 0
        text, _reasoning = _chat_impl(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": user_content}],
            kind=kind,
            max_tokens=max(max_tokens, _env_int("DEEPSEEK_JSON_RETRY_TOKENS", 2600))
            if force_plain else max_tokens,
            temperature=temperature, model=model, force_no_thinking=force_plain,
        )
        if not text:
            continue
        parsed = loads_json(text)
        if parsed is not None:
            if force_plain:
                _note_thinking_retry(kind, model)
            return parsed
    return None


# ═══════════════════════════════════════════════════════════════
#  给 LangChain agent 用的模型对象（要 tool calling / 真流式）
# ═══════════════════════════════════════════════════════════════

def chat_model(kind="chat", temperature=0.0, max_tokens=None, streaming=False,
               model=None, thinking=None):
    """构造一个 LangChain 聊天模型。

    DeepSeek 走 OpenAI 兼容协议，所以统一用 `langchain_openai.ChatOpenAI`
    指向 DeepSeek base_url：tool calling 与 `astream_events` 都是成熟的，
    不用自己实现 function call 解析。

    `thinking`：界面上那个「深度思考」开关。
      * None  -> 按环境变量/用途自动决定（`thinking_mode_for`）；
      * True  -> 改用 `deepseek-v4-pro`；
      * False -> 保留调用方选的模型。
    **不要给 DeepSeek 传 `thinking_mode`** —— 这不是它的参数。pro 会自己输出
    `reasoning_content`，且支持流式；所以这里不再有"开思考就关流式"的限制。
    """
    require_llm()
    kind = kind if kind in PURPOSES else "chat"
    # model 是界面上选的（可为 None）：白名单外的值一律回落到用途默认模型
    model = _resolve_model(model, kind)
    if thinking is None:
        think = thinking_mode_for(kind, model)
    else:
        think = bool(thinking)
    if think:
        model = THINKING_MODEL

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 langchain-openai（DeepSeek API 走 OpenAI 兼容协议）。"
            "请执行 pip install -r requirements.txt"
        ) from e

    kwargs = dict(
        model=api_model_for(model),
        api_key=api_key(),
        base_url=BASE_URL,
        temperature=temperature,
        timeout=TIMEOUT,
        max_retries=max(1, _env_int("DEEPSEEK_MAX_RETRIES", 4)),
        streaming=bool(streaming),
    )
    if max_tokens:
        kwargs["max_tokens"] = min(MAX_TOKENS_CAP, int(max_tokens))
    return ChatOpenAI(**kwargs)


# ═══════════════════════════════════════════════════════════════
#  自描述信息（/api/models、前端徽章、README 都对它取数）
# ═══════════════════════════════════════════════════════════════

def model_info():
    """当前模型栈的完整快照。**如实**标注 backend，不做美化。"""
    roles = []
    for kind, meta in PURPOSES.items():
        model = model_for(kind)
        roles.append({
            "kind": kind,
            "label": meta["label"],
            "why": meta["why"],
            "model": model,
            "api_model": api_model_for(model),
            "thinking": thinking_mode_for(kind, model),
            "supports_thinking": _supports_thinking(model),
        })
    bk = backend()
    return {
        "backend": bk or "unconfigured",
        "backend_label": {
            "deepseek": "DeepSeek（深度求索）",
            "unconfigured": "未配置",
        }[bk or "unconfigured"],
        "base_url": BASE_URL if bk else "",
        "configured": bool(bk),
        "roles": roles,
        "catalog": model_catalog(),
        "thinking_default": THINKING_SETTING,
        "rpm_limit": RPM,
        "max_context_note": "deepseek-v4-pro / deepseek-V4.1-flash 支持长上下文，长综述可直接喂全文摘要",
        "rate_limit_waits": _LIMITER.waits,
        "http_stats": http_stats(),
        "last_error": last_error(),
        # 发生过"用途模型 -> 快速模型"的降级（例如 pro 调不通或推理截断）
        "model_fallbacks": model_fallbacks(),
        # 发生过"推理吃光额度 -> 换 flash 重试"的次数
        "thinking_retries": thinking_retries(),
        "docs": "https://api-docs.deepseek.com/",
    }


def probe():
    """探活：向 DeepSeek `/models` 打一次，用来在界面上证明"确实连的是 DeepSeek"。"""
    if backend() != "deepseek":
        return {"ok": False, "reason": "当前未配置 DeepSeek API Key"}
    try:
        import requests
        resp = requests.get(BASE_URL + "/models",
                            headers={"Authorization": "Bearer " + api_key()},
                            timeout=min(TIMEOUT, 20))
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "body": (resp.text or "")[:200]}
        data = resp.json()
        ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
        return {"ok": True, "models": ids, "count": len(ids)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def check_models(max_tokens=8):
    """**按用途真打一次**，看这个账号实际能用哪些模型。

    为什么不能只看 `/models`：那个接口返回的是"账号可见的模型"，
    但实际调用还可能因为权限/额度/模型未开通而失败。所以这里对四个用途
    各发一条最小请求（max_tokens=8，成本可忽略），按 HTTP 成功与否判断，
    并直接给出该改哪一行环境变量。

    返回：
        {"ok": 全通/部分通, "backend", "results": [...], "usable": [...],
         "unusable": [...], "suggested_env": {环境变量: 值}, "note": "..."}
    """
    report = {"backend": backend(), "results": [], "usable": [], "unusable": [],
              "suggested_env": {}, "ok": False}
    if backend() != "deepseek":
        report["note"] = ("当前未配置 DeepSeek API Key（DEEPSEEK_API_KEY），"
                          "无法检查模型权限。")
        return report

    fast_model_name = model_for("fast")
    for kind, meta in PURPOSES.items():
        model = model_for(kind)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        t0 = time.time()
        ok, data = _post_chat(payload, timeout=min(TIMEOUT, 30))
        item = {"kind": kind, "label": meta["label"], "model": model,
                "ok": bool(ok), "elapsed_ms": round((time.time() - t0) * 1000, 1),
                "error": None if ok else str(data)[:200]}
        report["results"].append(item)
        (report["usable"] if ok else report["unusable"]).append(kind)
        if not ok and kind != "chat" and model != fast_model_name:
            # 不可用就建议指向快速模型（功能不残缺，只是推理弱一点）
            report["suggested_env"][meta["env"]] = fast_model_name

    report["ok"] = not report["unusable"]
    if report["suggested_env"]:
        report["note"] = ("以下用途的模型调不通（可能是账号未开通或额度不足）。"
                          "已在 .env.example 里写明改法：把这些用途指向快速模型即可，"
                          "功能不会缺失。")
    elif report["ok"]:
        report["note"] = "四个用途的模型都能调通。"
    return report


def reset_rate_limit_waits():
    """测试用：把限流计数清零，避免用例之间互相影响。"""
    _LIMITER.waits = 0


# 兼容旧代码里的名字
def chat_model_name(kind="chat"):
    return model_for(kind)


__all__ = [
    "BASE_URL", "PURPOSES", "MODEL_CATALOG", "SELECTABLE_MODELS", "model_catalog",
    "is_selectable", "available", "api_key", "api_model_for", "backend",
    "call_json", "call_text",
    "chat", "chat_model", "chat_model_name", "check_models", "last_error",
    "http_stats", "loads_json", "model_fallbacks", "model_for", "model_info", "probe",
    "require_llm", "reset_rate_limit_waits", "thinking_mode_for",
]
