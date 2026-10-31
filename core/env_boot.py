"""统一加载项目根目录的 `.env` / `.env.local`（幂等，只做一次）。

配置加载的实际实现已经收进项目根的 `envconfig.py`（`.env` → `.env.local`，
系统环境变量优先，`.env` 缺失时照 `.env.example` 自动生成）。本模块保留为
一个薄适配层：`llm.py` / `providers` / `app.py` 等旧调用点仍然 import 它，
这样不用在十几个文件里各改一遍。


## 这个文件为什么存在（踩过的坑）

原来的 `.env` 加载写死在 `app.py` 与 `Agent.py` 里，于是：

    python -c "import llm; print(llm.check_models())"   # 文档里就是这么写的
    python -c "import providers; ..."                   # 直接查数据源

这两条命令**读不到 .env**，`llm.backend()` 会返回 `None`，
表现是"明明填了 token，却说自己没配置"。同理，任何新写的脚本、
被外部 harness 挂载的 `mcp_server.py`、临时排查用的一行 python，
都各自缺一遍加载逻辑 —— 这种"每个入口都要记得写一遍"的设计迟早出错。

所以收成一处：`load_project_env()`，由**读取环境变量的模块自己在 import 时调用**
（`llm.py` / `providers/__init__.py` / `app.py` / `Agent.py`）。

## 三个刻意的细节

1. **绝对路径**：按"当前工作目录向上找 .env"是错的 —— 从别的目录执行
   （`python C:\\...\\app.py`、IDE 里配了别的工作目录）就找不到，
   而且不报错，密钥悄悄回退。这里用 `__file__` 锚定本文件所在的目录。
2. **override=False**：测试/CI 里通常先 `os.environ["X"]="..."` 再 import，
   必须让已经存在的环境变量优先，否则 `.env` 会把它们盖掉，
   导致"测试用的假 key 被真 key 替换"这种危险情况。
3. **缺 python-dotenv 不报错**：依赖缺失时本轮启动仍应继续（app.py 有自动装依赖），
   这里静默跳过，不把一个可选依赖变成硬依赖。
"""

from __future__ import annotations

import os
import threading

_LOCK = threading.Lock()
_DONE = {"done": False, "loaded": None}


def env_path():
    """项目根目录 .env 的绝对路径（本文件在 core/，.env 在上一级）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".env")


def load_project_env(force=False):
    """加载 .env（只做一次）。返回实际加载的路径，没用 dotenv 时返回 None。"""
    if _DONE["done"] and not force:
        return _DONE["loaded"]
    with _LOCK:
        if _DONE["done"] and not force:
            return _DONE["loaded"]
        path = env_path()
        loaded = None
        try:
            # 优先走 envconfig：它同时处理 .env.local、系统环境变量优先级，
            # 以及"新 clone 没有 .env 时自动生成一份"。
            import envconfig
            envconfig.ensure_env_file()
            loaded_files = envconfig.load_env_files()
            loaded = loaded_files[-1] if loaded_files else None
            if loaded is None and os.path.isfile(path):
                loaded = path
        except Exception:  # noqa: BLE001 - 可选依赖缺失不该影响启动
            loaded = None
            try:
                from dotenv import load_dotenv
                if os.path.isfile(path):
                    load_dotenv(path, override=False)
                    loaded = path
            except Exception:  # noqa: BLE001
                pass
        _DONE["done"] = True
        _DONE["loaded"] = loaded
        return loaded


def has_env_file():
    return os.path.isfile(env_path())


__all__ = ["env_path", "has_env_file", "load_project_env"]
