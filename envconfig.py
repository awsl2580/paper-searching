"""环境变量加载与启动校验（所有入口共用）。

为什么单独抽一个模块：本项目有三个入口都要加载同一批配置
  * `app.py`      网页版
  * `Agent.py`    命令行版
  * `tests/live_*.py`  真机脚本
以前各自写各自的，结果长出了 bug —— `Agent.py` 用的是裸 `load_dotenv()`
（按**当前工作目录**找文件），换个目录启动就读不到；`app.py` 修过这个坑，
但没有同步到别处。统一到这里以后，所有入口的行为一定一致。

## 两层配置

| 文件 | 是否进版本库 | 放什么 |
|------|--------------|--------|
| `.env` | 进（团队共用） | 模型名、超时、会话密钥等**不含个人 API Key** 的配置 |
| `.env.local` | 不进（`.gitignore`） | 每个使用者自己的 `DEEPSEEK_API_KEY` |

加载顺序 `.env` → `.env.local`（后者覆盖前者），所以个人的 key 一定生效，
而且不会因为改了共用文件而影响别人。

系统环境变量的优先级最高（进程里已有的值不会被文件覆盖。
`load_dotenv` 默认就是"不覆盖已存在的环境变量"，`.env.local` 那层用
`override=True` 是为了压过 `.env`，但仍不会压过真正的系统环境变量）。
"""

from __future__ import annotations

import os

#: 项目根目录（本文件所在目录），所有配置文件都按它定位，不受启动目录影响。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_FILE = os.path.join(PROJECT_DIR, ".env")
ENV_LOCAL_FILE = os.path.join(PROJECT_DIR, ".env.local")


def load_env_files(base_dir: str | None = None) -> list[str]:
    """加载 `.env` 与 `.env.local`，返回实际读到的文件列表（便于排错）。

    用绝对路径，保证"从哪个目录启动"都不影响结果。
    """
    base = base_dir or PROJECT_DIR
    loaded: list[str] = []
    try:
        from dotenv import load_dotenv
    except Exception:          # 没装 python-dotenv 时不应阻断启动
        return loaded

    env_path = os.path.join(base, ".env")
    local_path = os.path.join(base, ".env.local")

    try:
        if load_dotenv(env_path):
            loaded.append(env_path)
        # override=True：让个人文件压过 .env 里的同名项（而不是"先读到的赢"）
        if load_dotenv(local_path, override=True):
            loaded.append(local_path)
    except Exception:
        pass
    return loaded


def api_key() -> str:
    """当前生效的 DeepSeek API Key（没有就返回空串）。"""
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


def require_api_key(component: str = "本项目") -> str:
    """确保 API Key 存在，否则抛出**可直接照做**的错误。

    Key 不再随仓库分发（`.env` 里是空的），所以这条提示必须能独立看懂：
    写哪个文件、写什么内容、去哪里申请。
    """
    key = api_key()
    if key:
        return key

    raise RuntimeError(
        f"{component} 需要 DEEPSEEK_API_KEY，但当前没有配置。\n"
        "\n"
        "API Key 不随仓库分发，需要你自己填一次。在下面这个文件里写一行即可\n"
        "（它已在 .gitignore 里，不会被提交）：\n"
        "\n"
        f"    {ENV_LOCAL_FILE}\n"
        "\n"
        "文件内容：\n"
        "\n"
        "    DEEPSEEK_API_KEY=sk-你的密钥\n"
        "\n"
        "密钥申请：https://platform.deepseek.com/api_keys\n"
        "\n"
        "也可以改用系统环境变量 DEEPSEEK_API_KEY（优先级高于上面的文件）。"
    )
