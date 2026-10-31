"""启动器（由 一键启动.bat 调用；双击 bat 即可，不用直接跑这个）。

它负责三件"需要中文提示"的事：

1. **安装依赖**：先看依赖是否已齐（齐了就跳过，第二次启动秒开）；
   不齐才装，默认源失败自动换清华镜像；全程显示进度，不会像"卡住"。
2. **生成 .env**：没有就复制模板、并自动填好 FLASK_SECRET_KEY，
   然后用记事本打开让你填 DeepSeek API Key。
3. **环境自检 + 启动服务**：调用 check_env.py 把问题说清楚，再拉起 web/app.py。

## 为什么中文提示不写在 .bat 里

Windows 的 cmd.exe 按**当前代码页**逐行解析批处理文件，UTF-8 的中文与制表符
会在 `chcp 65001` 生效之前就被切碎，报出一堆
`'────' 不是内部或外部命令`。而且 .bat 还要求 **CRLF** 行尾，否则解析错乱。
所以：**.bat 只保留纯 ASCII 的最小骨架**（找 Python、建 venv、把控制权交给这里），
所有中文交互都在 Python 里 —— Python 在 Windows 上默认用 UTF-8 写控制台，稳定。
"""

from __future__ import annotations

import importlib
import os
import re
import secrets
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
ENV_PATH = os.path.join(HERE, ".env")
EXAMPLE = os.path.join(HERE, ".env.example")
APP = os.path.join(HERE, "web", "app.py")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
BOOTSTRAP = os.path.join(HERE, "scripts", "bootstrap_env.py")

REQUIRED_MODULES = [
    ("flask", "flask"),
    ("langchain", "langchain"),
    ("langchain_openai", "langchain-openai"),
    ("langchain_core", "langchain-core"),
    ("requests", "requests"),
    ("dotenv", "python-dotenv"),
    ("bcrypt", "bcrypt"),
]

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def say(text=""):
    print(text, flush=True)


def missing_deps():
    out = []
    for mod, pkg in REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pkg)
    return out


def ensure_deps():
    """装依赖。返回 True 表示可用。"""
    lack = missing_deps()
    if not lack:
        say("✓ 依赖已就绪，跳过安装")
        return True

    say("正在安装依赖（缺 %d 个：%s）" % (len(lack), "、".join(lack)))
    say("首次大约 1-3 分钟，请耐心等待；下面会滚动显示 pip 的进度。")
    say("")
    cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS,
           "--disable-pip-version-check", "--no-input", "--prefer-binary"]
    if subprocess.call(cmd, cwd=HERE) == 0:
        return True

    say("")
    say("默认源安装失败，改用清华镜像重试…")
    if subprocess.call(cmd + ["-i", MIRROR], cwd=HERE) == 0:
        return True

    say("")
    say("❌ 依赖仍然装不上。常见原因：")
    say("   * 网络受限：手动执行下面这条（用镜像）")
    say('     "%s" -m pip install -r requirements.txt -i %s' % (sys.executable, MIRROR))
    say("   * Python 版本太新且某个包没有 wheel：换 3.12 / 3.13 重装后重试")
    say("   * 没有编译工具：本项目已尽量选纯 Python/带 wheel 的依赖，一般不需要编译器")
    return False


def ensure_env():
    """没有 .env 就生成并自动填好会话密钥，然后打开记事本让你填 token。

    返回 True 表示"刚生成，等用户填 token"，本次启动到此为止。
    """
    if os.path.isfile(ENV_PATH):
        return False
    if not os.path.isfile(EXAMPLE):
        say("❌ 找不到 .env.example，压缩包可能不完整。")
        return True

    text = open(EXAMPLE, encoding="utf-8").read()
    # 模板里 FLASK_SECRET_KEY 是空的，这里直接填随机值，省得用户再跑一条命令
    text = re.sub(r"(?m)^FLASK_SECRET_KEY=.*$",
                  "FLASK_SECRET_KEY=" + secrets.token_hex(32), text)
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    say("=" * 64)
    say("  第一次运行：已生成 .env（会话密钥已自动填好）")
    say("=" * 64)
    say("  还差一步 —— 填入 DeepSeek API Key：")
    say("")
    say("  1. 申请地址：https://platform.deepseek.com/api_keys")
    say("  2. 现在会自动用记事本打开 .env，把 key 粘到这一行后面：")
    say("         DEEPSEEK_API_KEY=你的key")
    say("     等号后直接粘，**不要加引号、不要加 Bearer、前后不要空格**")
    say("  3. 保存（Ctrl+S）并关掉记事本，然后**再次双击 一键启动.bat**")
    say("")
    try:
        subprocess.Popen(["notepad.exe", ENV_PATH])
    except Exception:
        say("（没能自动打开记事本，请手动编辑：%s）" % ENV_PATH)
    return True


def _running_in_project_venv():
    """当前解释器是不是项目里的 .venv？"""
    try:
        return (os.path.normcase(os.path.realpath(sys.prefix))
                == os.path.normcase(os.path.realpath(os.path.join(HERE, ".venv"))))
    except Exception:  # noqa: BLE001
        return False


def _restart_in_venv():
    """不在项目 .venv 里时：先用宿主 Python 准备 venv，再用 venv 重跑本脚本。

    这样即使直接 `python scripts/launch.py`（而不是双击 bat），也会走同一套
    「venv 跟随本机 Python」的逻辑，不会把依赖装进系统 Python。
    返回 None 表示已经/无需切换；返回整数表示切换后的退出码。
    """
    if _running_in_project_venv():
        return None
    boot = BOOTSTRAP
    if not os.path.isfile(boot):
        return None
    print("正在检查 .venv（自动匹配本机 Python）…", flush=True)
    rc = subprocess.call([sys.executable, boot], cwd=HERE)
    if rc != 0:
        return rc
    vpy = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    if not os.path.isfile(vpy):
        print("❌ .venv 准备后仍找不到解释器：%s" % vpy)
        return 1
    return subprocess.call([vpy, os.path.abspath(__file__)] + sys.argv[1:], cwd=HERE)


def main():
    rc = _restart_in_venv()
    if rc is not None:
        return rc
    say("=" * 64)
    say("  DeepSeek AI4S 科学智能体 · 启动器")
    say("  目录：%s" % HERE)
    say("=" * 64)
    say("")

    if not ensure_deps():
        return 1
    if ensure_env():
        return 0

    say("")
    say("正在自检环境…")
    try:
        import check_env
    except Exception as e:  # noqa: BLE001
        say("❌ 自检脚本 check_env.py 加载失败：%s: %s" % (type(e).__name__, e))
        say("   压缩包可能不完整，请重新解压一份。")
        return 1
    if check_env.main() != 0:
        say("")
        say("上面这些问题修好后，重新双击 一键启动.bat 即可。")
        return 1

    say("")
    say("正在启动服务…（关闭这个窗口或按 Ctrl+C 停止）")
    say("")
    try:
        return subprocess.call([sys.executable, APP], cwd=HERE)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
