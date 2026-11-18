"""模型提供商存储（对应界面「设置 → 模型」里那一列）。

## 为什么要有这一层

原来站点只支持一把 DeepSeek Key（写死在 `.env` 的 `DEEPSEEK_API_KEY`）。
但 DeepSeek 走的是 OpenAI 兼容协议，同一套代码可以指向任何兼容服务
（DeepSeek 官方、各家中转、本地 vLLM/Ollama……）。把"提供商"做成一条条记录，
就能像常见客户端那样：**一家一个 Key，随时加、改、切换**，而不用改代码或重启。

## 存储

`providers.json`（与 accounts.json 同目录，**不进版本库** —— 里面是真 Key）：

    {
      "version": 1,
      "active_id": "deepseek",
      "providers": [
        {"id": "deepseek", "name": "DeepSeek",
         "base_url": "https://api.deepseek.com/v1",
         "api_key": "sk-...", "model": "deepseek-chat",
         "created_at": "2026-11-18 15:30:00"}
      ]
    }

## 兼容旧配置

文件不存在、或没有任何一条配置了 Key 时，`resolve()` 会退回 `.env` 的
`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` —— 之前填过 Key 的人不需要做任何迁移。
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

VERSION = 1
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


def path() -> str:
    """providers.json 的位置。

    优先级：set_path() > 环境变量 PROVIDERS_FILE > 测试临时目录 > 项目根目录。
    测试会把 PROVIDERS_FILE 指到临时目录，别污染真实配置。
    """
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
    return {"version": VERSION, "active_id": None, "providers": []}


def load() -> dict:
    """读文件；不存在/坏了返回空结构（坏文件改名留证，不抛异常）。"""
    p = path()
    if not os.path.isfile(p):
        return _empty()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
            raise ValueError("结构不对")
        data.setdefault("version", VERSION)
        data.setdefault("active_id", None)
        # 丢掉结构不完整的条目，避免一条坏数据让整个列表渲染失败
        data["providers"] = [x for x in data["providers"]
                             if isinstance(x, dict) and x.get("id") and x.get("name")]
        return data
    except Exception:
        try:
            os.replace(p, p + ".broken")
        except Exception:
            pass
        return _empty()


def save(data: dict) -> str:
    """原子写：先写临时文件再 replace，避免半截 JSON 把配置写坏。"""
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


def mask_key(key: str) -> str:
    """只回显头尾：够认出是哪一把，又不把整把 Key 送到前端。"""
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
    """收敛成 `.../v1`（去掉尾斜杠）。和 core/llm.py 的规则保持一致。"""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return DEFAULT_BASE_URL
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    if not base.endswith("/v1"):
        base = base + "/v1"
    return base


def list_public() -> dict:
    """给界面用的列表：Key 只留头尾，附带 active 标记。"""
    data = load()
    active = data.get("active_id")
    items = []
    for p in data["providers"]:
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
    return {"active_id": active, "providers": items,
            "env_fallback": not any(i["key_set"] for i in items)}


def upsert(payload: dict) -> dict:
    """新增或修改一条提供商。

    `api_key` 为空表示"沿用原来的"（编辑时不重新填 Key 也能改名字/地址）。
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("请填写提供商名称")
    if len(name) > 40:
        raise ValueError("名称太长（最多 40 字）")

    data = load()
    pid = (payload.get("id") or "").strip()
    existing_ids = {p["id"] for p in data["providers"]}
    if not pid:
        pid = _slug(name, existing_ids)

    target = next((p for p in data["providers"] if p["id"] == pid), None)
    key = (payload.get("api_key") or "").strip().strip('"').strip("'").strip()
    if key and (len(key) < 8 or any(c.isspace() for c in key)):
        raise ValueError("API Key 看起来不对（不能有空格，长度至少 8）")

    if target is None:
        target = {"id": pid, "created_at": _now(), "api_key": ""}
        data["providers"].append(target)
    if key:
        target["api_key"] = key
    target["name"] = name
    target["base_url"] = _normalize_base(payload.get("base_url") or "")
    model = (payload.get("model") or "").strip()
    if model:
        target["model"] = model
    target.setdefault("model", DEFAULT_MODEL)

    if not data.get("active_id") or not any(
            p["id"] == data["active_id"] for p in data["providers"]):
        data["active_id"] = pid

    save(data)
    return target


def delete(pid: str) -> bool:
    data = load()
    before = len(data["providers"])
    data["providers"] = [p for p in data["providers"] if p["id"] != pid]
    if len(data["providers"]) == before:
        return False
    if data.get("active_id") == pid:
        keyed = [p["id"] for p in data["providers"] if (p.get("api_key") or "").strip()]
        data["active_id"] = (keyed or [p["id"] for p in data["providers"]] or [None])[0]
    save(data)
    return True


def set_active(pid: str) -> bool:
    data = load()
    if not any(p["id"] == pid for p in data["providers"]):
        return False
    data["active_id"] = pid
    save(data)
    return True


def resolve() -> dict:
    """当前生效的提供商：{base_url, api_key, model, id, name, source}。

    没有任何可用配置时返回空 Key，由调用方决定怎么提示。
    """
    data = load()
    active = next((p for p in data["providers"]
                   if p["id"] == data.get("active_id")), None)
    if active is None:
        active = next((p for p in data["providers"]
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

    # 回退：沿用 .env 里的旧配置（老用户不需要迁移）
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


def summary() -> dict:
    """启动横幅/接口用：当前生效的是谁、配了几家。"""
    r = resolve()
    data = load()
    configured = sum(1 for p in data["providers"] if (p.get("api_key") or "").strip())
    return {
        "source": r["source"],
        "name": r["name"],
        "key_set": bool(r["api_key"]),
        "key_hint": mask_key(r["api_key"]),
        "base_url": r["base_url"],
        "configured_count": configured,
        "total_count": len(data["providers"]),
    }
