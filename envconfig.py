"""环境变量加载与启动校验（所有入口共用）。

为什么单独抽一个模块：本项目有三个入口都要加载同一批配置
  * `app.py`      网页版
  * `Agent.py`    命令行版
  * `tests/live_*.py`  真机脚本
以前各自写各自的，结果长出了 bug —— `Agent.py` 用的是裸 `load_dotenv()`
（按**当前工作目录**找文件），换个目录启动就读不到；`app.py` 修过这个坑，
但没有同步到别处。统一到这里以后，所有入口的行为一定一致。

## 配置文件怎么分工

| 文件 | 是否进版本库 | 放什么 |
|------|--------------|--------|
| `.env` | **不进**（`.gitignore`） | 你的 `DEEPSEEK_API_KEY`，以及模型名、超时等全部运行配置 |
| `.env.local` | 不进（`.gitignore`） | 可选：只想覆盖 `.env` 里某一项时用（优先级更高） |
| `.env.example` | 进（团队共用） | 模板 + 全部可配置项的说明，不含任何密钥 |

`.env` 不进版本库，所以新 clone 下来是没有这个文件的：`ensure_env_file()`
会照 `.env.example` 自动生成一份（会话密钥取随机值），网页右上角填了 Key
也会直接写进这个文件 —— 基本不需要手工复制模板。

加载顺序 `.env` → `.env.local`（后者覆盖前者）。
系统环境变量的优先级最高（加载完文件后会把进程里原有的值还原回去）。
"""

from __future__ import annotations

import os
import secrets

#: 项目根目录（本文件所在目录），所有配置文件都按它定位，不受启动目录影响。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_FILE = os.path.join(PROJECT_DIR, ".env")
ENV_LOCAL_FILE = os.path.join(PROJECT_DIR, ".env.local")
#: 生成 `.env` 的模板（同时是"全部可配置项"的说明文档）。
ENV_EXAMPLE_FILE = os.path.join(PROJECT_DIR, ".env.example")

#: `.env.example` 缺不到时的兜底内容（保证"没有模板也能跑起来"）。
_ENV_FALLBACK = """\
# 本文件由程序自动生成（.env 不进版本库，可放心写密钥）。
# 全部可配置项及含义见 .env.example。
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
API_TIMEOUT=30
FLASK_SECRET_KEY=__SECRET__
"""


def ensure_env_file() -> tuple[str, bool]:
    """确保 `.env` 存在，返回 `(路径, 是否新建)`。

    `.env` 现在**不进版本库**（里面会有 API Key），所以新 clone / 换台机器时
    它是没有的。而 `app.py` 启动要用 `FLASK_SECRET_KEY` 签登录态，缺了会直接
    拒绝启动 —— 那就变成"第一次运行先手工复制模板"。

    这一步把自动化做掉：照着 `.env.example` 生成一份，并把会话密钥换成
    **真随机值**（模板里是空的，空值启动会被 app.py 拒绝）。
    """
    if os.path.exists(ENV_FILE):
        return ENV_FILE, False

    template = _ENV_FALLBACK
    try:
        if os.path.exists(ENV_EXAMPLE_FILE):
            with open(ENV_EXAMPLE_FILE, "r", encoding="utf-8") as f:
                template = f.read()
    except OSError:
        pass

    # 会话密钥必须是随机的：模板里留空（或留着示例值）都会被启动检查拦下。
    secret = secrets.token_hex(32)
    lines, replaced = [], False
    for line in template.splitlines():
        if line.strip().startswith("FLASK_SECRET_KEY="):
            lines.append(f"FLASK_SECRET_KEY={secret}")
            replaced = True
        elif line.strip().startswith("DEEPSEEK_API_KEY=") and line.split("=", 1)[1].strip():
            # 模板里的占位文字（如 sk-你的密钥）不能当真的 Key 用
            lines.append("DEEPSEEK_API_KEY=")
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"FLASK_SECRET_KEY={secret}")

    content = "\n".join(lines).rstrip("\n") + "\n"
    if "__SECRET__" in content:      # 走的是兜底模板
        content = content.replace("__SECRET__", secret)
    else:
        # 复制模板时加一句说明：不然打开 .env 会看到"本文件是模板"的自述
        content = (
            "# 本文件由程序自动生成（照 .env.example 生成；.env 不进版本库）。\n"
            "# 可以直接在这里改配置、填 DEEPSEEK_API_KEY。\n"
            + content
        )

    try:
        with open(ENV_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError:
        # 目录只读（例如容器里挂的只读卷）：别把启动也拦下来，
        # 这种情况配置只能来自系统环境变量。
        return ENV_FILE, False
    return ENV_FILE, True


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

    # 先记下"进程里本来就有"的环境变量。
    # `.env.local` 用 override=True 是为了压过 `.env`，但按模块开头的约定，
    # 它**不该压过系统环境变量**；而 override=True 实际上会把系统变量一起盖掉。
    # 典型翻车：系统里设了 DEEPSEEK_API_KEY=sk-xxx，仓库自带的 `.env.local`
    # 里那行空的 `DEEPSEEK_API_KEY=` 会把它清成空串 —— 表现为"明明设了环境
    # 变量，程序却提示没有 Key"。所以在加载完文件后，把原本就存在的键还原。
    prior = dict(os.environ)

    try:
        if load_dotenv(env_path):
            loaded.append(env_path)
        # override=True：让个人文件压过 .env 里的同名项（而不是"先读到的赢"）
        if load_dotenv(local_path, override=True):
            loaded.append(local_path)
        for key, value in prior.items():
            if os.environ.get(key) != value:
                os.environ[key] = value
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
        "API Key 不随仓库分发，需要你自己填一次。三种方式任选：\n"
        "\n"
        "  ① 网页右上角点「🔑 填写 API Key」，粘进去保存即可（推荐，立刻生效）\n"
        f"  ② 在 {ENV_FILE} 里写一行（该文件不进版本库）：\n"
        "         DEEPSEEK_API_KEY=sk-你的密钥\n"
        "  ③ 设系统环境变量 DEEPSEEK_API_KEY（优先级最高）\n"
        "\n"
        "密钥申请：https://platform.deepseek.com/api_keys"
    )
