"""模型提供商存储 —— **按账号隔离**（每家一条，各自一把 API Key）。

## 为什么要按账号存

原来 providers.json 是"整台机器一份"：同一个浏览器换个账号登录，
新账号照样能用上一个账号填的 Key（甚至能直接看到它的头尾）。
现在 Key 跟着账号走 ——

    accounts.json  存账号（用户名 / bcrypt 哈希）
    providers.json 存"每个账号自己的模型提供商与 Key"

    {
      "version": 2,
      "users": {
        "1":         {"active_id": "deepseek", "providers": [...]},
        "2":         {"active_id": null,       "providers": []},
        "__guest__": {"active_id": null,       "providers": []}
      }
    }

* 已登录：只用**本账号**的桶；桶里没 Key 就退回 `.env`（全站共用的站点 Key），
  **不会**去读别的账号、也不会读访客桶 —— 跨账号串用就是从这两条漏出来的。
* 未登录（访客）：用 `__guest__` 桶。换账号登录后自然看不到它。
* 老版本（version 1，整机一份）首次读取时会整体搬进 `__guest__`，
  不会丢配置；登录用户自己再加一份即可。

## 当前账号怎么传进来

`core/llm.py` 在请求线程里被调用，所以用 **thread-local** 记当前账号：
`web/app.py` 的 before_request 每请求 `set_current_user(...)` 一次。
CLI（Agent.py）不设置，等价于访客。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_PATH = os.path.join(_HERE, "providers.json")
_LOCK = threading.RLock()
_OVERRIDE = None
_TLS = threading.local()

VERSION = 2
GUEST = "__guest__"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


# ── 当前账号（请求线程级）────────────────────────────────────
def set_current_user(user_id):
    """由 web 层的 before_request 每请求调用；None 表示未登录（访客）。"""
    _TLS.uid = None if user_id in (None, "") else str(user_id)
    return _TLS.uid


def current_user():
    return getattr(_TLS, "uid", None)


def bucket_key(user_id="__auto__"):
    if user_id == "__auto__":
        user_id = current_user()
    return GUEST if user_id in (None, "") else str(user_id)


# ── 文件位置 ────────────────────────────────────────────────
def path() -> str:
    """优先级：set_path() > PROVIDERS_FILE > 测试临时目录 > 项目根目录。"""
    if _OVERRIDE:
        return _OVERRIDE
    env = (os.environ.get("PROVIDERS_FILE") or "").strip()
    if env:
        return env
    test_tmp = (os.environ.get("WF_TEST_TMP") or "").strip()
    if test_tmp:
        return os.path.join(test_tmp, "_test_providers.json")
    return _DEFAULT_PATH


def set_path(p):
    global _OVERRIDE
    _OVERRIDE = p
    return p


def reset_path():
    global _OVERRIDE
    _OVERRIDE = None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _empty():
    return {"version": VERSION, "users": {}}


def _migrate(data: dict) -> dict:
    """把 version 1（整机一份）搬进访客桶。"""
    if data.get("version") == VERSION and isinstance(data.get("users"), dict):
        return data
    old_providers = data.get("providers") if isinstance(data.get("providers"), list) else []
    return {
        "version": VERSION,
        "users": {
            GUEST: {
                "active_id": data.get("active_id"),
                "providers": [p for p in old_providers if isinstance(p, dict)],
            }
        } if old_providers else {},
    }


def load() -> dict:
    p = path()
    if not os.path.isfile(p):
        return _empty()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("结构不对")
        data = _migrate(data)
        # 丢掉结构不完整的条目，避免一条坏数据让整个列表渲染失败
        for bucket in data["users"].values():
            if isinstance(bucket, dict) and isinstance(bucket.get("providers"), list):
                bucket["providers"] = [x for x in bucket["providers"]
                                       if isinstance(x, dict) and x.get("id") and x.get("name")]
        return data
    except Exception:
        try:
            os.replace(p, p + ".broken")
        except Exception:
            pass
        return _empty()


def save(data: dict) -> str:
    p = path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%s.%s.tmp" % (p, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    return p


def _bucket(data: dict, user_id="__auto__") -> dict:
    key = bucket_key(user_id)
    users = data.setdefault("users", {})
    bucket = users.get(key)
    if not isinstance(bucket, dict):
        bucket = {"active_id": None, "providers": []}
        users[key] = bucket
    bucket.setdefault("active_id", None)
    bucket.setdefault("providers", [])
    return bucket


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "****"
    return f"{key[:6]}…{key[-4:]}"


def _slug(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "provider"
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _normalize_base(raw: str) -> str:
    """把地址收敛成可直接拼 `/chat/completions` 的形式。

    只在**末尾没有版本号**时补 `/v1`：各家并不都用 v1 ——
    例如 Z.ai 是 `.../coding/paas/v4`、智谱国内是 `.../api/coding/paas/v4`，
    无脑补 `/v1` 会变成 `.../v4/v1`，请求必然 404（这次同步预设时踩到过）。
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return DEFAULT_BASE_URL
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    if re.search(r"/v\d+$", base, re.I):      # 已经以 /v1、/v4 这类结尾
        return base
    return base + "/v1"


# ── 读 ──────────────────────────────────────────────────────
def list_public(user_id="__auto__") -> dict:
    """当前账号的提供商列表（Key 只留头尾）+ active 标记。"""
    data = load()
    bucket = _bucket(data, user_id)
    active = bucket.get("active_id")
    items = []
    for p in bucket["providers"]:
        key = (p.get("api_key") or "").strip()
        items.append({
            "id": p["id"],
            "name": p.get("name") or p["id"],
            "base_url": p.get("base_url") or DEFAULT_BASE_URL,
            "model": p.get("model") or "",
            "key_set": bool(key),
            "key_hint": mask_key(key),
            "active": p["id"] == active,
        })
    return {
        "active_id": active,
        "providers": items,
        "env_fallback": not any(i["key_set"] for i in items),
        "bucket": bucket_key(user_id),
        "logged_in": bucket_key(user_id) != GUEST,
    }


# ── 写 ──────────────────────────────────────────────────────
def upsert(payload: dict, user_id="__auto__") -> dict:
    """新增或修改**当前账号**的一条提供商。

    `api_key` 为空表示"沿用原来的"（编辑时不重填 Key 也能改名字/地址）。
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("请填写提供商名称")
    if len(name) > 40:
        raise ValueError("名称太长（最多 40 字）")

    data = load()
    bucket = _bucket(data, user_id)
    pid = (payload.get("id") or "").strip()
    existing_ids = {p["id"] for p in bucket["providers"]}
    if not pid:
        pid = _slug(name, existing_ids)

    target = next((p for p in bucket["providers"] if p["id"] == pid), None)
    key = (payload.get("api_key") or "").strip().strip('"').strip("'").strip()
    if key and (len(key) < 8 or any(c.isspace() for c in key)):
        raise ValueError("API Key 看起来不对（不能有空格，长度至少 8）")

    if target is None:
        target = {"id": pid, "created_at": _now(), "api_key": ""}
        bucket["providers"].append(target)
    if key:
        target["api_key"] = key
    target["name"] = name
    target["base_url"] = _normalize_base(payload.get("base_url") or "")
    model = (payload.get("model") or "").strip()
    if model:
        target["model"] = model
    target.setdefault("model", DEFAULT_MODEL)

    if not bucket.get("active_id") or not any(
            p["id"] == bucket["active_id"] for p in bucket["providers"]):
        bucket["active_id"] = pid

    save(data)
    return target


def delete(pid: str, user_id="__auto__") -> bool:
    data = load()
    bucket = _bucket(data, user_id)
    before = len(bucket["providers"])
    bucket["providers"] = [p for p in bucket["providers"] if p["id"] != pid]
    if len(bucket["providers"]) == before:
        return False
    if bucket.get("active_id") == pid:
        keyed = [p["id"] for p in bucket["providers"] if (p.get("api_key") or "").strip()]
        bucket["active_id"] = (keyed or [p["id"] for p in bucket["providers"]] or [None])[0]
    save(data)
    return True


def set_active(pid: str, user_id="__auto__") -> bool:
    data = load()
    bucket = _bucket(data, user_id)
    if not any(p["id"] == pid for p in bucket["providers"]):
        return False
    bucket["active_id"] = pid
    save(data)
    return True


def resolve(user_id="__auto__") -> dict:
    """当前账号生效的提供商。

    已登录只看自己的桶；访客只看访客桶；都没有 Key 时退回 `.env`。
    **绝不跨桶回退** —— 那正是"换账号还能用上一个账号 Key"的原因。
    """
    data = load()
    bucket = _bucket(data, user_id)
    active = next((p for p in bucket["providers"]
                   if p["id"] == bucket.get("active_id")), None)
    if active is None:
        active = next((p for p in bucket["providers"]
                       if (p.get("api_key") or "").strip()), None)
    if active is not None:
        key = (active.get("api_key") or "").strip()
        if key:
            return {
                "id": active["id"],
                "name": active.get("name") or active["id"],
                "base_url": _normalize_base(active.get("base_url") or ""),
                "api_key": key,
                "model": (active.get("model") or "").strip(),
                "source": "store",
            }

    env_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    env_base = (os.getenv("DEEPSEEK_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    return {
        "id": "env",
        "name": "DeepSeek（.env）",
        "base_url": _normalize_base(env_base),
        "api_key": env_key,
        "model": (os.getenv("DEEPSEEK_MODEL") or "").strip(),
        "source": "env",
    }


def summary(user_id="__auto__") -> dict:
    r = resolve(user_id)
    data = load()
    bucket = _bucket(data, user_id)
    configured = sum(1 for p in bucket["providers"] if (p.get("api_key") or "").strip())
    return {
        "source": r["source"],
        "name": r["name"],
        "key_set": bool(r["api_key"]),
        "key_hint": mask_key(r["api_key"]),
        "base_url": r["base_url"],
        "configured_count": configured,
        "total_count": len(bucket["providers"]),
        "bucket": bucket_key(user_id),
    }
