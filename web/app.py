"""AI4S 科学智能体 —— Web 服务入口（DeepSeek 大模型 + MCP + 多智能体）

启动时自动检查并安装缺失依赖。
"""

import os
import sys
import traceback
import socket
import hmac
import uuid
import subprocess
import json
import re
import queue
import asyncio
import threading
import time
import mimetypes
from difflib import SequenceMatcher as _SequenceMatcher
from urllib.parse import quote

# ── 输出编码：别让一句 emoji 把整个启动搞崩 ──────────────────
# 现象：Windows 中文控制台是 GBK（cp936），把 stdout 重定向到文件/管道时
# Python 会用 gbk 编码，于是 print("✅ 数据库初始化完成") 直接抛
#   UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
# 而且它发生在 init_db() 里 —— 启动就死在第一屏，报错比"没装依赖"还难懂。
# 处理：只在**被重定向**（不是终端）时把输出切成 UTF-8，
# 直接开在控制台上时保持原样（控制台走 Windows 宽字符 API，本来就没问题）。
for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001 - 老版本/被包装过的流不支持就算了
        pass

# ── 目录整理后的路径兼容（必须先于项目内的 import）──────────────
# 源码按功能分到了 core/ web/ mcp/ cli/ scripts/ 等目录，但内部仍用
# `import llm` / `import providers` 这种扁平写法。项目根的 paths.py 会把这些
# 目录加进 sys.path；这里先把项目根插入，再 import paths。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import paths  # noqa: E402,F401  （导入即生效：把 core/ mcp/ science/ ... 加入 sys.path）

# ── Python 版本守卫 ──────────────────────────────────────
# 为什么放在最前面：依赖装不上时，pip 的报错（"Could not find a version..."）
# 对不熟悉的人几乎是天书。宁可自己先说清楚"需要 3.10+"，并给出下一步。
# 3.10 是本项目的下限（用了 X | Y 类型注解、match 之外的 3.10 特性）；
# 3.14 可以跑（37 个依赖在 3.14 上都有官方 wheel，见 scripts/check_deps_wheels.py）。
if sys.version_info < (3, 10):
    sys.stderr.write(
        "\n[需要更高版本的 Python]\n"
        "  本项目需要 Python 3.10 及以上，当前是 %d.%d.%d。\n"
        "  请安装 3.12 或 3.13（https://www.python.org/downloads/，"
        "安装时勾选 Add python.exe to PATH），\n"
        "  然后在项目目录重新执行：\n"
        "      python -m venv .venv\n"
        "      .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
        "      .venv\\Scripts\\python.exe web\\app.py\n"
        "  或者用系统 Python 直接运行：python web\\app.py（会自动切到 .venv）。\n\n"
        % (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    )
    raise SystemExit(2)

if sys.version_info >= (3, 14):
    print("[提示] 当前 Python %d.%d：依赖都有官方 wheel，可以正常跑；"
          "若某个包装不上，改用 3.12/3.13 最稳。"
          % (sys.version_info.major, sys.version_info.minor))

# ── 便携启动：用宿主 Python 直接跑 app.py 时，先把 .venv 对齐到本机 Python ──
# 为什么放在依赖检查之前：.venv 可能是从另一台机器拷来的（pyvenv.cfg 里的
# home 已经失效），这时 .venv\Scripts\python.exe 根本起不来，只有宿主 Python
# 能修它。修好后用 venv 的解释器重新执行本文件，后续依赖检查/启动都在 venv 里。
def _project_root():
    """项目根目录（本文件在 web/ 下，数据/静态资源/.venv 都在上一级）。"""
    return _ROOT


def _port_in_use(host, port):
    """端口是否已经有人在监听。

    为什么需要：Windows 上 Werkzeug 开了 SO_REUSEADDR，两个 `python web/app.py`
    可以同时绑 8000，于是两个服务共享 accounts.json / literature.db，
    `os.replace` 抢同一个临时文件会报 WinError 5 —— 表现就是登录/注册 500。
    这里在启动前先探测一次，宁可明确拒绝第二个实例。
    """
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    s = socket.socket()
    try:
        s.settimeout(0.5)
        return s.connect_ex((probe_host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _in_project_venv():
    try:
        venv = os.path.join(_project_root(), ".venv")
        return (os.path.normcase(os.path.realpath(sys.prefix))
                == os.path.normcase(os.path.realpath(venv)))
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__" and not _in_project_venv():
    _here = _project_root()
    _boot = os.path.join(_here, "scripts", "bootstrap_env.py")
    if os.path.isfile(_boot):
        print("正在检查 .venv（自动匹配本机 Python）…", flush=True)
        _rc = subprocess.call([sys.executable, _boot], cwd=_here)
        if _rc != 0:
            raise SystemExit(_rc)
        _vpy = os.path.join(_here, ".venv", "Scripts", "python.exe")
        if os.path.isfile(_vpy):
            raise SystemExit(subprocess.call(
                [_vpy, os.path.abspath(__file__)] + sys.argv[1:], cwd=_here))

# ── 自动检查并安装依赖 ────────────────────────────────────
REQUIRED_PACKAGES = {
    "flask": "flask>=3.0.0",
    "langchain": "langchain",
    # DeepSeek API 走 OpenAI 兼容协议，用 langchain-openai 接（tool calling / 流式都成熟）
    "langchain_openai": "langchain-openai",
    "langchain_core": "langchain-core",
    "requests": "requests>=2.28.0",
    "dotenv": "python-dotenv>=1.0.0",
    "bcrypt": "bcrypt>=4.0.0",
}
# 朗读（edge-tts）**不在必装列表里**：core/tts.py 会自己探测，缺了只是少个
# 朗读按钮，不应该让整个服务起不来。

# 国内网络下默认源经常超时；失败后自动再试一次镜像
_PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
_REQUIREMENTS = os.path.join(_ROOT, "requirements.txt")


def _missing_packages():
    """按模块名探测缺哪些依赖（比 pip list 快，也不受名字里的横线影响）。"""
    lack = []
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(module_name)
        except Exception:      # noqa: BLE001 - 缺包/装坏了都算缺
            lack.append(pip_name)
    return lack


missing = _missing_packages()

if missing:
    # 这里以前是 `pip install -q ... stdout=DEVNULL`：出问题时整段黑屏，
    # 用户只能盯着 "⏳ 正在自动安装..." 等，卡住了也不知道是慢还是死了；
    # 而且失败时 pip 的真实报错被丢进 DEVNULL，只剩一句"安装失败"。
    # 现在改成：说清楚要装什么、大概多久，pip 的输出直接滚在屏幕上。
    print("📦 缺少 %d 个依赖：%s" % (len(missing), ", ".join(missing)))
    print("⏳ 正在自动安装。首次通常 1–3 分钟（有本地缓存会更快），")
    print("   装完之后再启动就只需几秒；下面会滚动显示 pip 的进度。")
    print("   如果卡住不动，Ctrl+C 后手动执行：")
    print("       %s -m pip install -r requirements.txt" % sys.executable)
    print("", flush=True)

    # 没有 pip 的 venv（ensurepip 没跑成功）先补 pip，否则下面每条命令都会失败
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "--version"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:      # noqa: BLE001
        print("   （这个环境里没有 pip，先补一份）")
        try:
            subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"])
        except Exception:  # noqa: BLE001
            pass

    # --prefer-binary：宁可用旧一点的 wheel，也不要在 3.14 上现场编译；
    # --timeout/--retries：默认源连不上时别干等 5 轮重试，10 秒就换镜像。
    _pip_flags = ["--disable-pip-version-check", "--no-input", "--prefer-binary",
                  "--timeout", "15", "--retries", "1"]
    _attempts = []
    if os.path.isfile(_REQUIREMENTS):
        _attempts.append(("requirements.txt", [sys.executable, "-m", "pip", "install",
                                               "-r", _REQUIREMENTS] + _pip_flags))
        _attempts.append(("清华镜像", [sys.executable, "-m", "pip", "install",
                                       "-r", _REQUIREMENTS, "-i", _PIP_MIRROR] + _pip_flags))
    _attempts.append(("缺失包", [sys.executable, "-m", "pip", "install"] + missing + _pip_flags))
    _attempts.append(("缺失包 + 清华镜像", [sys.executable, "-m", "pip", "install", "-i",
                                            _PIP_MIRROR] + missing + _pip_flags))

    installed = False
    for i, (label, cmd) in enumerate(_attempts):
        if i:
            print("\n↻ 换一种方式重试（%s）…" % label, flush=True)
        try:
            if subprocess.call(cmd) == 0:
                installed = True
                break
        except Exception as e:      # noqa: BLE001
            print("   pip 起不来：%s: %s" % (type(e).__name__, e))

    if not installed:
        print("\n❌ 依赖没装上。常见原因与处理：")
        print("   · 网络受限 —— 用镜像手动装：")
        print("       %s -m pip install -r requirements.txt -i %s"
              % (sys.executable, _PIP_MIRROR))
        print("   · Python 太新、某个包还没有 wheel —— 换 3.12 / 3.13 重建 .venv")
        print("   · 权限不足 —— 别装到系统目录，确认用的是项目里的 .venv\\Scripts\\python.exe")
        sys.exit(1)

    # 装完重新探测一遍：pip 返回 0 但模块仍导不进来（装错解释器等）也要说清楚
    still = _missing_packages()
    if still:
        print("❌ pip 说装完了，但这些模块仍然导入失败：%s" % ", ".join(still))
        print("   多半是装到了别的 Python 上。当前解释器：%s" % sys.executable)
        sys.exit(1)
    print("✅ 依赖安装完成\n")

# ── 第三方导入 ────────────────────────────────────────────
import requests
from flask import (Flask, Response, jsonify, make_response, redirect, render_template_string,
                   request, session, stream_with_context, send_from_directory, g)
from functools import wraps

from langchain.agents import create_agent
from langchain_core.tools import tool

# 加载环境变量（.env → .env.local，后者覆盖前者）。
# 时间点很关键：必须在这之后的所有 `os.getenv(...)` 之前完成。
# 细节见 envconfig.py —— 网页版 / 命令行版 / 真机脚本共用它，
# 避免"从别的目录启动就读不到配置"这类坑各修各的。
import envconfig       # noqa: E402
# `.env` 不进版本库（里面会有 API Key），新 clone / 换台机器时它不存在。
# 先确保它被生成出来，否则下面读 FLASK_SECRET_KEY 会直接拒绝启动 ——
# 用户会卡在"连页面都打不开，更没法在页面上填 Key"。
envconfig.ensure_env_file()
envconfig.load_env_files()

TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
S2_USER_AGENT = os.getenv("S2_USER_AGENT", "LiteratureSearchAssistant/1.0 (personal research tool)")

# ── 模型：全部经 llm.py（DeepSeek 主链路）────────────────────
# 模型名不再散落在各处：
#   llm.model_for("chat")    对话主模型   deepseek-V4.1-flash（快速通用）
#   llm.model_for("fast")    轻量批处理   deepseek-V4.1-flash
#   llm.model_for("vision")  多模态读图   deepseek-V4.1-flash
#   llm.model_for("reason")  深度推理     deepseek-v4-pro（先推理再回答）
# 具体怎么选、深度思考怎么落到 pro、限流怎么控，见 llm.py 的模块说明。
import llm as llm_layer

# 导入数据库模块
import database as db

# 运行日志（管理员界面可查）。见 core/runlog.py：
# 只记"谁在什么时候做了什么、多大规模、哪一步失败"，不记问题原文/密码/token。
import runlog

# 朗读（TTS）。见 core/tts.py：走 edge-tts 的在线中文神经语音（晓晓 / 云希）。
# 没装依赖时这个模块自己会返回"不可用"，不会挡住启动 ——
# 朗读只是锦上添花，不能拖垮查文献。
import tts as tts_layer

# ── 静态文件：白名单，不做整目录托管 ──────────────────────
# 原来写的是 Flask(__name__, static_folder="static", static_url_path="")，
# 那会把整个 static/ 目录挂到根路径的通配路由 /<path:filename> 上——
# 目录里放什么就能下载什么。实测当时 static/app.py（一份 561 行的旧服务端
# 副本）可以直接 GET /app.py 拿到 200 + 21715 字节源码，app.js / styles.css
# 同理。根因是"整目录托管"这个配置本身：以后任何人往里丢一个 .py / .bak / .sql，
# 都会立刻变成公开可下载的文件。
#
# 前端是单文件应用（内联 CSS/JS，不引用任何外部资源），所以这里只放行
# 真正需要的文件。新增静态资源记得加进来，否则会被 404。
# 图标是同一个图形的三种尺寸（都从同一张 1024px 原图缩下来）：
# logo-owl-64.png 给顶栏标记、logo-owl-128.png 给登录页、favicon-owl-32.png 给标签页。
# welcome-owl.webp 是空对话页 / 登录页那张大插画（四边羽化透明，62 KB）。
STATIC_FILES = {"index.html", "favicon-owl-32.png", "logo-owl-64.png",
                "logo-owl-128.png", "welcome-owl.webp"}

# 这台机器的 mimetypes 不认识 .webp（Windows 的 MIME 注册表里没有这一条），
# 不登记的话 send_from_directory 会把插画发成 application/octet-stream ——
# 浏览器通常靠嗅探还能显示，但严格模式下会拒掉，所以这里显式登记。
mimetypes.add_type("image/webp", ".webp")
app = Flask(__name__, static_folder=None)

# ── 会话密钥：必须有，且不能是那个公开的兜底值 ─────────────
# 为什么这次改成"缺失就拒绝启动"：本站要挂到公网域名上。
# Flask 的登录态就是一枚用 secret_key 签名的 cookie，签名密钥一旦可猜，
# 任何人都能自己签一个 {"user_id": 1} 冒充别人登录 —— 别人的对话记录、
# 收藏就都能读。而原来的兜底值 "your-secret-key-change-this-in-production"
# 写在开源代码里，等于公开密钥。
#
# 本地跑不受影响：.env 里已经生成了一个随机值（见 .env.example 的说明）。
_SECRET_DEFAULT = "your-secret-key-change-this-in-production"
_secret = (os.getenv("FLASK_SECRET_KEY") or "").strip()
if not _secret or _secret == _SECRET_DEFAULT:
    raise RuntimeError(
        "FLASK_SECRET_KEY 未设置（或仍是示例里的默认值）。\n"
        "本站的登录态由它签名，用默认值等于任何人都能伪造登录。\n"
        "请在 .env 里设置一个随机值，例如：\n"
        "  python -c \"import secrets; print('FLASK_SECRET_KEY=' + secrets.token_hex(32))\"\n"
        "把输出那行加进 .env 即可。"
    )
app.secret_key = _secret

from datetime import timedelta  # noqa: E402


def _is_loopback_request():
    """这个请求是不是来自本机？（登录页"本机账号"提示的使用条件之一）"""
    addr = (request.remote_addr or "").strip()
    return addr in ("127.0.0.1", "::1", "localhost", "")

# ── 反向代理（natapp / ngrok 这类内网穿透）────────────────
# 穿透工具在中间做了一层代理：浏览器访问的是 https://xxx.natappfree.cc，
# 但它转发到本地是 http。不加这一层的话 Flask 以为自己是 http，
# 生成绝对 URL、判断 is_secure 都会出错（安全 cookie 就发不出去）。
# x_proto=1 表示只信一层代理的 X-Forwarded-Proto。
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

# ── 公网访问口令（可选，设了才生效）─────────────────────
# 为什么需要：本站在公网暴露的是**共享的 DeepSeek 额度**，
# 随手转发的链接就可能被爬虫或陌生人刷掉费用。
# 不设 PUBLIC_PASSWORD 就完全开放（本地开发/评委直连都方便）；
# 设了之后所有页面和接口都要求先过这一关。
PUBLIC_PASSWORD = (os.getenv("PUBLIC_PASSWORD") or "").strip()

# ── 登录态能保持多久 ──────────────────────────────────────────
# 默认 30 天：勾了「记住我」之后，关掉浏览器、重启服务再打开页面仍然是登录状态
# —— 账号本来就在 literature.db 里，没必要每次启动都重新登一遍。
# 想更短更安全就设 LOGIN_DAYS=1；设 0 表示"只在本次浏览器会话内有效"。
try:
    LOGIN_DAYS = max(0, int(os.getenv("LOGIN_DAYS") or "30"))
except ValueError:
    LOGIN_DAYS = 30

# 本机账号提示（登录页"本机已注册：xxx"）只对**本机回环请求**开放。
# 公网部署时把它关掉：用户名列表不该给路人看。
# ACCOUNT_HINT=1 强制开启，0 强制关闭，留空=只有 127.0.0.1 才开。
_ACCOUNT_HINT = (os.getenv("ACCOUNT_HINT") or "").strip().lower()

# 开发便利：设 DEV_AUTO_LOGIN=<用户名> 时，未登录的请求会自动以该账号登录
# （账号必须已存在）。只建议本机演示用，公网部署不要设。
DEV_AUTO_LOGIN = (os.getenv("DEV_AUTO_LOGIN") or "").strip()

# session 默认是"浏览器会话 cookie"：关掉浏览器就没了。设了 PERMANENT_SESSION_LIFETIME
# 之后，只要登录时写着 session.permanent = True，cookie 就会带 Expires，
# 关掉浏览器再打开仍然是登录状态 —— 这就是"下次运行不用重新登录"。
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=LOGIN_DAYS or 1),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def _account_hint_allowed():
    """登录页要不要列出"本机已注册的账号"。默认只对本机回环请求开放。"""
    if _ACCOUNT_HINT in ("1", "true", "yes", "on"):
        return True
    if _ACCOUNT_HINT in ("0", "false", "no", "off"):
        return False
    return _is_loopback_request()

# 放行清单：口令页本身、健康检查、站点图标。其余一律先过口令。
_GATE_EXEMPT = {"/gate", "/api/health", "/favicon-owl-32.png",
                "/logo-owl-64.png", "/logo-owl-128.png", "/welcome-owl.webp"}

_GATE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>学术检索 · AI 助手</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#f0f4f8;font-family:'Segoe UI','PingFang SC',Roboto,sans-serif}
 .box{background:#fff;border-radius:20px;padding:32px 28px;width:340px;
      box-shadow:0 16px 44px rgba(20,40,90,.14);text-align:center}
 h1{font-size:17px;color:#1a2a6c;margin:0 0 6px}
 p{font-size:13px;color:#8892a4;margin:0 0 18px;line-height:1.7}
 input{width:100%;box-sizing:border-box;padding:11px 14px;border:1px solid #dde1e8;
       border-radius:10px;font-size:14px;font-family:inherit;outline:none}
 input:focus{border-color:#4f7df3}
 button{width:100%;margin-top:12px;padding:11px;border:none;border-radius:10px;
        background:linear-gradient(135deg,#1a2a6c,#2d4373);color:#fff;font-size:14px;
        cursor:pointer;font-family:inherit}
 .err{margin-top:10px;font-size:12.5px;color:#c0392b;min-height:16px}
</style></head><body>
<div class="box">
  <h1>📚 学术检索 · AI 助手</h1>
  <p>本站部署在公网，请先输入访问口令</p>
  <form method="post" action="/gate">
    <input type="password" name="password" placeholder="访问口令" autofocus autocomplete="current-password">
    <button type="submit">进入</button>
  </form>
  <div class="err">__ERROR__</div>
</div></body></html>"""


@app.before_request
def _public_access_gate():
    """设了 PUBLIC_PASSWORD 才生效的门禁。

    用 cookie 里的一个签名标记记住"已通过"，而不是每次请求都带口令。
    标记内容就是 'ok'，靠 Flask 的会话签名保证不可伪造
    （所以上面那条"必须设置 FLASK_SECRET_KEY"是这套东西的前提）。

    实现上有个坑：POST /gate 必须由**路由**处理，不能在这里返回重定向 ——
    原来把表单提交也写在这个钩子里，而 Flask 在 before_request 返回非 None 之后
    仍会按 URL 规则匹配路由，/gate 只声明了 GET，于是 POST 变成 405。
    所以这里只负责"放行判断"，口令校验交给下面的 /gate 路由。
    """
    if not PUBLIC_PASSWORD:
        return None
    if request.path in _GATE_EXEMPT:
        return None
    if session.get("gate_ok"):
        return None
    return _GATE_PAGE.replace("__ERROR__", ""), 401


@app.route("/gate", methods=["GET", "POST"])
def gate():
    """口令页：GET 显示表单，POST 校验口令。"""
    if not PUBLIC_PASSWORD:
        return redirect("/")
    if request.method == "POST":
        # 直接比较口令。这里不做常数时间比较 ——
        # 这是一个共享口令而不是每人一个密码，收益极低。
        if (request.form.get("password") or "") == PUBLIC_PASSWORD:
            session["gate_ok"] = True
            return redirect("/")
        return _GATE_PAGE.replace("__ERROR__", "口令不正确"), 401
    return redirect("/")

# ── 运行日志：请求级埋点 ─────────────────────────────────────
# 目标：管理员界面上能看到"谁在什么时候调了什么接口、多慢、什么状态码"。
# 明确不记：请求体（聊天内容、密码、token 都在 body 里）、响应体。
_SKIP_LOG_PATHS = ("/static/", "/favicon", "/logo-", "/api/health")


@app.before_request
def _log_start():
    g._t0 = time.time()
    g._log_user = session.get("user_id") if "user_id" in session else None


@app.after_request
def _log_request(resp):
    try:
        path = request.path or ""
        if path.startswith(_SKIP_LOG_PATHS):
            return resp
        took_ms = (time.time() - getattr(g, "_t0", time.time())) * 1000
        status = resp.status_code
        level = "ERROR" if status >= 500 else ("WARN" if status >= 400 else "INFO")
        detail = {"status": status, "ms": round(took_ms)}
        if request.query_string:
            detail["query"] = request.query_string.decode("utf-8", "ignore")[:200]
        runlog.log(level, "request",
                   "%s %s → %d（%.0fms）" % (request.method, path, status, took_ms),
                   user_id=getattr(g, "_log_user", None), detail=detail)
    except Exception:  # noqa: BLE001 - 记日志永远不能影响响应
        pass
    return resp


@app.teardown_request
def _log_unhandled(exc):
    """未捕获异常单独记一条 ERROR。

    这种情况 after_request 可能来不及跑（或状态码已经定了），
    所以不指望它，直接在这里留痕，并带上异常类型与栈的最后几行。
    """
    if exc is None:
        return
    try:
        tb = traceback.format_exc().strip().splitlines()
        runlog.error("system",
                     "%s %s 未处理异常：%s" % (request.method, request.path, type(exc).__name__),
                     user_id=session.get("user_id") if "user_id" in session else None,
                     detail={"error": str(exc)[:300], "trace": tb[-6:]})
    except Exception:  # noqa: BLE001
        pass


@app.before_request
def _dev_auto_login():
    """DEV_AUTO_LOGIN=<用户名> 时，未登录的请求自动以该账号登录。

    场景：本机反复调界面/截图，每次启动都要登一遍很烦。
    两条安全约束：
      * 账号必须**已经存在**（不会凭空造号）；
      * 只对**本机回环请求**生效 —— 哪怕误设了环境变量，公网访问者也不会
        被"自动登录成某个账号"。
    默认不设这个变量，行为与以前完全一致。
    """
    if not DEV_AUTO_LOGIN:
        return None
    if 'user_id' in session:
        return None
    if not _is_loopback_request():
        return None
    try:
        user = db.get_user_by_username(DEV_AUTO_LOGIN)
    except Exception:  # noqa: BLE001 - 老库/未建表时安静跳过
        user = None
    if not user:
        return None
    session['user_id'] = user['id']
    session['username'] = user['username']
    session.permanent = LOGIN_DAYS > 0
    return None


# ── 登录验证装饰器 ────────────────────────────────────────
def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "请先登录"}), 401
        return f(*args, **kwargs)
    return decorated_function

# ── 文献数据源：多源聚合层 ────────────────────────────────
# 数据源不是单一的 Semantic Scholar，而是 providers/ 下的多源聚合：
#   semantic_scholar  英文，免费公共配额（高峰期会限流）
#   openalex          英+中，完全免费无需 key，中文靠 filter=language:zh
#   crossref          卷期页码最全，补 GB/T 7714
#   europepmc         生命科学，带被引次数
#   doaj              开放获取期刊
# 节流 / 指数退避 / 缓存 / 跨源去重都在 providers 里，命令行版 Agent.py 共用同一份。
# 换源或加源只需要新增 providers/xxx.py 并在注册表里加一行，本文件不用动。
import providers
import workflow as wf
import attachments
import agent_tools
import agents
import memory
import skills
import mcp_client
import mcp_server


DEFAULT_SYSTEM_PROMPT = """你是「DeepSeek AI4S 科学智能体」——一个能自己"搜、读、算、做、写"的科研助手。
你的底层模型是 DeepSeek 大模型（deepseek-V4.1-flash 负责快速对话、批量抽取与读图，
deepseek-v4-pro 负责深度推理），工具层通过 MCP 协议调用真实的科学计算程序。

【你能做什么】
你不只是"查论文"，你要把一次科学研究完整做完：
  搜（多源检索）→ 读（精读与图表理解）→ 想（提出可证伪假设）→
  做（实验/仿真方案）→ 算（真调计算工具）→ 验（复算与审稿）→ 写（综述与报告）

【你的工具】
- search_papers(query, limit, source, min_relevance)
    多源并发检索。source: all=中英文(默认) / cn=仅中文 / en=仅英文。
- get_paper_detail(paper_id)
    按 id（或标题）取完整摘要、被引、卷期页、全文链接。
- list_science_tools(category)
    列出可用的科学计算工具（统计检验 / 谱图解析 / 曲线拟合 / 分子描述符 / 实验设计）。
- run_science_tool(tool_name, arguments_json)
    通过 MCP 真的执行一个计算工具，拿到**真实计算结果**。
- start_research_workflow(goal)
    把"整篇研究报告"这类长任务移交给 AI4S 工作流引擎。
- remember_note(text, kind)
    把值得长期记住的结论写进研究记忆（登录后可用，跨会话可召回）。

【铁律：数值只能来自工具】
凡是统计量、p 值、拟合参数、峰位、信噪比、分子量、样本量，**必须**调 run_science_tool 算出来，
不许自己心算、不许"根据经验估计"、不许把记忆里上次的数值当成本次结果。
回答里每个数字都要能指回一次工具调用。工具返回 ok=false 时要说清失败原因（参数不合法/数据退化），
不要换个数字糊过去。

【检索与展示规则】
1. 用户问某个方向的论文时，先 search_papers。中文主题可先中文检索，必要时再补一次英文检索。
   **即使上一轮已经检索过同样的题目，这一轮也必须重新调用**（相同检索会命中本地缓存，
   不会真的重复请求数据源）。绝对不许把历史回答里的论文清单当成"这一轮的检索结果"复述：
   那样界面上不会出现论文卡片，用户只能看到一段纯文本（这是实测踩过的真实事故）。
2. 【重要】检索结果会由界面自动渲染成"论文卡片"（含作者、年份、期刊、被引、摘要、来源、
   原文链接、PDF 链接、收藏/详情按钮）。所以正文**不要重复这些字段**，只写两部分：
   (a) 开头一句话概括：主题、共几篇、覆盖的方向或年份跨度；
   (b) 一个「编号. 标题」的清单，每条一行。
   这份清单必须保留 —— 用户下一轮会说"看第 2 篇"，而上下文里只有你的正文。
3. 用户要"看第X篇详细信息"时，把该篇 id 传给 get_paper_detail，那次没有卡片，
   可以把完整摘要、被引、期刊、链接都写清楚。
4. 只有用户**明确索要链接**时才写 URL，并且只能照抄 url / openAccessPdf 字段，
   绝对不要自己拼造 DOI 或链接；字段为空就说"该源未提供"。
5. 结果是 {"error": "rate_limited"} 时如实转述 message/suggestion，可以重试一次
   （相同检索会命中本地缓存），不要连续重试超过两次。
6. warnings 非空（某源本次失败）时，用半句话带过，不要单独写数据源状态说明。
7. 不要编造检索结果里没有的论文、作者、引用数或链接。

【科学计算规则】
8. 用户给了谱图/曲线/实验数值/分子结构（或让你"算一下"）时：
   先判断数据类型 → list_science_tools 看有哪些工具 → run_science_tool 真算 → 报工具返回的数。
   - 谱图：先 spectra_smooth 再 spectra_peaks，报峰位/峰高/半高宽/面积/信噪比；
   - 两组数值比较：先 stats_describe，再按分布选 stats_ttest / stats_mannwhitney；
   - 三组以上：stats_anova；两变量关系：stats_correlation（相关不等于因果，说清）；
   - 分子结构：把结构式转成 SMILES，交给 molecule_parse 算分子式/分子量；
   - 要设计实验：design_factorial / design_lhs / design_power。
9. 汇报统计结论时给"检验名 + 统计量 + p + 效应量 + n"，并说明样本量小的时候
   "不显著"只代表证据不足，不代表没有差异。
10. 从图片里读出来的数值必须标注"读图估计值"，不要和工具算出来的值混在一起当测量值。

【整篇任务：交给工作流】
11. 用户要"写综述/做文献调研/研究现状/带参考文献的报告/系统梳理进展/做一次完整研究"时，
    不要自己一段段写，也不要连续调用十几次 search_papers 硬凑。改用 start_research_workflow：
    它会跑完 检索 → 精读 → 假设 → 实验设计 → 科学计算 → 复现验证（可迭代）→ 综述 → 报告，
    报告带 GB/T 7714 参考文献与复现清单，中途可中断、可从断点续跑。
    调用后告诉用户"已规划好研究方案，点按钮开始执行"，并简述规划重点，**不要假装报告已经写完**。
12. 绝对不要把工具的返回内容原样复制到正文里。工具返回的是给你看的结构化数据，
    用户看不懂。要用自己的话转述成中文。正文里出现 `{` `}` `"key":` 这类结构化痕迹就是错的。

【图片 / PDF】
13. 用户可能附上论文截图、参考文献页、实验照片、谱图、图表或 PDF（PDF 已在服务端渲染成图）。
    你自己能看（多模态），**不要回答"我无法查看图片"**。按三类处理：
    (a) 题录类（论文首页/参考文献页）：读标题、作者、期刊、DOI，然后**立刻调 search_papers**
        把论文找出来。没有 DOI 就只用标题+作者检索，不要编 DOI。多篇时挑最相关的先检索，
        并说明"图中共 N 篇，已检索前几篇"。
    (b) 科学数据类（谱图/曲线/数据表/结构式）：读出数值/结构 → 交给 run_science_tool 计算
        （谱图用 spectra_summary，结构式用 molecule_parse）。读不准就写"约"，并说明读图误差。
    (c) 实验现场类（照片/装置/现象）：客观描述所见（对象、条件、趋势、异常），
        再归纳 3-5 个检索关键词去检索同类研究。看不清就写"无法辨认"。
14. 涉及图片时，正文开头先用一句话说明你从图里读到了什么，让用户能校验。
    识别不确定要明说。用户只发图不说话时，按 (a)/(b)/(c) 自己判断并直接开始工作。

【长周期记忆】
15. 登录用户的研究记忆会以「【长周期研究记忆】」块出现在上下文里。它是**你过去的笔记，
    不是本次检索到的文献证据**：可以据此调整检索式、避免重复劳动、继承待解问题，
    但任何具体论断都必须以本轮检索到的文献为准，不得把记忆当引用来源。
16. 用户说"记住…"、或对话得出了可复用的结论/偏好/排除项时，调 remember_note。
    普通问答内容不要写进记忆（记忆库不是聊天记录的副本）。

【风格】
17. 中文回答，专业但平实。不确定就说不确定。不要用"大量研究表明"这类无指代表述。
"""



# ── 工具定义───────────────────────────────
# 工具定义已抽到 agent_tools.py：对话智能体、工作流的“计算员”、命令行版
# 共用同一批定义，避免“三个地方各写一遍参数校验”。这里只做引用，保留 app.工具名 这个路径。
search_papers = agent_tools.search_papers
get_paper_detail = agent_tools.get_paper_detail
list_science_tools = agent_tools.list_science_tools
run_science_tool = agent_tools.run_science_tool


def _make_research_tool(notify, user_id):
    """造一个"把整件事移交出去"的工具：模型负责规划，执行交给工作流引擎。

    为什么是每次请求现造，而不是像 search_papers 那样做成全局工具：
    工具跑在 agent 的工作线程里，要把"已规划好哪条工作流"告诉同一请求的
    SSE 生成器。用闭包捕获 notify 回调是最省事也最不会串线的做法 ——
    换成模块级变量，两个用户同时聊天就会互相覆盖。
    """

    @tool
    def start_research_workflow(goal: str) -> str:
        """把"写综述/做文献调研/出研究报告"这类整篇任务移交科研工作流引擎。

        适用：用户要一篇综述、研究现状、文献调研、带参考文献的报告。
        不适用：只查几篇文献（用 search_papers）、查某篇的详情（用 get_paper_detail）。

        参数:
            goal: 研究目标，用一句话写清楚研究对象和范围，
                  例如"图神经网络在遥感图像语义分割中的应用进展"。

        引擎会自动完成：检索筛选 -> 精读抽取 -> 科学假设 -> 实验设计 -> 科学计算
        -> 复现验证（可迭代） -> 综述撰写 -> 参考文献与复现清单，
        产出带 GB/T 7714 参考文献的 Markdown 报告，支持中断与断点续跑。
        调用后不要臆造综述内容，只需告诉用户方案已就绪、可以开始执行。
        """
        text = (goal or "").strip()
        if len(text) < 4:
            return json.dumps({"error": "goal_too_short",
                               "message": "研究目标太短，请补充研究对象与范围"},
                              ensure_ascii=False)
        try:
            # 长周期记忆参与规划：同一课题第二次做时，检索式可以继承上次的
            # 有效术语、避开上次明确排除的方向。取不到记忆就当空。
            hint = ""
            try:
                if user_id:
                    hint = memory.context_pack(user_id, text, limit=5,
                                               budget_chars=700)
            except Exception:  # noqa: BLE001
                hint = ""
            plan = wf.plan_workflow(text, memory_hint=hint) if hint else wf.plan_workflow(text)
            workflow_id = db.create_workflow(user_id, text, plan) if user_id else None
        except Exception as e:  # noqa: BLE001 - 规划失败不能让整轮对话崩掉
            return json.dumps({"error": "plan_failed", "message": f"{type(e).__name__}: {e}"},
                              ensure_ascii=False)

        notify({"workflow_id": workflow_id, "goal": text, "plan": plan,
                "memory_used": bool(hint)})
        steps = "、".join(s["title"] for s in wf.STEPS)
        # 返回**一段自然语言**，不要用 "字段：值" 的清单，更不要用 JSON。
        # 实测教训：返回 JSON 时，模型会把 JSON 原样答复吐出来；
        # 改成 "字段：值" 的清单后，模型照样照着清单拼了一个 JSON 对象
        # （正文里出现 {"topic":"...","query":"...","sections":[...]}）。
        # 模型会模仿工具返回的形状，所以这里就给成散文。
        return (
            f"已为用户规划好一条 AI4S 科研工作流（编号 {workflow_id}），主题是"
            f"「{plan.get('topic') or text}」，准备用检索式「{plan.get('query')}」"
            f"去查{plan.get('source')}范围的文献，重点关注{plan.get('focus')}。"
            f"执行流程依次是{steps}；其中假设—实验—计算—验证是一个可迭代的闭环，"
            "验证不通过会自动返工重做。"
            + ("这次规划参考了用户以往的研究记忆。" if hint else "")
            + "界面上的「开始执行」按钮已经就绪，点击后开始跑，"
            "中途可以随时中断，之后能从断点继续。"
            "请用简体中文、以自然的段落转述这些要点，告诉用户方案已就绪；"
            "不要编造综述内容，也不要用花括号或引号列出字段。"
        )

    return start_research_workflow


# ═══════════════════════════════════════════════════════════════
#  路由：静态页面（白名单见 STATIC_FILES）
# ═══════════════════════════════════════════════════════════════

STATIC_DIR = os.path.join(_ROOT, "static")


@app.get("/")
def index():
    """首页。

    **必须禁缓存**：前端就是这一个 index.html，改完代码后浏览器如果还用旧副本，
    就会出现"新 JS 配旧 HTML"的混合状态 —— 表现是点功能没反应、面板一片空白、
    或者报 `xxx is not defined` 却怎么刷新都不好。这类问题排查起来极费时间
    （本项目已经踩过：删掉登录浮层后，页面还在调 toggleAuthDropdown）。
    所以首页一律 no-store，静态资源走强校验头。
    """
    resp = make_response(send_from_directory(STATIC_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/<path:filename>")
def static_whitelist(filename):
    """只放行白名单里的静态文件；其余（例如误放进去的 .py/.bak/.db）一律 404。

    send_from_directory 自身会拒绝越权路径，这里的成员判断是第二道闸。
    """
    if filename not in STATIC_FILES:
        return jsonify({"error": "not found"}), 404
    resp = make_response(send_from_directory(STATIC_DIR, filename))
    if filename.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    else:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ═══════════════════════════════════════════════════════════════
#  路由：用户认证
# ═══════════════════════════════════════════════════════════════

@app.post("/api/register")
def register():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    email = data.get('email', '').strip()
    # 注册时可以顺手传一个头像（前端已压到 128×128），也可以不传
    ok_avatar, avatar, avatar_err = _clean_avatar(data.get('avatar'))

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if not ok_avatar:
        return jsonify({"error": avatar_err}), 400
    if len(username) < 3:
        return jsonify({"error": "用户名至少3个字符"}), 400
    if len(password) < 8 or len(password) > 16:
        return jsonify({"error": "密码需为8-16位"}), 400
    if not re.search(r'[a-z]', password) or not re.search(r'[A-Z]', password) or not re.search(r'\d', password):
        return jsonify({"error": "密码需包含大写字母、小写字母和数字"}), 400

    success, message = db.register_user(username, password, email)
    if not success:
        runlog.warn("auth", "注册失败：%s" % username, detail={"reason": message[:80]})
        return jsonify({"error": message}), 400
    if avatar:
        # 注册时就带头像：先建用户再写头像（user id 这时候才有）
        try:
            new_user = db.get_user_by_username(username)
            if new_user:
                db.update_avatar(new_user["id"], avatar)
        except Exception:  # noqa: BLE001 - 头像失败不该导致注册失败
            pass

    # 注册成功就直接把我们登录：用户不用再走一遍"注册完再登录"。
    user = db.login_user(username, password)
    if user:
        session['user_id'] = user['id']
        session['username'] = user['username']
        session.permanent = bool(data.get('remember', True)) and LOGIN_DAYS > 0
        full = db.get_user_by_id(user['id']) or {}
        return jsonify({
            "success": True,
            "message": "注册成功，已自动登录",
            "user": {"id": user['id'], "username": user['username'],
                     "avatar": full.get("avatar") or ""},
            "expires_days": LOGIN_DAYS if session.permanent else 0,
        })
    # 极端情况（写库成功但登录失败）：仍告诉用户注册成功，让他手动登录
    return jsonify({"success": True, "message": "注册成功，请登录"})


@app.post("/api/login")
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    remember = bool(data.get('remember', True))

    user = db.login_user(username, password)
    if user:
        session['user_id'] = user['id']
        session['username'] = user['username']
        # 勾了"记住我" -> 持久 cookie（LOGIN_DAYS 天）；否则只在本次浏览器会话内有效
        session.permanent = remember and LOGIN_DAYS > 0
        full = db.get_user_by_id(user['id']) or {}
        runlog.info("auth", "登录成功：%s" % user['username'], user_id=user['id'])
        return jsonify({
            "success": True,
            "user": {"id": user['id'], "username": user['username'],
                     "avatar": full.get("avatar") or ""},
            "expires_days": LOGIN_DAYS if session.permanent else 0,
        })

    # 登录失败：本机访问时给出**具体原因**（是没这个账号还是密码错），
    # 公网访问时保持笼统提示，避免被用来枚举用户名。
    runlog.warn("auth", "登录失败：%s" % username,
                detail={"loopback": _is_loopback_request()})
    if _is_loopback_request():
        if not db.user_exists(username):
            accounts = [a["username"] for a in db.list_users(5)]
            return jsonify({
                "error": f"本机没有「{username}」这个账号",
                "hint": ("本机已注册：" + "、".join(accounts)) if accounts
                        else "本机还没有任何账号，先点下面的「注册」建一个",
                "action": "register",
            }), 401
        return jsonify({
            "error": "密码不正确（账号存在）",
            "hint": "忘了密码的话，换个用户名重新注册一个即可",
        }), 401
    return jsonify({"error": "用户名或密码错误"}), 401


# ═══════════════════════════════════════════════════════════════
#  管理员页面（隐藏路由：只有 /admin/ 能进）
# ═══════════════════════════════════════════════════════════════
#
# 需求：
#   * 不在任何界面入口里放链接，只能手动在首页地址后加 admin/ 访问；
#   * 管理员账号/密码默认都是 admin（可用 ADMIN_USER / ADMIN_PASSWORD 覆盖）；
#   * 普通登录用户访问 /admin/ 直接弹回首页；
#   * 当前只做"查看用户"，不提供删除/改密等危险操作。
#
# 为什么用独立的 session["admin_ok"]：管理员不是 accounts.json 里的普通账号，
# 不能把 admin 塞进用户表，否则会出现在用户自己的列表里、也会被普通登录接口当成普通账号。
ADMIN_USER = (os.getenv("ADMIN_USER") or "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or "admin"

_ADMIN_CSS = """
:root { --navy:#1a2a6c; --blue:#4f7df3; --paper:#f5f8fd; --ink:#1f2d45; --muted:#7b8798; --line:#e3eaf6; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; font-family:'Segoe UI','PingFang SC',Roboto,'Helvetica Neue',sans-serif;
       color:var(--ink); background:radial-gradient(1200px 600px at 10% 0%, #e8efff 0%, var(--paper) 55%, #f8fafc 100%); }
.shell { max-width:980px; margin:48px auto; padding:0 20px; }
.brand { display:flex; align-items:center; gap:10px; font-size:20px; font-weight:800; color:var(--navy); }
.badge { font-size:10px; letter-spacing:.8px; color:var(--blue); background:#eef3ff;
         border:1px solid #dbe6ff; border-radius:999px; padding:2px 7px; font-weight:700; }
.sub { font-size:13px; color:var(--muted); margin-top:5px; }
.top { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:18px; }
.card { background:#fff; border:1px solid var(--line); border-radius:18px;
        box-shadow:0 18px 50px rgba(20,40,90,.10); padding:26px; }
.login-card { max-width:430px; margin:40px auto; }
h1 { font-size:22px; margin:0 0 6px; color:var(--navy); }
label { display:block; font-size:13px; color:#4a5568; margin:16px 0 6px; }
input { width:100%; padding:11px 13px; border:1px solid #dde3ee; border-radius:10px;
        font-size:14px; font-family:inherit; outline:none; }
input:focus { border-color:var(--blue); box-shadow:0 0 0 3px rgba(79,125,243,.12); }
button { padding:11px 16px; border:none; border-radius:10px; color:#fff; font-size:14px;
         font-family:inherit; cursor:pointer; background:linear-gradient(135deg,var(--navy),#2d4373); }
button:hover { filter:brightness(1.06); }
.login-btn { width:100%; margin-top:20px; }
.error { background:#fff1f0; border:1px solid #ffd6d2; color:#b42318; border-radius:10px;
         padding:10px 12px; font-size:13px; margin-top:14px; }
.back { display:inline-block; margin-top:18px; color:var(--muted); font-size:13px; text-decoration:none; }
.back:hover { color:var(--blue); }
.stats { display:flex; gap:28px; padding:6px 2px 18px; border-bottom:1px solid #eef2f7; margin-bottom:6px; }
.stats b { display:block; font-size:28px; color:var(--navy); line-height:1.1; }
.stats span { font-size:12px; color:var(--muted); }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:12px 10px; border-bottom:1px solid #eef2f7; font-size:13.5px; vertical-align:middle; }
th { font-size:12px; color:#7b8798; font-weight:600; letter-spacing:.3px; }
.user { display:flex; align-items:center; gap:9px; }
.avatar { width:28px; height:28px; border-radius:50%; display:inline-flex; align-items:center;
          justify-content:center; background:linear-gradient(135deg,#1a2a6c,#4f7df3);
          color:#fff; font-size:12px; font-weight:700; }
.empty { text-align:center; color:var(--muted); padding:28px; }
.foot { margin-top:18px; font-size:12px; color:var(--muted); display:flex;
        justify-content:space-between; gap:12px; flex-wrap:wrap; }
.ghost { background:#fff; color:#5b6577; border:1px solid #dde3ee; }
.ghost:hover { filter:none; background:#f2f6ff; color:#1f2d45; }
/* ── 标签页 + 日志视图 ─────────────────────────────────────── */
.tabs { display:flex; gap:6px; margin:0 0 16px; }
.tabs a { padding:8px 14px; border-radius:10px; border:1px solid var(--line); background:#fff;
          color:#5b6577; font-size:13px; text-decoration:none; font-weight:600; }
.tabs a:hover { background:#f2f6ff; color:#1f2d45; }
.tabs a.active { background:linear-gradient(135deg,var(--navy),#2d4373); color:#fff; border-color:transparent; }
.filters { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:4px 0 14px; }
.filters select, .filters button, .filters .reset, .filters .auto { flex:0 0 auto; }
.filters select, .filters input[type=text] {
    padding:8px 10px; border:1px solid #dde3ee; border-radius:9px; font-size:13px;
    font-family:inherit; background:#fff; color:var(--ink); outline:none; }
.filters select:focus, .filters input[type=text]:focus { border-color:var(--blue); }
/* 关键词框吃掉剩余宽度，但先于按钮收缩 —— 否则按钮/开关会被挤成竖排 */
.filters input[type=text] { flex:1 1 200px; min-width:170px; }
.filters button { padding:8px 14px; font-size:13px; }
.filters .reset { font-size:13px; color:var(--muted); text-decoration:none; padding:8px 4px; }
.filters .reset:hover { color:var(--blue); }
.auto { display:flex; align-items:center; gap:6px; font-size:12.5px; color:var(--muted);
        white-space:nowrap; }
.auto input { margin:0; }
.log-table th { white-space:nowrap; }
.log-table td { vertical-align:top; }
.log-ts { color:#7b8798; white-space:nowrap; font-family:Consolas,Menlo,monospace;
          font-size:12.5px; font-variant-numeric:tabular-nums; }
.lv { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px;
      font-weight:700; letter-spacing:.3px; }
.lv-INFO { background:#eef3ff; color:#2b4acb; }
.lv-WARN { background:#fff6e5; color:#a8620a; }
.lv-ERROR { background:#ffecec; color:#b42318; }
.lv-DEBUG { background:#f1f3f6; color:#6b7480; }
.log-msg { line-height:1.6; word-break:break-word; }
.log-detail summary { cursor:pointer; color:#8794a8; font-size:12px; margin-top:4px; }
.log-detail pre { margin:6px 0 0; padding:8px 10px; background:#f6f8fc; border:1px solid var(--line);
                  border-radius:8px; font-size:11.5px; color:#4a5568; white-space:pre-wrap;
                  max-height:200px; overflow:auto; }
.pager { display:flex; gap:10px; align-items:center; margin-top:14px; font-size:13px; color:#5b6577; }
.pager a { padding:6px 12px; border-radius:9px; border:1px solid var(--line);
           background:#fff; color:#2b3a55; text-decoration:none; font-size:13px; }
.pager a:hover { background:#f2f6ff; }
.pager a.disabled { opacity:.35; pointer-events:none; }
.danger { background:#fff1f0; color:#b42318; border:1px solid #ffd6d2; }
.danger:hover { background:#ffe3e0; filter:none; }
.hint { font-size:12px; color:var(--muted); font-weight:400; }
@keyframes logIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:none; } }
.log-table tbody tr { animation:logIn .22s ease-out both; }
@media (max-width:640px) {
  .card { padding:18px; }
  th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4) { display:none; }
}
"""

_ADMIN_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>管理员登录 · DeepSeek AI4S</title>
<style>""" + _ADMIN_CSS + """</style>
</head><body>
<div class="shell">
  <div class="brand">🔐 DeepSeek AI4S <span class="badge">ADMIN</span></div>
  <div class="card login-card">
    <h1>管理员登录</h1>
    <p class="sub">此页面不在任何入口中展示，仅管理员可直接访问。</p>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="post" action="/admin/login">
      <label for="adminUser">账号</label>
      <input id="adminUser" name="username" autocomplete="username"
             value="{{ username or '' }}" autofocus>
      <label for="adminPass">密码</label>
      <input id="adminPass" type="password" name="password" autocomplete="current-password">
      <button class="login-btn" type="submit">进入控制台</button>
    </form>
    <a class="back" href="/">返回首页</a>
  </div>
</div>
</body></html>"""

_ADMIN_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>管理员控制台 · DeepSeek AI4S</title>
<style>""" + _ADMIN_CSS + """</style>
</head><body>
<div class="shell">
  <div class="top">
    <div>
      <div class="brand">🔐 管理员控制台 <span class="badge">ADMIN</span></div>
      <div class="sub">DeepSeek AI4S · {{ '运行日志' if tab == 'logs' else '用户总览' }}</div>
    </div>
    <form method="post" action="/admin/logout">
      <button class="ghost" type="submit">退出管理员</button>
    </form>
  </div>

  <div class="tabs">
    <a href="/admin/" class="{{ 'active' if tab != 'logs' else '' }}">用户总览</a>
    <a href="/admin/?tab=logs" class="{{ 'active' if tab == 'logs' else '' }}">运行日志</a>
  </div>

  {% if tab == 'logs' %}
  <div class="card">
    <div class="stats">
      <div><b>{{ log_stats.total or 0 }}</b><span>日志总数</span></div>
      <div><b>{{ log_stats.last24h or 0 }}</b><span>近 24 小时</span></div>
      <div><b>{{ err_count }}</b><span>ERROR</span></div>
      <div><b>{{ warn_count }}</b><span>WARN</span></div>
    </div>

    <form class="filters" method="get" action="/admin/">
      <input type="hidden" name="tab" value="logs">
      <select name="level" aria-label="级别">
        <option value="">全部级别</option>
        {% for lv in levels %}<option value="{{ lv }}"{% if lv == f_level %} selected{% endif %}>{{ lv }}</option>{% endfor %}
      </select>
      <select name="module" aria-label="模块">
        <option value="">全部模块</option>
        {% for md in modules %}<option value="{{ md }}"{% if md == f_module %} selected{% endif %}>{{ md }}</option>{% endfor %}
      </select>
      <input type="text" name="q" value="{{ f_q }}" placeholder="关键词（消息 / 详情 / IP）" aria-label="关键词">
      <button type="submit">筛选</button>
      <a class="reset" href="/admin/?tab=logs">重置</a>
      <label class="auto"><input type="checkbox" id="autoRefresh"> 自动刷新（5 秒）</label>
      <button type="button" class="danger" onclick="clearLogs()">清空日志</button>
    </form>

    <table class="log-table">
      <thead><tr>
        <th style="width:160px">时间</th><th style="width:72px">级别</th>
        <th style="width:92px">模块</th><th>内容</th><th style="width:130px">IP</th>
      </tr></thead>
      <tbody id="logBody">
      {% for r in logs.rows %}
        <tr>
          <td class="log-ts">{{ r.ts }}</td>
          <td><span class="lv lv-{{ r.level }}">{{ r.level }}</span></td>
          <td>{{ r.module }}</td>
          <td class="log-msg">{{ r.message }}
            {% if r.user_id %}<span class="hint">（用户 #{{ r.user_id }}）</span>{% endif %}
            {% if r.detail %}<details class="log-detail"><summary>详情</summary><pre>{{ r.detail }}</pre></details>{% endif %}
          </td>
          <td class="hint">{{ r.ip or '—' }}</td>
        </tr>
      {% endfor %}
      {% if not logs.rows %}
        <tr><td colspan="5" class="empty">没有符合条件的日志</td></tr>
      {% endif %}
      </tbody>
    </table>

    <div class="pager">
      <a class="{{ 'disabled' if logs.page <= 1 else '' }}" href="{{ prev_url }}">上一页</a>
      <span id="pagerInfo">第 {{ logs.page }} / {{ logs.pages }} 页 · 共 {{ logs.total }} 条</span>
      <a class="{{ 'disabled' if logs.page >= logs.pages else '' }}" href="{{ next_url }}">下一页</a>
    </div>

    <div class="foot">
      <span>日志只记"谁在什么时候做了什么、多大规模、哪一步失败"，
            不记录问题原文、密码与 token。默认保留最新 {{ keep_rows }} 条。</span>
      <a class="back" href="/">返回首页</a>
    </div>
  </div>

  <script>
  (function () {
    var autoBox = document.getElementById('autoRefresh');
    var body = document.getElementById('logBody');
    var info = document.getElementById('pagerInfo');
    var timer = 0;
    var KEY = 'adminLogAuto';

    function esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }

    /* 把一批日志画回表格。用 DOM 拼而不是 innerHTML 直接插值：
       日志内容是外部输入（URL、关键词、错误信息），拼字符串就等于开了 XSS。 */
    function render(payload) {
      if (!body || !payload || !payload.rows) return;
      body.innerHTML = '';
      if (!payload.rows.length) {
        var tr0 = document.createElement('tr');
        var td0 = document.createElement('td');
        td0.colSpan = 5; td0.className = 'empty'; td0.textContent = '没有符合条件的日志';
        tr0.appendChild(td0); body.appendChild(tr0);
      }
      payload.rows.forEach(function (r) {
        var tr = document.createElement('tr');

        var tdTs = document.createElement('td');
        tdTs.className = 'log-ts';
        tdTs.textContent = r.ts || '';

        var tdLv = document.createElement('td');
        var sp = document.createElement('span');
        sp.className = 'lv lv-' + (r.level || 'INFO');
        sp.textContent = r.level || '';
        tdLv.appendChild(sp);

        var tdMd = document.createElement('td');
        tdMd.textContent = r.module || '';

        var tdMsg = document.createElement('td');
        tdMsg.className = 'log-msg';
        tdMsg.appendChild(document.createTextNode(r.message || ''));
        if (r.user_id) {
          var u = document.createElement('span');
          u.className = 'hint';
          u.textContent = '（用户 #' + r.user_id + '）';
          tdMsg.appendChild(u);
        }
        if (r.detail) {
          var d = document.createElement('details');
          d.className = 'log-detail';
          var s = document.createElement('summary');
          s.textContent = '详情';
          var pre = document.createElement('pre');
          pre.textContent = r.detail;
          d.appendChild(s); d.appendChild(pre);
          tdMsg.appendChild(d);
        }

        var tdIp = document.createElement('td');
        tdIp.className = 'hint';
        tdIp.textContent = r.ip || '—';

        tr.appendChild(tdTs); tr.appendChild(tdLv); tr.appendChild(tdMd);
        tr.appendChild(tdMsg); tr.appendChild(tdIp);
        body.appendChild(tr);
      });
      if (info) {
        info.textContent = '第 ' + payload.page + ' / ' + payload.pages +
                           ' 页 · 共 ' + payload.total + ' 条';
      }
    }

    function refresh() {
      var qs = new URLSearchParams(location.search);
      qs.set('tab', 'logs');
      fetch('/api/admin/logs?' + qs.toString(), { credentials: 'include' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && d.success) render(d); })
        .catch(function () { /* 网络抖动就算了，下一轮再试 */ });
    }

    function setTimer(on) {
      if (timer) { clearInterval(timer); timer = 0; }
      if (on) timer = setInterval(refresh, 5000);
    }

    if (autoBox) {
      try { autoBox.checked = localStorage.getItem(KEY) === '1'; } catch (e) {}
      autoBox.addEventListener('change', function () {
        try { localStorage.setItem(KEY, autoBox.checked ? '1' : '0'); } catch (e) {}
        setTimer(autoBox.checked);
        if (autoBox.checked) refresh();
      });
      setTimer(autoBox.checked);
    }

    window.clearLogs = function () {
      if (!confirm('确定清空全部日志吗？此操作不可恢复。')) return;
      fetch('/api/admin/logs', { method: 'DELETE', credentials: 'include' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.success) { alert(d.error || '清空失败'); return; }
          location.href = '/admin/?tab=logs';
        })
        .catch(function () { alert('清空失败：网络错误'); });
    };
  })();
  </script>

  {% else %}
  <div class="card">
    {% if load_error %}<div class="error">{{ load_error }}</div>{% endif %}
    <div class="stats">
      <div><b>{{ count }}</b><span>当前用户</span></div>
      <div><b>{{ admin_user }}</b><span>当前管理员</span></div>
    </div>
    <table>
      <thead><tr><th>ID</th><th>用户名</th><th>邮箱</th><th>注册时间</th><th>最后登录</th></tr></thead>
      <tbody>
      {% for u in users %}
        <tr>
          <td>{{ u.id }}</td>
          <td><div class="user"><span class="avatar">{{ (u.username or '?')[:1] }}</span>{{ u.username }}</div></td>
          <td>{{ u.email or '—' }}</td>
          <td>{{ u.created_at or '—' }}</td>
          <td>{{ u.last_login_at or '—' }}</td>
        </tr>
      {% endfor %}
      {% if not users %}
        <tr><td colspan="5" class="empty">暂无用户</td></tr>
      {% endif %}
      </tbody>
    </table>
    <div class="foot">
      <span>数据来自 accounts.json；密码哈希不会显示。</span>
      <a class="back" href="/">返回首页</a>
    </div>
  </div>
  {% endif %}
</div>
</body></html>"""


def _admin_response(template, **context):
    resp = make_response(render_template_string(template, **context))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _admin_users():
    try:
        users = db.list_users(500) or []
    except Exception as e:  # noqa: BLE001 - 账号文件坏了也要能看到错误原因
        return [], f"读取用户列表失败：{type(e).__name__}: {e}"
    users = sorted(users, key=lambda u: (u.get("id") or 0))
    return users, ""


@app.get("/admin")
def admin_no_slash():
    """只允许 /admin/；不带斜杠不暴露登录页，回首页。"""
    return redirect("/")


def _admin_log_url(page, level, module, q):
    """拼日志页的翻页链接（保留当前筛选条件）。"""
    params = ["tab=logs", "page=%d" % max(1, int(page))]
    if level:
        params.append("level=" + quote(level))
    if module:
        params.append("module=" + quote(module))
    if q:
        params.append("q=" + quote(q))
    return "/admin/?" + "&".join(params)


@app.get("/admin/")
def admin_page():
    if session.get("admin_ok"):
        tab = (request.args.get("tab") or "users").strip().lower()
        if tab == "logs":
            level = (request.args.get("level") or "").strip().upper()
            module = (request.args.get("module") or "").strip()
            keyword = (request.args.get("q") or "").strip()
            data = runlog.query(level=level, module=module, keyword=keyword,
                                page=request.args.get("page") or 1, size=50)
            st = runlog.stats()
            by_level = st.get("by_level") or {}
            return _admin_response(
                _ADMIN_DASHBOARD_HTML, tab="logs",
                users=[], count=0, admin_user=ADMIN_USER, load_error="",
                logs=data, log_stats=st,
                err_count=by_level.get("ERROR", 0),
                warn_count=by_level.get("WARN", 0),
                levels=runlog.LEVELS, modules=runlog.MODULES,
                f_level=level, f_module=module, f_q=keyword,
                keep_rows=runlog.MAX_ROWS,
                prev_url=_admin_log_url(data["page"] - 1, level, module, keyword),
                next_url=_admin_log_url(data["page"] + 1, level, module, keyword))
        users, load_error = _admin_users()
        return _admin_response(_ADMIN_DASHBOARD_HTML, tab="users",
                               users=users, count=len(users),
                               admin_user=ADMIN_USER, load_error=load_error,
                               logs={"rows": [], "page": 1, "pages": 1, "total": 0},
                               log_stats={}, err_count=0, warn_count=0,
                               levels=runlog.LEVELS, modules=runlog.MODULES,
                               f_level="", f_module="", f_q="",
                               keep_rows=runlog.MAX_ROWS,
                               prev_url="/admin/?tab=logs", next_url="/admin/?tab=logs")
    # 普通账号已登录：按要求直接弹回初始页，不给任何管理员入口
    if session.get("user_id"):
        return redirect("/")
    return _admin_response(_ADMIN_LOGIN_HTML, error="", username="")


# ── 日志查询接口（管理员页面自动刷新用；也方便脚本拉取）──────────
@app.get("/api/admin/logs")
def api_admin_logs():
    if not session.get("admin_ok"):
        return jsonify({"success": False, "error": "需要管理员权限"}), 403
    data = runlog.query(
        level=(request.args.get("level") or "").strip().upper(),
        module=(request.args.get("module") or "").strip(),
        keyword=(request.args.get("q") or "").strip(),
        page=request.args.get("page") or 1,
        size=request.args.get("size") or 50)
    return jsonify({"success": True, **data, "stats": runlog.stats()})


@app.delete("/api/admin/logs")
def api_admin_logs_clear():
    if not session.get("admin_ok"):
        return jsonify({"success": False, "error": "需要管理员权限"}), 403
    deleted = runlog.clear()
    runlog.warn("admin", "清空了运行日志（%s 条）" % deleted)
    return jsonify({"success": True, "deleted": max(0, deleted)})


@app.post("/admin/login")
def admin_login():
    if session.get("admin_ok"):
        return redirect("/admin/")
    if session.get("user_id"):
        return redirect("/")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ok_user = hmac.compare_digest(username, ADMIN_USER)
    ok_pass = hmac.compare_digest(password, ADMIN_PASSWORD)
    if ok_user and ok_pass:
        session["admin_ok"] = True
        runlog.info("admin", "管理员登录成功", detail={"admin": ADMIN_USER,
                                                     "loopback": _is_loopback_request()})
        return redirect("/admin/")
    runlog.warn("admin", "管理员登录失败", detail={"admin": username[:32],
                                                "loopback": _is_loopback_request()})
    return _admin_response(_ADMIN_LOGIN_HTML,
                           error="账号或密码不正确", username=username), 401


@app.post("/admin/logout")
def admin_logout():
    if session.get("admin_ok"):
        runlog.info("admin", "管理员退出登录")
    session.pop("admin_ok", None)
    return redirect("/")


# ═══════════════════════════════════════════════════════════════
#  账号管理（设置 → 账号管理）：改用户名 / 改密码 / 改头像
# ═══════════════════════════════════════════════════════════════

_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.\-]{3,24}$")
_AVATAR_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\s]+)$")
AVATAR_MAX_B64 = 200 * 1024


def _clean_avatar(raw):
    """校验头像 data URL。返回 (ok, 值或 None, 错误信息)。

    规则：空串=清除；只接受 png/jpeg/webp 的 data URL；base64 部分 <= 200KB。
    注册与"账号管理里改头像"共用这一份，避免两处规则跑偏。
    """
    raw = (raw or "").strip()
    if not raw:
        return True, "", ""
    m = _AVATAR_RE.match(raw)
    if not m:
        return False, None, "头像格式不对，只支持 png/jpeg/webp 的 data URL"
    if len(m.group(2)) > AVATAR_MAX_B64:
        return False, None, "头像太大（压缩后应小于 200KB）"
    return True, raw, ""


@app.post("/api/account/username")
@login_required
def api_account_username():
    """改用户名。校验规则与注册保持一致（3 个字符起，允许中文），并查重。"""
    data = request.get_json(force=True, silent=True) or {}
    new_name = (data.get("username") or "").strip()
    if len(new_name) < 3:
        return jsonify({"success": False, "error": "用户名至少3个字符"}), 400
    if not _USERNAME_RE.match(new_name):
        return jsonify({"success": False,
                        "error": "用户名只能包含中英文、数字、下划线、点和短横线（3-24 位）"}), 400
    ok_, msg = db.update_username(session["user_id"], new_name)
    if not ok_:
        return jsonify({"success": False, "error": msg}), 400
    # 同步 session 里的用户名（否则页面上还显示旧名字）
    session["username"] = new_name
    user = db.get_user_by_id(session["user_id"]) or {}
    return jsonify({"success": True, "message": msg,
                    "user": {"id": user.get("id"), "username": user.get("username"),
                             "avatar": user.get("avatar") or ""}})


@app.post("/api/account/password")
@login_required
def api_account_password():
    """改密码。

    界面上只让填「新密码 + 确认新密码」（用户要求去掉当前密码输入框），
    所以 **old_password 是可选的**：带了就校验（给别处调用留二次确认的能力），
    没带就只看 session —— 请求本身必须已登录。
    """
    data = request.get_json(force=True, silent=True) or {}
    old = (data.get("old_password") or "").strip()
    new = (data.get("new_password") or "").strip()
    if not new:
        return jsonify({"success": False, "error": "请填写新密码"}), 400
    if old and not db.verify_password(session["user_id"], old):
        return jsonify({"success": False, "error": "当前密码不正确"}), 400
    if len(new) < 8 or len(new) > 16:
        return jsonify({"success": False, "error": "新密码需为8-16位"}), 400
    if not (re.search(r"[a-z]", new) and re.search(r"[A-Z]", new) and re.search(r"\d", new)):
        return jsonify({"success": False, "error": "新密码需包含大写字母、小写字母和数字"}), 400
    if old and new == old:
        return jsonify({"success": False, "error": "新密码不能和当前密码相同"}), 400
    db.update_password(session["user_id"], new)
    return jsonify({"success": True, "message": "密码已更新，下次登录请用新密码"})


@app.post("/api/account/avatar")
@login_required
def api_account_avatar():
    """改头像。前端已压到 128×128 的 data URL，这里只做保存与兜底校验。

    限制：只接受 data:image/png|jpeg|webp;base64,...，且 base64 部分 <= 200KB
    （128×128 的图正常只有 5-30KB，给足余量但不允许塞别的东西）。
    """
    data = request.get_json(force=True, silent=True) or {}
    ok_, avatar, err = _clean_avatar(data.get("avatar"))
    if not ok_:
        return jsonify({"success": False, "error": err}), 400
    db.update_avatar(session["user_id"], avatar)
    return jsonify({"success": True,
                    "message": "头像已清除" if not avatar else "头像已更新",
                    "avatar": avatar})


@app.get("/api/local-accounts")
def local_accounts():
    """本机已注册的账号（**只有用户名与时间，永远不返回密码或哈希**）。

    存在意义：账号本来就落在项目目录的 literature.db 里，但界面上看不见，
    用户每次都得回忆自己的用户名。这里列出来，点一下就填。
    默认只对**本机回环请求**开放（公网部署时不泄露用户名列表）。
    """
    if not _account_hint_allowed():
        return jsonify({"success": True, "accounts": [], "count": 0,
                        "reason": "非本机访问，不返回账号列表"})
    try:
        accounts = db.list_users(20)
        total = db.count_users()
    except Exception as e:  # noqa: BLE001 - 库还没建好时不该 500
        return jsonify({"success": False, "accounts": [], "count": 0,
                        "error": f"{type(e).__name__}: {e}"})
    return jsonify({
        "success": True,
        "accounts": accounts,
        "count": total,
        "db_path": os.path.basename(db.DB_PATH),
        "note": "账号保存在项目目录的 literature.db；密码只存 bcrypt 哈希，不可逆。",
    })


@app.post("/api/logout")
def logout():
    uid = session.get("user_id")
    uname = session.get("username")
    if uid:
        runlog.info("auth", "登出：%s" % uname, user_id=uid)
    session.clear()
    return jsonify({"success": True, "message": "已登出"})


@app.get("/api/me")
def get_current_user():
    if 'user_id' in session:
        user = db.get_user_by_id(session['user_id'])
        if user:
            return jsonify({"success": True, "user": user})
    return jsonify({"success": False, "user": None})


# ═══════════════════════════════════════════════════════════════
#  路由：API Key 配置
#
#  为什么要有这一段：Key 不随仓库分发，以前缺 Key 时服务直接退出，
#  打开页面只看到控制台一屏红字。现在改成"先让站点起来"，由页面右上角的
#  「填写 API Key」按钮引导填写 —— 对第一次上手的人友好得多。
# ═══════════════════════════════════════════════════════════════

def _mask_api_key(key: str) -> str:
    """只回显头尾：够用户认出"填的是哪一把"，又不会把整把 Key 送到前端。"""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "****"
    return f"{key[:6]}…{key[-4:]}"


def _api_key_editable() -> bool:
    """这笔请求能不能改站点的 API Key。

    本机访问（127.0.0.1 / ::1）直接放行 —— 那就是跑服务的人自己在填。
    从公网/内网穿透过来的请求要求先登录：否则任何拿到链接的人都能把站点
    用的 Key 换成自己的（轻则站点不可用，重则流量走别人的账单）。
    """
    if (request.remote_addr or "") in ("127.0.0.1", "::1"):
        return True
    return "user_id" in session


def _write_api_key(key: str) -> str:
    """把 Key 写进 `.env`（该文件不进版本库），返回文件路径。

    两件事一起做：
      1. `.env` 不存在就先生成一份（`envconfig.ensure_env_file`）——
         所以新机器上"填完 Key 就能跑"，不用手工复制 `.env.example`；
      2. 只替换 `DEEPSEEK_API_KEY` 那一行，文件里的注释和其它配置原样保留。

    另外，若 `.env.local` 里也写了这一项，要跟着一起改：它优先级更高，
    不改的话会继续盖住新 Key，页面显示"保存成功"实际却没生效。
    """
    env_file, _created = envconfig.ensure_env_file()

    def upsert(target: str, append_if_missing: bool) -> None:
        lines: list[str] = []
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()

        new_line = f"DEEPSEEK_API_KEY={key}"
        for i, old in enumerate(lines):
            if re.match(r"^\s*(export\s+)?DEEPSEEK_API_KEY\s*=", old):
                lines[i] = new_line
                break
        else:
            if not append_if_missing:
                return
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("# 由网页右上角的「填写 API Key」写入（.env 不进版本库）")
            lines.append(new_line)

        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    upsert(env_file, append_if_missing=True)
    if os.path.exists(envconfig.ENV_LOCAL_FILE):
        upsert(envconfig.ENV_LOCAL_FILE, append_if_missing=False)
    return env_file


# ── 模型提供商（类似常见客户端的「模型 → 添加提供商」）──────────────
# 一家一条记录，各自一把 Key；当前生效的那家由 providers.json 的 active_id 决定。
# 这样不用改代码就能在 DeepSeek 官方 / 各家兼容服务 / 本地推理之间切换。
try:
    import provider_store  # noqa: E402
except Exception:            # pragma: no cover - 极端情况下也不该挡住站点起来
    provider_store = None


@app.get("/api/providers")
def list_providers():
    """提供商列表（Key 只回显头尾）+ 当前生效的那一家。"""
    if provider_store is None:
        return jsonify({"success": False, "error": "提供商模块不可用"}), 500
    data = provider_store.list_public()
    data.update({
        "success": True,
        "editable": _api_key_editable(),
        "env_fallback": data.get("env_fallback", False),
    })
    return jsonify(data)


@app.post("/api/providers")
def save_provider():
    """新增或修改一家提供商。api_key 留空 = 沿用原来的（编辑时不必重填）。"""
    if provider_store is None:
        return jsonify({"success": False, "error": "提供商模块不可用"}), 500
    if not _api_key_editable():
        return jsonify({"success": False, "error": "请先登录后再修改模型提供商"}), 403
    data = request.get_json(force=True, silent=True) or {}
    try:
        item = provider_store.upsert(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"success": False, "error": f"写入 providers.json 失败：{e}"}), 500
    # 立刻生效：llm.api_key()/base_url() 每次请求都重新解析，不用重启。
    return jsonify({
        "success": True,
        "message": "已保存，立即生效（无需重启）",
        "id": item["id"],
        "list": provider_store.list_public(),
    })


@app.delete("/api/providers/<pid>")
def remove_provider(pid):
    if provider_store is None:
        return jsonify({"success": False, "error": "提供商模块不可用"}), 500
    if not _api_key_editable():
        return jsonify({"success": False, "error": "请先登录后再修改模型提供商"}), 403
    ok = provider_store.delete(pid)
    if not ok:
        return jsonify({"success": False, "error": "没有这一家提供商"}), 404
    return jsonify({"success": True, "message": "已删除", "list": provider_store.list_public()})


@app.post("/api/providers/<pid>/activate")
def activate_provider(pid):
    if provider_store is None:
        return jsonify({"success": False, "error": "提供商模块不可用"}), 500
    if not _api_key_editable():
        return jsonify({"success": False, "error": "请先登录后再切换模型提供商"}), 403
    if not provider_store.set_active(pid):
        return jsonify({"success": False, "error": "没有这一家提供商"}), 404
    return jsonify({"success": True, "message": "已切换", "list": provider_store.list_public()})


@app.get("/api/config")
def get_config():
    """前端启动时问一次：当前用的是哪家、Key 配了没（只给头尾）。"""
    key = llm_layer.api_key()
    return jsonify({
        "success": True,
        "api_key_set": bool(key),
        "api_key_hint": _mask_api_key(key),
        "api_key_editable": _api_key_editable(),
        "model": llm_layer.model_for("chat"),
    })


@app.post("/api/config/api-key")
def save_api_key():
    if not _api_key_editable():
        return jsonify({"success": False,
                        "error": "请先登录后再填写 API Key"}), 403

    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("api_key") or "").strip().strip('"').strip("'").strip()

    if not key:
        return jsonify({"success": False, "error": "请填写 API Key"}), 400
    if not key.startswith("sk-") or len(key) < 20 or any(c.isspace() for c in key):
        return jsonify({
            "success": False,
            "error": "格式不对：DeepSeek 的 Key 以 sk- 开头，后面是一长串字母数字",
        }), 400

    try:
        path = _write_api_key(key)
    except OSError as e:
        return jsonify({"success": False, "error": f"写入 .env 失败：{e}"}), 500

    # 立刻生效：llm.api_key() 每次请求都重读环境变量，不用重启服务。
    os.environ["DEEPSEEK_API_KEY"] = key
    # 同步一下 import 时的快照，兼容仍直接读 llm.API_KEY 的旧代码。
    llm_layer.API_KEY = key

    return jsonify({
        "success": True,
        "message": f"已保存到 {os.path.basename(path)}，立即生效（无需重启）",
        "api_key_set": True,
        "api_key_hint": _mask_api_key(key),
    })


# ═══════════════════════════════════════════════════════════════
#  路由：搜索历史
# ═══════════════════════════════════════════════════════════════

@app.get("/api/history")
@login_required
def get_history():
    history = db.get_search_history(session['user_id'], 20)
    return jsonify({"success": True, "history": history})


@app.delete("/api/history")
@login_required
def clear_history():
    db.clear_search_history(session['user_id'])
    return jsonify({"success": True, "message": "历史已清空"})


# ═══════════════════════════════════════════════════════════════
#  路由：会话 / 对话记录（登录后可用）
# ═══════════════════════════════════════════════════════════════

@app.get("/api/conversations")
@login_required
def list_conversations():
    """当前用户的会话列表"""
    conversations = db.get_conversations(session['user_id'])
    return jsonify({"success": True, "conversations": conversations})


@app.post("/api/conversations")
@login_required
def new_conversation():
    """新建会话（标题可用首条消息自动生成）"""
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get('title') or '新对话').strip() or '新对话'
    conversation_id = db.create_conversation(session['user_id'], title)
    if not conversation_id:
        return jsonify({"error": "创建会话失败"}), 500
    return jsonify({"success": True, "conversation": {"id": conversation_id, "title": title[:50]}})


@app.get("/api/conversations/<int:conversation_id>")
@login_required
def read_conversation(conversation_id):
    """读取某个会话的全部消息"""
    conversation, messages = db.get_conversation_messages(session['user_id'], conversation_id)
    if not conversation:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"success": True, "conversation": conversation, "messages": messages})


@app.delete("/api/conversations/<int:conversation_id>")
@login_required
def remove_conversation(conversation_id):
    """删除某个会话。

    **幂等**：会话本来就不在（别的标签页删过 / 连点了两次 / 这个页面的
    侧栏列表是旧的）也返回成功。

    以前这里回 404「会话不存在」，前端就会把它当成聊天错误，往对话里塞一条
    「助手：❌ 会话不存在」—— 用户看到一串报错气泡，却不知道自己到底删没删掉。
    对调用方来说结果是一样的：这个会话现在确实没了，那就该是成功。
    """
    deleted = db.delete_conversation(session['user_id'], conversation_id)
    return jsonify({"success": True, "message": "已删除", "already": not deleted})


@app.delete("/api/conversations")
@login_required
def clear_all_conversations():
    """清空当前用户的全部会话"""
    db.clear_conversations(session['user_id'])
    return jsonify({"success": True, "message": "已清空所有对话记录"})


# ═══════════════════════════════════════════════════════════════
#  路由：收藏（简化版）
# ═══════════════════════════════════════════════════════════════

@app.post("/api/favorites")
@login_required
def add_favorite():
    """添加收藏"""
    data = request.get_json(force=True, silent=True) or {}
    paper_id = data.get('paper_id', '').strip()
    paper_title = data.get('paper_title', '').strip()
    # 作者/年份：前端从结构化检索结果里带过来，让收藏列表有出处信息
    authors = str(data.get('authors') or '').strip()
    year = str(data.get('year') or '').strip()

    if not paper_title:
        return jsonify({"error": "缺少论文标题"}), 400

    if not paper_id or paper_id.startswith('title_'):
        clean_title = re.sub(r'[《》\s]', '_', paper_title)
        paper_id = 'title_' + clean_title[:50]

    success = db.add_favorite(session['user_id'], paper_id, paper_title, authors, year)
    if success:
        return jsonify({
            "success": True,
            "message": "已收藏",
            "paper": {
                "title": paper_title,
                "authors": authors,
                "year": year,
            }
        })
    else:
        return jsonify({"error": "已收藏过"}), 400


@app.delete("/api/favorites/<path:paper_id>")
@login_required
def remove_favorite(paper_id):
    """取消收藏"""
    db.remove_favorite(session['user_id'], paper_id)
    return jsonify({"success": True, "message": "已取消收藏"})


@app.delete("/api/favorites")
@login_required
def clear_favorites():
    """清空所有收藏"""
    db.clear_favorites(session['user_id'])
    return jsonify({"success": True, "message": "已清空所有收藏"})


@app.get("/api/favorites")
@login_required
def get_favorites():
    """获取收藏列表"""
    favorites = db.get_favorites(session['user_id'])
    return jsonify({"success": True, "favorites": favorites})


@app.get("/api/favorites/<path:paper_id>")
@login_required
def check_favorite(paper_id):
    """检查是否已收藏"""
    is_fav = db.is_favorited(session['user_id'], paper_id)
    return jsonify({"success": True, "is_favorited": is_fav})


# ═══════════════════════════════════════════════════════════════
#  路由：论文摘要润色
# ═══════════════════════════════════════════════════════════════

# 五种风格：key -> (显示名, 给模型的风格要求)
# 每种都要求"重写"，差别只在"重写成什么样"。
POLISH_STYLES = {
    "academic": ("学术正式",
                 "保持论文语气：客观、克制、术语准确，句子要有信息密度，不要口语和夸张。"),
    "concise": ("精简紧凑",
                "删掉重复与铺垫，一句话只说一件核心事实；能用短句就不用长句，但不牺牲逻辑连接。"),
    "clear": ("清晰易懂",
              "术语首次出现时用半句解释；把抽象结论落到具体对象与动作上，让非本领域读者也能顺着读完。"),
    "vivid": ("生动有画面感",
              "用可感知的动作、场景与对比替换抽象名词，让读者在脑中\u201c看见\u201d实验怎么做的、现象怎么发生的；"
              "比喻要克制，一段最多一处。"),
    "logic": ("强化逻辑",
              "显式写出因果、转折、递进与限定；每段先给结论再给依据，句与句之间用承接词扣住，不允许跳跃。"),
}

POLISH_SYSTEM_TEMPLATE = """你是一位中文学术写作编辑。你的任务不是校对错别字，而是**重写这一段文字**。

硬性要求：
1. **逐句重写**：除了专有名词、术语、公式、数据与文献编号，不允许整句照抄原文；
   至少一半的句子要换掉句法结构（换个主语、调主从句顺序、合并或拆分）。
2. **更具体**：把\u201c显著提升 / 有效改善 / 存在一定影响\u201d这类空话，换成原文能支撑的具体对象、动作与条件。
3. **有逻辑**：显式写出因果、转折、递进、限定；读者读完要能回答\u201c为什么\u201d和\u201c然后呢\u201d。
4. **通顺**：一句话只说一件事，长句拆开；避免连续的\u201c的\u201d和重复词。
5. **有画面感**：用可感知的动作与场景替换抽象名词（例如\u201c模型学会了在噪声里抓住主要趋势\u201d，
   而不是\u201c提升了鲁棒性\u201d）；保持学术分寸，不煽情、不堆比喻。
6. **绝对不许编造**：原文没有的数字、百分比、准确率、数据集名、对比方法、引用，一律不得出现。
   宁可少一点画面感，也不许补事实——这是硬红线。
7. **信息不丢失**：原文的每个要点（做法、数据、指标、结论、局限）都要在新文本里有对应表达；
   改写不是缩写，篇幅应与原文相当（\u00b120%），除非用户选的是\u201c精简紧凑\u201d风格。
8. **事实不变**：数字、术语、结论与因果关系不得改变。

风格要求：{style_hint}

改写示例（只示范\u201c怎么改\u201d，内容与本次无关）：
原文：本文提出了一种新的遥感影像分割方法，实验表明该方法效果很好。
改写：本文的工作是一套新的遥感影像分割方法；从实验结果看，它的分割效果明显好于原有做法。

输出格式（严格遵守）：
第一行开始直接是润色后的正文，**不要**写\u201c润色后的正文\u201d这类标题或占位符，也不要用代码块；
正文之后空一行，写【改动说明】，再列 3 条以内的短句说明改了什么。
"""

# 数字（含百分比）：用来拦截"润色时凭空编数据"
_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*%?")


def _new_numbers(src, out):
    """润色后新出现的数字（原文没有的）。学术写作里编数据是硬伤，必须拦。"""
    a = {m.replace(" ", "") for m in _NUM_RE.findall(src or "")}
    b = {m.replace(" ", "") for m in _NUM_RE.findall(out or "")}
    return sorted(b - a)


def _split_sentences(text):
    parts = re.split(r"[。！？!?；;\n]+", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 6]


def _sentence_overlap(src, out):
    """有多大比例的句子还和原文某句几乎一样（0~1）。

    只看整体相似度会漏掉"逐句换几个词"的假润色：字数变了、句子结构没变。
    """
    a = _split_sentences(src)
    b = _split_sentences(out)
    if not a or not b:
        return 0.0
    same = 0
    for s in b:
        best = max((_text_similarity(s, t) for t in a), default=0.0)
        if best >= 0.8:
            same += 1
    return same / len(b)


def _polish_retry_note(last, abstract):
    """根据上一版到底哪儿不合格，给第二次尝试一句针对性的要求。"""
    if not last:
        return ""
    bits = ["\n\n注意：上一版没有通过检查，请重写："]
    if last.get("invented"):
        bits.append("你编造了原文没有的数据（" + "、".join(last["invented"][:3]) +
                    "），全部删掉；原文没有数字就不要写数字。")
    if last.get("short"):
        bits.append("上一版把原文的要点删掉了（明显变短）。改写不是缩写，"
                    "原文每一个要点都要在新文本里写回来。")
    if last.get("same_sent"):
        bits.append("上一版有超过一半的句子和原文几乎一样，必须换掉句子的结构"
                    "（换个主语、调主从句顺序、合并或拆分句子）。")
    if last.get("sim", 0) > 0.9:
        bits.append("整体改动太小，至少要有一半的句子是重新组织过的。")
    return "".join(bits)


def _split_polish_output(text):
    """把模型输出拆成 (正文, 改动说明列表)。没带说明就整段当正文。"""
    raw = (text or "").strip()
    # 模型有时会照着模板把占位符也抄出来（<润色后的正文> / 【润色后的正文】），去掉
    raw = re.sub(r"^(?:<[^>\n]{1,20}>|【[^】\n]{1,20}】)\s*", "", raw)
    if not raw:
        return "", []
    mark = "【改动说明】"
    idx = raw.find(mark)
    if idx < 0:
        return raw, []
    body = raw[:idx].strip()
    notes = []
    for line in raw[idx + len(mark):].splitlines():
        line = re.sub(r"^(?:\d+[.、)]|[-–—•·*])\s*", "", line.strip()).strip()
        if line:
            notes.append(line[:60])
    return body, notes[:4]


def _text_similarity(a, b):
    """字符级相似度（忽略空白）。用来判断"润色后是不是几乎没变"。"""
    x = re.sub(r"\s+", "", a or "")
    y = re.sub(r"\s+", "", b or "")
    if not x or not y:
        return 0.0
    return _SequenceMatcher(None, x, y).ratio()


@app.post("/api/polish")
@login_required
def polish_abstract():
    """AI 润色论文摘要：**重写**，而不是校对。

    老版本只写了一句"润色成更学术的风格，保持原意不变"，轻量模型经常原样返回，
    用户看到的就是"润色前后一模一样"。现在做了三件事：
      1. 提示词改成硬约束（逐句重写、具体化、显式逻辑、可控的画面感、事实不变）；
      2. 服务端算一次相似度，**太像就带着"上次几乎没改"的提醒重试一次**；
      3. 顺带让模型给出 3 条"改动说明"，界面上能看出到底动了哪里。
    """
    if not llm_layer.available():
        return jsonify({"error": "还没有填写 DeepSeek API Key",
                        "need_api_key": True}), 400

    data = request.get_json(force=True, silent=True) or {}
    abstract = data.get('abstract', '').strip()
    style = (data.get('style') or 'academic').strip()
    if style not in POLISH_STYLES:
        style = 'academic'

    if not abstract:
        return jsonify({"error": "请提供需要润色的摘要内容"}), 400
    if len(abstract) < 20:
        return jsonify({"error": "摘要内容太短，至少需要20个字符"}), 400
    if len(abstract) > 5000:
        return jsonify({"error": "摘要内容过长，请控制在5000字符以内"}), 400

    # 短文本天然相似度高，阈值放宽一点
    limit = 0.95 if len(abstract) < 80 else 0.90
    system_base = POLISH_SYSTEM_TEMPLATE.format(style_hint=POLISH_STYLES[style][1])
    max_tokens = min(3000, max(800, len(abstract) * 2))

    candidates = []          # 每轮结果：正文/说明/相似度/是否编数字/句子是否复用/是否删减过多
    try:
        for attempt in range(2):
            system_prompt = system_base
            if attempt:
                system_prompt += _polish_retry_note(candidates[-1] if candidates else None, abstract)
            # 用主模型（chat 槽位）：fast 槽位改写偏保守，容易只换几个同义词。
            # 润色是一次点击触发的轻操作，多等几秒换真正的重写是值得的。
            model = llm_layer.chat_model("chat", temperature=0.6, max_tokens=max_tokens)
            response = model.invoke([
                {"role": "system", "content": system_prompt},
                # 格式要求放在 user 里：模型对"用户这句话"里的输出格式更买账
                {"role": "user", "content": "原文：\n" + abstract +
                 "\n\n请按要求输出：先给改写后的正文，空一行，再写【改动说明】和 3 条以内的短句。"},
            ])
            body, got_notes = _split_polish_output(getattr(response, "content", ""))
            if not body:
                continue
            similarity = _text_similarity(abstract, body)
            invented = _new_numbers(abstract, body)
            same_sentences = _sentence_overlap(abstract, body) >= 0.5
            # 改写不是缩写：非"精简"风格下，结果明显比原文短 = 丢了要点
            too_short = style != "concise" and len(body) < len(abstract) * 0.6
            candidates.append({
                "body": body, "notes": got_notes, "sim": similarity,
                "invented": invented, "same_sent": same_sentences, "short": too_short,
            })
            if similarity <= limit and not invented and not same_sentences and not too_short:
                break
    except Exception as e:
        return jsonify({"error": f"润色失败: {str(e)}"}), 500

    if not candidates:
        return jsonify({"error": "没能生成润色结果，请稍后重试或把摘要分成两段"}), 502

    # 安全优先：宁可"改得不够"，也不能返回编造的数字；其次不能丢掉原文要点
    safe = [c for c in candidates if not c["invented"]] or candidates
    best = sorted(safe, key=lambda c: (c["short"], c["same_sent"], c["sim"]))[0]
    polished, notes = best["body"], best["notes"]
    similarity, invented = best["sim"], best["invented"]

    if invented:
        return jsonify({
            "error": "模型生成时编造了原文没有的数据（如 " + "、".join(invented[:3]) +
                     "），已拒绝返回。请换个风格或手动补上这些数据后再润色。"
        }), 502

    return jsonify({
        "success": True,
        "original": abstract,
        "polished": polished,
        "notes": notes,
        "changed": similarity <= limit,
        "similarity": round(similarity, 3),
        "style": style,
        "word_count_original": len(abstract),
        "word_count_polished": len(polished),
    })


@app.post("/api/term")
def explain_term():
    """名词解释：把读论文时遇到的术语讲成「定义 + 通俗 + 语境 + 相关概念」。

    为什么不要求登录：查一个词是轻量的阅读辅助，和聊天一样对游客开放；
    但输入长度与输出结构都做硬约束，避免变成“任意长文本改写”的免费通道。
    """
    data = request.get_json(force=True, silent=True) or {}
    term = (data.get("term") or "").strip()
    context = (data.get("context") or "").strip()

    if not term:
        return jsonify({"error": "请输入要解释的名词"}), 400
    if len(term) > 60:
        return jsonify({"error": "名词太长（不超过 60 字），只填术语本身即可"}), 400
    if len(context) > 1200:
        context = context[:1200]

    system = (
        "你是科研术语讲解员，读者是刚进入该领域的研究生。要求：\n"
        "1) **只解释用户给出的那个术语**；原文只用来判断它在这个语境里指什么，"
        "不要改去解释原文里出现的其他词；\n"
        "2) 解释必须准确；拿不准就说明不确定，不要编造出处、人名与数字；\n"
        "3) 先给一句话定义，再用大白话讲一遍，必要时打比方；\n"
        "4) 如果给了原文，说明它在这个语境里具体指什么；没给就讲一般用法；\n"
        "5) 指出最容易混淆的概念或常见误解。\n"
        "只输出 JSON，不要 markdown 代码块。字段：\n"
        '{"term":"术语（中英文对照）","en":"英文全称，没有就空串",'
        '"definition":"一句话定义（不超过60字）","plain":"通俗解释（2-3句）",'
        '"context":"在论文语境中的含义（2-3句）",'
        '"related":["相关概念1","相关概念2","相关概念3"],'
        '"misuse":"常见误解（1-2句，没有就写暂无）","read_more":"想深入该查什么（1句）"}'
    )
    user = "要解释的术语（只解释这个词）：" + term
    if context:
        user += "\n\n它出现的原文（仅用于判断语境，不要逐句翻译）：\n" + context

    try:
        out = llm_layer.call_json(system, user, kind="fast",
                                  max_tokens=900, temperature=0.2)
    except Exception as e:
        return jsonify({"error": f"解释失败: {e}"}), 500

    if not isinstance(out, dict):
        return jsonify({"error": "暂时没能解释这个词，请稍后重试或换个说法"}), 502

    def _s(key, limit):
        return str(out.get(key) or "").strip()[:limit]

    related = out.get("related") or []
    if isinstance(related, str):
        related = [x for x in re.split(r"[、,，;；\s]+", related) if x]
    related = [str(x).strip()[:40] for x in related if str(x).strip()][:6]

    return jsonify({
        "success": True,
        "term": _s("term", 80) or term,
        "en": _s("en", 80),
        "definition": _s("definition", 200),
        "plain": _s("plain", 600),
        "context": _s("context", 600),
        "related": related,
        "misuse": _s("misuse", 300),
        "read_more": _s("read_more", 200),
    })


# ═══════════════════════════════════════════════════════════════
#  路由：聊天
# ═══════════════════════════════════════════════════════════════

def _text_from_message(message):
    """从模型返回的消息里取正文（兼容 str / 内容块列表）。

    为什么需要它：某些模型/网关在"推理模式"下可能不产生
    `on_chat_model_stream` 分片，正文只在 `on_chat_model_end` 的
    `data.output` 里出现一次。只读分片的话整轮一个 token 都收不到，
    最后只能回一句"抱歉，未能获取到回复" —— 这里做兜底。
    """
    if message is None:
        return ""
    content = getattr(message, "content", message if isinstance(message, str) else None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in ("text", "text_delta"):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _parse_conversation_id(value):
    """把前端传来的 conversation_id 规整成 int 或 None"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _papers_from_tool_output(content):
    """从工具返回的 JSON 字符串里取出结构化论文列表。

    （工具返回 {"papers": [...]}，这里直接解析，前端不再靠正则从
    Markdown 里猜标题 —— 那样一篇结果只能猜出一篇，也拿不到作者/年份。）
    """
    if not isinstance(content, str) or '"papers"' not in content:
        return []
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("papers"), list):
        return [p for p in data["papers"] if isinstance(p, dict)]
    return []


def _pdf_filename(paper):
    """给下载的 PDF 起一个人能看懂的名字：年份_第一作者_标题.pdf

    那些期刊给的原始文件名是 OJS 生成的编号（`1609-1936-1-PB.pdf`），
    存下来完全认不出是哪篇，所以这里重命名。

    必须清洗 Windows 不允许的字符（\\ / : * ? " < > |），否则保存会直接失败；
    标题也截断，避免路径过长。
    """
    def safe(text, limit):
        text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or ""))
        text = re.sub(r"\s+", " ", text).strip(" ._")
        return text[:limit]

    title = safe(paper.get("title") or "paper", 80) or "paper"
    year = safe(paper.get("year"), 8)
    if year == "未知":
        year = ""
    authors = paper.get("authors") or []
    first_author = safe(authors[0], 16).replace(" ", "") if authors else ""

    parts = [p for p in (year, first_author) if p]
    parts.append(title)
    return "_".join(parts) + ".pdf"


def _calc_from_tool_output(content):
    """从 run_science_tool 的返回里取出一次科学计算的记录。

    前端拿到它就能把"真实算出来的数"和 `repro`（工具名 + 参数）一起展示出来，
    而不是只看到模型转述的一段话。这样评委能当场核对"这个 p 值是谁算的"。
    """
    if not isinstance(content, str) or '"tool"' not in content:
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "ok" not in data or "tool" not in data:
        return None
    payload = data.get("result") if isinstance(data.get("result"), dict) else {}
    return {
        "tool": data.get("tool"),
        "ok": bool(data.get("ok")),
        "error": data.get("error"),
        "result": (payload.get("result") if isinstance(payload, dict) else None),
        "repro": (payload.get("repro") if isinstance(payload, dict) else None),
        "elapsed_ms": (payload.get("elapsed_ms") if isinstance(payload, dict) else None),
    }


# 工具名 -> 阶段提示。三类工具说三种话，用户才知道它在"查"还是"算"。
_TOOL_STATUS = {
    "search_papers": "正在检索文献源…",
    "get_paper_detail": "正在读取论文详情…",
    "start_research_workflow": "正在规划研究方案…",
    "list_science_tools": "正在查看可用的科学计算工具…",
    "run_science_tool": "正在调用科学计算工具（真实计算）…",
    "remember_note": "正在写入研究记忆…",
}


def _tool_status(name):
    return _TOOL_STATUS.get(name or "", "正在检索文献源…")


def system_prompt_for_chat(user_id=None, message=""):
    """本轮对话的系统提示词 = 基础提示 + 长周期记忆 + 按需技能。

    记忆与技能都是**追加**，不改变基础规则；两者都做了异常兜底与字数预算，
    因为它们是增强能力，不该在数据库/MCP 出问题时把整轮对话拖垮。
    """
    parts = [DEFAULT_SYSTEM_PROMPT]
    try:
        if user_id:
            hint = memory.summarize_for_prompt(user_id, message)
            if hint:
                parts.append(hint)
    except Exception:  # noqa: BLE001 - 记忆不可用不影响对话
        pass
    try:
        block = skills.skill_context(message, role="coordinator", limit=1,
                                     budget_chars=1400)
        if block:
            parts.append(block)
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(parts)


def _resolve_conversation(user_id, requested_id, question):
    """在流式响应之前就确定本轮要写入哪个会话，并返回该会话 id。

    - 请求里的会话不存在或不属于该用户 -> 新建一个
    - 没有请求会话 -> 新建一个（标题取首条消息）
    提前决定很关键：否则 SSE 会先把别的会话 id 告诉前端，导致下一轮又开新会话。
    """
    if not user_id:
        return None

    if requested_id:
        _, existing = db.get_conversation_messages(user_id, requested_id)
        if existing:
            return requested_id
    # 只发了图没写字时 question 是空的，标题给个可辨认的占位，
    # 否则左侧会话列表会出现一条没有标题的记录。
    title = (question or "").strip()[:30] or "（图片提问）"
    try:
        return db.create_conversation(user_id, title)
    except Exception:
        return None


def _persist_turn(user_id, conversation_id, question, answer, results_count=0, papers=None):
    """把一轮问答写进搜索历史和会话（任一失败都不影响聊天本身）

    papers 是这一轮检索到的论文（结构化 list），跟 assistant 消息一起落库。
    不存的话，切走再切回历史对话，论文卡片就没了 —— 只剩正文里那串点不开的标题。
    """
    try:
        db.save_search_history(user_id, question, results_count)
    except Exception:
        pass

    if not user_id or not conversation_id:
        return
    try:
        db.append_message(user_id, conversation_id, "user", question)
        db.append_message(user_id, conversation_id, "assistant", answer, papers=papers)
    except Exception:
        pass


# ── 聊天强制中止 ─────────────────────────────────────────────
# 前端每次聊天带一个随机 request_id；点"正在思考"时先 abort fetch，再调这个接口。
# 服务端据此取消 asyncio 任务，而不是让模型在后台继续烧 token。
_CHAT_ABORTS = {}
_CHAT_ABORT_LOCK = threading.Lock()


# ── 聊天用的常驻事件循环 ─────────────────────────────────────
# 全进程只用这一个循环（跑在专用线程里）。原因见 _run_worker 的注释：
# 每轮 asyncio.run() 一个新循环会让上一轮的 HTTP 连接跨循环关闭，
# 抛 RuntimeError: Event loop is closed，把下一轮请求打断。
_AGENT_LOOP = None
_AGENT_LOOP_LOCK = threading.Lock()


def _agent_loop():
    """取得（必要时启动）全局唯一的 agent 事件循环。"""
    global _AGENT_LOOP
    with _AGENT_LOOP_LOCK:
        # is_running()：线程要是意外死了（loop 没来得及 close），也得换一个新的，
        # 否则交上去的任务永远不会有结果 —— 请求会一直挂到 240 秒超时。
        if (_AGENT_LOOP is None or _AGENT_LOOP.is_closed()
                or not _AGENT_LOOP.is_running()):
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever,
                             name="ai4s-agent-loop", daemon=True).start()
            _AGENT_LOOP = loop
        return _AGENT_LOOP


@app.post("/api/chat/abort")
def chat_abort():
    data = request.get_json(force=True, silent=True) or {}
    request_id = str(data.get("request_id") or "").strip()[:80]
    if not request_id:
        return jsonify({"error": "request_id is required"}), 400
    with _CHAT_ABORT_LOCK:
        event = _CHAT_ABORTS.get(request_id)
        if event is not None:
            event.set()
            # 先不 pop：worker 的 finally 会清理；保留到 set 之后避免竞态。
    return jsonify({"success": True, "aborted": bool(event is not None)})


@app.post("/api/chat")
def chat():
    # 没填 Key 时不要甩一条报错给用户：发一个 need_api_key 事件，
    # 前端收到后直接弹出「填写 API Key」面板（见 index.html 的 sendMessage）。
    if not llm_layer.available():
        payload = json.dumps({"type": "need_api_key",
                              "message": "还没有填写 DeepSeek API Key"},
                             ensure_ascii=False)
        return Response(
            iter([f"data: {payload}\n\n"]),
            headers={"Content-Type": "text/event-stream; charset=utf-8",
                     "Cache-Control": "no-cache, no-transform",
                     "X-Accel-Buffering": "no"},
            mimetype="text/event-stream",
        )

    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    source_pref = (data.get("source_pref") or "all").strip().lower()

    # 图片/PDF 附件。产品要求**不保存图片**，所以全程只在内存里过一遍：
    # 解码 -> （PDF 则渲染成 PNG）-> 拼进本轮消息 -> 发完即丢。
    # 不落盘、不入库、不写日志（日志里只有体积和格式，没有 base64 内容）。
    try:
        attach_blocks, attach_notes = attachments.prepare_attachments(
            data.get("files") or [])
    except attachments.AttachmentError as e:
        return jsonify({"error": str(e)}), 400

    # 只发了图没写字也要能工作：此时给一句中性的默认指令，
    # 具体怎么处理交给系统提示词里的图片规则去判断。
    if not message and not attach_blocks:
        return jsonify({"error": "message is required"}), 400

    # 界面上选的模型。**必须白名单校验**：不能让任意字符串进请求体
    # （否则等于把"改模型名"的开关开给了外部）。
    # 不在白名单里就静默用默认模型 —— 旧前端不带这个字段，行为完全不变。
    requested_model = (data.get("model") or "").strip()
    picked_model = requested_model if llm_layer.is_selectable(requested_model) else None

    messages = []
    for m in history:
        role = m.get("role")
        content = m.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str):
            messages.append({"role": role, "content": content})

    # 前端的"文献来源"开关：以系统消息的形式带进本轮上下文。
    # 不拼进用户消息里，是因为那样会被模型当成用户说的话，也会污染保存下来的对话记录。
    if source_pref in ("cn", "en"):
        scope = "仅中文文献（source='cn'）" if source_pref == "cn" else "仅英文文献（source='en'）"
        messages.insert(0, {
            "role": "system",
            "content": f"用户当前的文献来源偏好是：{scope}。调用 search_papers 时必须使用这个 source 取值。",
        })

    messages.append({"role": "user",
                     "content": attachments.build_user_content(
                         message, attach_blocks, attach_notes)})


    user_id = session.get('user_id') if 'user_id' in session else None

    # 登录用户：本轮对话写入哪个会话（流式开始前就定下来）
    conversation_id = _resolve_conversation(user_id, _parse_conversation_id(data.get("conversation_id")), message)

    # 本轮聊天的中止通道。前端的 request_id 由 crypto.randomUUID() 生成；
    # 旧客户端不带就服务端补一个（前端仍可通过 abort fetch 停掉自己的流）。
    request_id = str(data.get("request_id") or "").strip()[:80] or uuid.uuid4().hex
    stop_event = threading.Event()
    with _CHAT_ABORT_LOCK:
        _CHAT_ABORTS[request_id] = stop_event

    # ── 深度思考开关 ──────────────────────────────────────────
    # 前端传 thinking=true/false；没传就是 None（沿用环境变量/用途默认）。
    # DeepSeek 没有 thinking_mode 这个参数：这里的"开"= 改用 deepseek-v4-pro。
    thinking_req = data.get("thinking")
    if isinstance(thinking_req, str):
        thinking_req = thinking_req.strip().lower() in ("1", "true", "on", "yes")
    if thinking_req is not None:
        thinking_req = bool(thinking_req)

    # 本轮实际用的模型：
    #   * 发图片/PDF -> 视觉用途（flash，它同时承担读图）；
    #   * 开了深度思考 -> deepseek-v4-pro；
    #   * 否则 -> 界面选的（没选就是默认 flash）。
    thinking_note = ""
    if attach_blocks:
        effective_model = llm_layer.model_for("vision")
        effective_thinking = False
        if thinking_req:
            thinking_note = "发了图片，本轮用多模态模型，不支持深度思考"
    elif thinking_req:
        effective_model = llm_layer.model_for("reason")
        effective_thinking = True
    else:
        effective_model = llm_layer._resolve_model(picked_model, "chat")
        effective_thinking = False
    effective_name = next(
        (m["name"] for m in llm_layer.MODEL_CATALOG if m["id"] == effective_model),
        effective_model)

    # 这一轮的起始时间 + 开头那条日志（记规模，不记问题原文）
    _t_start = time.time()
    try:
        runlog.info("chat", "收到问答：%d 字%s" % (
            len(message or ""), "，带 %d 个附件" % len(attach_blocks) if attach_blocks else ""),
            user_id=user_id,
            detail={"model": effective_model, "thinking": effective_thinking,
                    "source_pref": source_pref, "conversation_id": conversation_id})
    except Exception:  # noqa: BLE001
        pass

    def sse_pack(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def generate():
        # ── 为什么用"工作线程 + 队列"而不是直接 invoke ──────────────
        # 原来用 AGENT.invoke()：整段答案生成完才开始切片下发，
        # 用户要盯着一个空泡泡等十几秒（学术接口还经常要退避重试），
        # 看起来就像卡死了。改成 astream_events 之后可以：
        #   1. 一边生成一边吐字（真流式）
        #   2. 在调用工具时发一个 status 事件，让前端显示"正在检索…"
        # 但 astream_events 是 async 的，而 Flask 的生成器是同步的，
        # 所以让它在独立线程里跑，事件经 Queue 交给生成器转成 SSE。
        # 先说"本轮用哪个模型"：**不管有没有对话 id 都要发**，
        # 否则未登录用户拿不到回显（这个事件曾被塞进 if conversation_id 里，静默丢失）。
        yield sse_pack({"type": "request_id", "request_id": request_id}).encode("utf-8")
        yield sse_pack({"type": "model", "model": effective_model,
                        "name": effective_name,
                        "requested": picked_model,
                        "switched": bool(attach_blocks and picked_model),
                        "thinking": effective_thinking,
                        "thinking_requested": bool(thinking_req),
                        "thinking_note": thinking_note}).encode("utf-8")
        if conversation_id:
            yield sse_pack({"type": "conversation_id", "conversation_id": conversation_id}).encode("utf-8")

        yield sse_pack({"type": "status", "text": "正在思考…"}).encode("utf-8")

        events = queue.Queue()
        state = {"tool_used": False, "answer": "", "papers": [], "workflow": None,
                 "streamed_any": False, "reset_sent": False,
                 "muted": False, "mute_sent": False, "aborted": False,
                 # 科学计算的返回（前端"计算"卡片用它展示真实数值 + repro）
                 "calculations": [],
                 # 思考模型会先吐 reasoning_content：
                 # 用这两个字段记录"思考了多少字""是否已提示过思考中"
                 "reasoning_chars": 0, "thinking_notified": False,
                 # 没有流式分片时正文只出现在 on_chat_model_end，存这里
                 "final_text": ""}

        def suppress_json():
            """判断本轮回答是不是"模型把工具返回的结构化数据当答案吐了出来"。

            实测：移交工作流那一步，模型的回答从 `{"topic": "…", "query": "…",
            "sections": [...]}` 开始 —— 这是把工具的返回原样复读，不是给人看的话。
            改成给它散文形状的返回后仍偶发，所以在出口再兜一层：
            只要回答以 `{` 或 `[` 开头，就静音后续 token，末尾用一句人话代替。
            前端本来就有结构化渲染（方案卡 / 论文卡片），不需要正文再复读一遍。
            """
            if state["muted"]:
                return True
            head = state["answer"].lstrip()
            if head[:1] in ("{", "["):
                state["muted"] = True
            return state["muted"]

        def on_workflow_ready(info):
            state["workflow"] = info

        # 每个请求现造一个 agent：多挂"移交工作流""科学计算""写记忆"三类工具，
        # 它们要把结果回传给本次请求的 state（见 _make_research_tool 的说明）。
        #
        # 模型选择：有图片时走多模态用途（flash），否则走本轮的 effective_model
        # （开了深度思考时 effective_model 已经是 deepseek-v4-pro）。
        #
        # 带图片时把 max_tokens 抬高：读图本身需要更多输出预算。
        model_kind = "vision" if attach_blocks else "chat"
        # 发图/PDF 时一律用视觉用途的模型（flash）：哪怕界面上选的是别的。
        # 这不是"忽略用户选择"，而是能力约束 —— 本轮要读图。
        # 界面上会显示一行"本轮：DeepSeek-V4.1-Flash（因为发了图片）"，不是悄悄换。
        use_model = (None if attach_blocks else effective_model)
        # streaming=True 走真流式；DeepSeek 的 pro 也支持流式（reasoning_content
        # 先到、正文随后到），所以开深度思考不再强制整段返回。
        model_kwargs = {"temperature": 0, "streaming": True,
                        "thinking": bool(effective_thinking)}
        if attach_blocks:
            model_kwargs["max_tokens"] = attachments.VISION_MIN_MAX_TOKENS
        elif effective_thinking:
            # 推理内容占用同一个 max_tokens 预算：额度给小了会出现"正文为空"，
            # 看起来像模型没回答。
            model_kwargs["max_tokens"] = min(
                llm_layer.MAX_TOKENS_CAP,
                int(os.getenv("DEEPSEEK_THINKING_MAX_TOKENS") or "4096"))
        chat_tools = [
            search_papers, get_paper_detail,
            list_science_tools, run_science_tool,
            _make_research_tool(on_workflow_ready, user_id),
        ]
        if user_id:
            chat_tools.append(agent_tools.make_memory_tool(user_id))
        agent = create_agent(
            model=llm_layer.chat_model(model_kind, model=use_model, **model_kwargs),
            tools=chat_tools,
            system_prompt=system_prompt_for_chat(user_id, message),
        )

        async def run_agent():
            try:
                async for ev in agent.astream_events({"messages": messages}, version="v2"):
                    kind = ev.get("event")

                    if kind == "on_tool_start":
                        # 工具调用前模型常会先说一句"我先搜索一下"（还可能是英文）。
                        # 那句话已经按"真流式"发到界面上了，所以这里发一个 reset
                        # 让前端清空气泡；之后模型吐的才是答案。
                        state["tool_used"] = True
                        state["answer"] = ""
                        if not state["reset_sent"]:
                            state["reset_sent"] = True
                            events.put(("reset", None))
                        name = ev.get("name") or ""
                        events.put(("status", _tool_status(name)))

                    elif kind == "on_tool_end":
                        output = (ev.get("data") or {}).get("output")
                        content = getattr(output, "content", None)
                        if content is None and isinstance(output, str):
                            content = output
                        papers = _papers_from_tool_output(content)
                        if papers:
                            state["papers"].extend(papers)
                            events.put(("papers", list(state["papers"])))
                        # 计算类工具的返回也带给前端（"科学计算"卡片用它展示真实数值）
                        calc = _calc_from_tool_output(content)
                        if calc:
                            state["calculations"].append(calc)
                            events.put(("calculation", list(state["calculations"])))
                        events.put(("status", "正在阅读检索结果并生成回答…"))

                    elif kind == "on_chat_model_stream":
                        chunk = (ev.get("data") or {}).get("chunk")
                        # 只把真正的"回答文字"当正文。tool_call_chunks 非空说明
                        # 这一段是**工具参数**（模型在拼 {"goal": "..."}），
                        # 不是给用户看的内容 —— 实测它一度被当成正文发出去。
                        if getattr(chunk, "tool_call_chunks", None):
                            continue

                        # 推理模型（deepseek-v4-pro）会先吐一大段 reasoning_content，
                        # 正文要等它结束才开始。推理阶段可能持续十几秒，
                        # 这期间 content 一直是空的 —— 如果只读 content，
                        # 界面上就是一个空白泡泡转十几秒，看起来完全像卡死。
                        # 所以这里把"思考中"推给前端，让用户知道它在干活。
                        reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get(
                            "reasoning_content")
                        if reasoning:
                            state["reasoning_chars"] = state.get("reasoning_chars", 0) + len(
                                str(reasoning))
                            # 只在第一片时报一次，避免每个分片都刷状态
                            if not state.get("thinking_notified"):
                                state["thinking_notified"] = True
                                events.put(("status", "模型正在思考…"))

                        text = getattr(chunk, "content", None)
                        if text:
                            # 正文一开始就把状态切回"生成回答"，否则会一直停在
                            # "思考中"。用"是不是第一次拿到正文"来判断，而不是去
                            # 猜 langchain 给分片起的 type 名字。
                            if state.get("thinking_notified"):
                                state["thinking_notified"] = False
                                events.put(("status", "正在生成回答…"))
                            state["answer"] += text
                            # 一律边生成边发（真流式）；被静音的情况除外
                            if not suppress_json():
                                state["streamed_any"] = True
                                events.put(("token", text))

                    elif kind == "on_chat_model_end":
                        # 没有流式分片时，正文只在这里出现一次：
                        # 存下最后一个非空输出，收尾时当答案用。
                        # 流式时这里也会有内容，但那时 state["answer"] 已经拼接好了，
                        # 收尾处只在"一个分片都没收到"时才用它，不会重复。
                        text = _text_from_message((ev.get("data") or {}).get("output"))
                        if text:
                            state["final_text"] = text

                events.put(("done", None))
            except asyncio.CancelledError:
                # 用户点了「中止」：必须让它继续往上冒，
                # run_agent_with_watch 靠它把 state["aborted"] 置位并推 aborted 事件。
                raise
            except BaseException as e:  # noqa: BLE001 - 异常要送回生成器，不能只留在子线程里
                # 这里必须接 **BaseException**：现在所有请求共用同一个常驻事件循环，
                # 一个 KeyboardInterrupt / SystemExit 从任务里逃出去会把整个循环带走，
                # 之后**所有**请求都会挂到 240 秒超时（实测：一次崩溃 = 全家挂死）。
                #
                # 前端只看得到一句话，服务端必须留一份完整栈 —— 否则
                # 「RuntimeError: Event loop is closed」这种只有一行字的信息
                # 根本没法定位是哪个调用炸的（实测排查一次要重跑好几轮）。
                try:
                    traceback.print_exc()
                    sys.stderr.write(f"[chat] 本轮失败：{type(e).__name__}: {e}\n")
                    sys.stderr.flush()
                except Exception:  # noqa: BLE001 - 日志失败不能影响把错误送给前端
                    pass
                events.put(("error", f"{type(e).__name__}: {str(e)[:500]}"))

        async def run_agent_with_watch():
            """跑 agent，同时监听 stop_event；用户点了「中止」就 cancel 掉任务。

            之前是直接 asyncio.run(run_agent())，客户端断开了后台还在继续烧 token。
            这里用 asyncio 任务包一层，abort 时 task.cancel()，让 HTTP 流/模型请求
            跟着取消。run_agent 内部的 except 会先把 CancelledError 原样抛出去
            （它继承 BaseException，不会被兜底分支吞掉），所以取消能正常冒到这里。
            """
            task = asyncio.create_task(run_agent())

            async def watch():
                while not stop_event.is_set():
                    if task.done():
                        return
                    await asyncio.sleep(0.1)
                if not task.done():
                    task.cancel()

            watcher = asyncio.create_task(watch())
            try:
                await task
            except asyncio.CancelledError:
                state["aborted"] = True
                events.put(("aborted", None))
            finally:
                watcher.cancel()
                with _CHAT_ABORT_LOCK:
                    _CHAT_ABORTS.pop(request_id, None)

        def _run_worker():
            """在常驻事件循环里跑这一轮，并把任何异常都变成终结事件。

            为什么要用常驻循环而不是 asyncio.run()：
            每轮新建一个循环时，**上一轮**留下的 HTTP 连接会在下一轮的新循环
            里被 GC 回收，回收时它会去 close 自己那个已经关闭的旧循环，
            Windows 的 proactor 传输层直接抛 `RuntimeError: Event loop is closed`
            —— 而这一刻恰好在一个正在跑的任务里，于是把**下一轮请求**打断。
            实测隔一轮挂一次（症状：这一轮没有论文、或直接"抱歉，未能获取到回复"）。

            另外这层 try 是兜底：只有 run_agent 内部的 except 不够，
            CancelledError / 循环本身的异常都是 BaseException，抛出去没人接，
            队列就会一直空着，生成器只能干等 240 秒超时。
            """
            try:
                future = asyncio.run_coroutine_threadsafe(
                    run_agent_with_watch(), _agent_loop())
                future.result()
            except BaseException as e:  # noqa: BLE001 - 后台线程不能静默死掉
                events.put(("error", f"后台任务异常：{type(e).__name__}: {str(e)[:300]}"))

        threading.Thread(target=_run_worker, daemon=True).start()

        # 主线程只做一件事：把队列里的事件转成 SSE。带总超时，避免永久挂住
        deadline = time.time() + 240
        # 心跳：SSE 注释行（客户端会忽略内容）。作用是把"连接还活着"变成可观测的
        # 字节流 —— 前端据此区分"模型还在想"和"连接已经断了"，不会再无限等下去。
        try:
            ping_every = max(1.0, float(os.getenv("CHAT_HEARTBEAT_SECONDS") or 10))
        except ValueError:
            ping_every = 10.0
        last_ping = time.time()
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                yield sse_pack({"type": "error", "message": "检索超时（超过 240 秒），请重试"}).encode("utf-8")
                break
            try:
                kind, value = events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if time.time() - last_ping >= ping_every:
                    last_ping = time.time()
                    yield b": ping\n\n"
                continue
            last_ping = time.time()

            if kind == "aborted":
                state["aborted"] = True
                yield sse_pack({"type": "aborted"}).encode("utf-8")
                break
            if kind == "done":
                # 极窄竞态：用户点中止时任务刚好跑完。以 stop_event 为准，
                # 不能把这一轮当成正常完成去落库。
                if stop_event.is_set():
                    state["aborted"] = True
                    yield sse_pack({"type": "aborted"}).encode("utf-8")
                break
            if kind == "error":
                yield sse_pack({"type": "error", "message": value}).encode("utf-8")
                break
            if kind == "status":
                yield sse_pack({"type": "status", "text": value}).encode("utf-8")
            elif kind == "papers":
                yield sse_pack({"type": "papers", "papers": value}).encode("utf-8")
            elif kind == "reset":
                yield sse_pack({"type": "reset"}).encode("utf-8")
            elif kind == "calculation":
                yield sse_pack({"type": "calculation",
                                "calculations": value}).encode("utf-8")
            elif kind == "token":
                yield sse_pack({"type": "token", "token": value}).encode("utf-8")

        aborted = state["aborted"] or stop_event.is_set()
        if aborted:
            # 中止的轮次不落库、不写记忆、不弹工作流按钮；
            # 前端收到 aborted 后会把气泡改成「已中止」。
            with _CHAT_ABORT_LOCK:
                _CHAT_ABORTS.pop(request_id, None)
            yield sse_pack({"type": "done", "aborted": True}).encode("utf-8")
            return

        # 某些模型/网关在"推理模式"下可能不产生 on_chat_model_stream 分片，
        # 正文只在 on_chat_model_end 里出现一次。不把它接过来，整轮就一个 token
        # 都收不到，最后只能回一句"抱歉，未能获取到回复"。这里做兜底。
        if not state["answer"] and state.get("final_text"):
            state["answer"] = state["final_text"]
            suppress_json()     # 复用同一套"别把结构化数据当正文"的判断

        answer = state["answer"] or "抱歉，未能获取到回复"

        # 被静音的情况（模型把结构化数据当答案复读了）：用一句人话代替，
        # 并把补发的 token 也改成这句 —— 否则用户会看到一坨花括号。
        if state["muted"]:
            if state["workflow"]:
                answer = ("已经为这个题目规划好一条 AI4S 科研工作流（检索 → 精读 → 假设 → "
                          "实验设计 → 科学计算 → 复现验证 → 综述 → 报告）。"
                          "点下面的「开始执行」就可以跑，中途能中断，之后可以从断点继续。")
            else:
                answer = "已获取到结果，结构化内容见下方卡片。"

        # 只在"一个 token 都没发出去"（模型没吐内容）或"被静音"（要换成一句人话）
        # 时补发；正常情况正文已经边生成边发完了，再补就会重复两遍。
        if state["muted"] or not state["streamed_any"]:
            for i in range(0, len(answer), 3):
                yield sse_pack({"type": "token", "token": answer[i:i + 3]}).encode("utf-8")

        if user_id:
            # results_count 记的是"这一轮检索到几篇论文"，不是消息条数；
            # papers 一并落库，历史对话里才能把卡片（和原文链接）原样画回来
            _persist_turn(user_id, conversation_id, message, answer,
                          len(state["papers"]), state["papers"])

        # 模型决定"这事该交给工作流"时，把方案和入口发给前端，
        # 由界面渲染成「开始执行」按钮（执行是分钟级的，不适合塞在聊天流里）。
        if state["workflow"]:
            yield sse_pack({"type": "workflow_ready", **state["workflow"]}).encode("utf-8")

        yield sse_pack({"type": "done"}).encode("utf-8")

        # 一轮结束：记"规模"而不是"内容" —— 问题多长、答多长、几篇文献、耗时多少。
        # 这样管理员能看出负载与异常，又读不到别人问了什么。
        try:
            runlog.info("chat", "问答完成：%d 字问题 → %d 字回答，%d 篇文献" % (
                len(message or ""), len(answer or ""), len(state["papers"])),
                user_id=user_id,
                detail={"model": effective_model,
                        "ms": round((time.time() - _t_start) * 1000),
                        "tools": bool(state["tool_used"]),
                        "streamed": bool(state["streamed_any"]),
                        "workflow": bool(state["workflow"]),
                        "conversation_id": conversation_id})
        except Exception:  # noqa: BLE001
            pass

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        generate(),
        headers=headers,
        mimetype="text/event-stream",
        direct_passthrough=True,
    )


@app.get("/api/sources")
def list_sources():
    """当前可用的文献数据源（前端据此显示来源开关的提示）。"""
    return jsonify({"success": True, "sources": providers.source_status()})


@app.get("/api/paper/<path:paper_id>")
def paper_detail(paper_id):
    """按论文 id 取详情。

    单独开这个接口而不是让前端再走一轮 LLM：点"详情"只是取数据，
    不该花一次模型调用，也不该被大模型的幻觉影响。
    """
    try:
        payload = json.loads(providers.get_detail(paper_id))
    except Exception as e:
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500

    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "返回内容无法解析"}), 502
    if payload.get("error"):
        return jsonify({"success": False, "error": payload.get("message") or payload["error"],
                        "retryable": payload.get("retryable", False)}), 502
    # 成功时 payload 就是论文本身；没找到时是 {"message": ..., "paper": None}
    # 所以判断依据是"有没有 title"，不能去看有没有 paper 键
    if not payload.get("title"):
        return jsonify({"success": False, "error": payload.get("message", "未找到该论文")}), 404

    return jsonify({"success": True, "paper": payload})


@app.get("/api/paper/<path:paper_id>/pdf")
def paper_pdf(paper_id):
    """把论文的开放获取 PDF 转发下来，并**由我们用可读的文件名**保存。

    为什么要做这个转发：那些期刊的 PDF 直链是 OJS 生成的
    （`48962-Article Text-60922-1-10-20241217.pdf`），下载到本地完全看不出是哪篇。
    而浏览器会**忽略跨域链接上的 HTML download 属性**（这是安全限制，改不了），
    所以只能由同源的我们自己把文件取回来、再设置 Content-Disposition。

    安全上有意收紧：**只使用我们自己数据源里记录的 openAccessPdf**，
    不接受任何调用方传入的 URL —— 否则这就成了一个可以被拿来抓任意地址的代理。
    """
    try:
        paper = json.loads(providers.get_detail(paper_id))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"获取论文信息失败：{type(e).__name__}"}), 500

    if not isinstance(paper, dict) or paper.get("error"):
        return jsonify({"error": (paper or {}).get("message", "未找到该论文")}), 404
    # 成功时 payload 就是论文本身；没找到时是 {"message":..., "paper": None}
    if not paper.get("title"):
        return jsonify({"error": paper.get("message", "未找到该论文")}), 404

    url = (paper.get("openAccessPdf") or "").strip()
    if not url:
        return jsonify({"error": "该论文没有可下载的全文 PDF 直链，请点「阅读」打开来源页"}), 404
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "全文链接格式无效"}), 400

    filename = _pdf_filename(paper)
    try:
        upstream = requests.get(
            url, stream=True, timeout=(10, 120),
            headers={"User-Agent": S2_USER_AGENT, "Accept": "application/pdf,*/*"},
        )
    except requests.RequestException as e:
        return jsonify({"error": f"下载失败：{type(e).__name__}，请稍后重试或点「阅读」"}), 502

    if upstream.status_code != 200:
        code = upstream.status_code
        upstream.close()
        return jsonify({"error": f"来源站返回 {code}，请点「阅读」打开原页面"}), 502

    ctype = (upstream.headers.get("Content-Type") or "").lower()
    if "pdf" not in ctype and "octet-stream" not in ctype:
        upstream.close()
        return jsonify({
            "error": f"这个链接不是 PDF（{ctype or '未知类型'}），请点「阅读」打开原页面",
        }), 415

    # Content-Disposition 里非 ASCII 文件名要用 RFC 5987 的 filename*，
    # 同时给一个 ASCII 的 filename 兜底（老浏览器只认前者）
    disposition = (
        'attachment; filename="paper.pdf"; '
        "filename*=UTF-8''" + quote(filename)
    )

    def stream():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(stream()),
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": disposition,
            # 给前端/调试看最终用了什么文件名
            "X-Paper-Filename": quote(filename),
            "Cache-Control": "no-store",
        },
        direct_passthrough=True,
    )


# ═══════════════════════════════════════════════════════════════
#  路由：科研工作流（命题要求的自规划 + 状态管理 + 中断恢复）
# ═══════════════════════════════════════════════════════════════

# 正在跑的工作流 -> 停止标志。中断接口把它置 True，编排器每步结束会检查。
_RUNNING = {}
_RUNNING_LOCK = threading.Lock()


def _wf_state_payload(wf_row, runner=None):
    """把库里的工作流整理成前端要的结构。"""
    steps = []
    by_key = wf_row.get("steps") or {}
    for s in wf.STEPS:
        cur = by_key.get(s["key"]) or {}
        item = {
            "key": s["key"], "title": s["title"], "core": s["core"],
            "detail_text": s["detail"],
            "status": cur.get("status") or "pending",
            "progress": cur.get("detail") or "",
            "error": cur.get("error") or "",
            "updated_at": cur.get("updated_at") or "",
        }
        steps.append(item)
    out = {
        "id": wf_row["id"],
        "goal": wf_row["goal"],
        "status": wf_row["status"],
        "plan": wf_row.get("plan") or {},
        "steps": steps,
        "created_at": wf_row.get("created_at"),
        "updated_at": wf_row.get("updated_at"),
        # 多智能体分工表：刷新后仍能显示"谁做了什么"
        "team": agents.team_overview(),
    }
    ctx = wf_row.get("context") or {}
    if ctx:
        if ctx.get("papers"):
            out["papers"] = ctx["papers"]
        if ctx.get("stats"):
            out["stats"] = ctx["stats"]
        if ctx.get("synth"):
            out["synth"] = ctx["synth"]
        # AI4S 闭环产物：假设 / 实验 / 计算 / 验证 / 迭代记录
        for key in ("hypothesis", "experiment", "computation", "verify"):
            if ctx.get(key):
                out[key] = ctx[key]
        if ctx.get("iterations"):
            out["iterations"] = ctx["iterations"]
        if ctx.get("plan"):
            out.setdefault("plan", ctx["plan"])
    if wf_row.get("report_md"):
        out["report"] = wf_row["report_md"]
    return out


@app.post("/api/workflow")
@login_required
def run_workflow():
    """启动（或续跑）一条科研工作流。

    SSE 事件：
        plan     规划结果（topic/query/source/limit/focus/sections）
        step     某一步的状态变化：{key, status, detail, error}
        snapshot 阶段性快照（文献数、统计、综述、报告）
        done     结束：{status, workflow_id}
        error    出错
    """
    if not llm_layer.available():
        return jsonify({"error": "还没有填写 DeepSeek API Key",
                        "need_api_key": True}), 400

    data = request.get_json(force=True, silent=True) or {}
    goal = (data.get("goal") or "").strip()
    resume_id = data.get("workflow_id")
    papers_override = data.get("papers")      # 人工修正后的文献列表
    # 可选：用户直接给实验数据（CSV/制表符/谱图两列数字/SMILES 列表）。
    # 给了就优先在"算"这一步用它做真实计算，而不是只做文献计量。
    dataset = (data.get("dataset") or "").strip()
    dataset_kind = (data.get("dataset_kind") or "table").strip().lower()
    if len(dataset) > 200_000:
        return jsonify({"error": "数据集过大（上限 200KB），请只放需要分析的列"}), 400

    if not goal and not resume_id:
        return jsonify({"error": "请填写研究目标"}), 400

    # 必须在进入生成器之前把 session 读出来：生成器是在请求上下文之外被迭代的，
    # 里面再访问 session 会抛 "Working outside of request context"（踩过这个坑）。
    user_id = session.get("user_id")

    def sse_pack(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    # 开跑前留一条：续跑还是新建、目标多长、有没有带数据集
    try:
        runlog.info("workflow", ("续跑工作流 #%s" % resume_id) if resume_id else "新建工作流",
                    user_id=user_id,
                    detail={"goal_len": len(goal or ""),
                            "dataset": (dataset_kind if dataset else None),
                            "papers_override": len(papers_override or [])})
    except Exception:  # noqa: BLE001
        pass

    def generate():
        # 这几个只在"续跑"分支里赋值，但后面无条件使用 ——
        # 必须先给默认值，否则新工作流会抛 UnboundLocalError（同一个坑踩了两次）。
        ctx_prev = {}
        state_prev = {}
        start_from = wf.STEP_KEYS[0]
        workflow_goal = goal

        # ── 已有工作流：续跑 ──────────────────────────────────
        if resume_id:
            row = db.get_workflow(int(resume_id), user_id)
            if not row:
                yield sse_pack({"type": "error", "message": "工作流不存在"}).encode("utf-8")
                yield sse_pack({"type": "done", "status": "failed"}).encode("utf-8")
                return
            plan = row.get("plan") or wf.plan_workflow(row["goal"])
            ctx_prev = row.get("context") or {}
            # 把上一轮各步骤的状态铺回去，快照才是完整时间线
            # （否则续跑后 search/extract 会显示成 pending，看起来像白跑了）
            state_prev = {k: {"status": (v or {}).get("status", "pending"),
                              "detail": (v or {}).get("detail", "")}
                          for k, v in (row.get("steps") or {}).items()}
            workflow_id = row["id"]
            # 注意：不要在这里写 goal = row["goal"]。
            # 一旦在 generate() 里给 goal 赋值，Python 就把它当成局部变量，
            # 于是同一个函数里 else 分支的 plan_workflow(goal) 会抛
            # UnboundLocalError（踩过这个坑）。续跑用 row["goal"] 就够了。
            workflow_goal = row["goal"]
            # 已完成的步骤：除了要把产物带回来，也让编排器从最早未完成的那步接着跑
            done_keys = [k for k, v in (row.get("steps") or {}).items()
                         if (v or {}).get("status") == "done"]
            start_from = "search"
            for s in wf.STEPS:
                if s["key"] not in done_keys:
                    start_from = s["key"]
                    break
            else:
                start_from = wf.STEP_KEYS[-1]
            yield sse_pack({"type": "plan", "plan": plan, "workflow_id": workflow_id,
                            "resumed": True, "start_from": start_from}).encode("utf-8")
        else:
            # ── 新工作流：自规划（带上长周期记忆，同一课题可继承上次经验）──
            yield sse_pack({"type": "step", "key": "plan", "status": "running",
                            "title": "规划研究方案", "core": "—",
                            "detail": "正在理解研究目标并规划步骤"}).encode("utf-8")
            hint = ""
            try:
                hint = memory.context_pack(user_id, goal, limit=5, budget_chars=700)
            except Exception:  # noqa: BLE001 - 记忆不可用不影响规划
                hint = ""
            plan = (wf.plan_workflow(goal, memory_hint=hint) if hint
                    else wf.plan_workflow(goal))
            workflow_id = db.create_workflow(user_id, goal, plan)
            ctx_prev = {}
            yield sse_pack({"type": "plan", "plan": plan, "workflow_id": workflow_id,
                            "memory_used": bool(hint),
                            "team": agents.team_overview()}).encode("utf-8")
            yield sse_pack({"type": "step", "key": "plan", "status": "done",
                            "title": "规划研究方案", "core": "—",
                            "detail": plan.get("plan") or "已确定检索方案"}).encode("utf-8")

        with _RUNNING_LOCK:
            _RUNNING[workflow_id] = False

        def should_stop():
            with _RUNNING_LOCK:
                return bool(_RUNNING.get(workflow_id))

        # ── 为什么改成"工作线程 + 队列" ──────────────────────────
        # 原来这里是同步跑完 runner.run()，再把事件列表一次性回放。
        # 实测后果（profiler 的时间线）：整条流水线 12.7s，面板在 12.7s 时
        # **一次性收到所有事件** —— 前 12.7 秒界面上只有一句"工作流执行中…"，
        # 看起来就是"卡住了"。真实耗时其实没变，但主观感受差很多。
        # 现在和 /api/chat 用同一套办法：编排器跑在独立线程里，
        # 每步状态变化立刻经 Queue 推给生成器，用户能实时看到走到哪一步。
        events = queue.Queue()
        error_box = {}

        def on_step(key, status, detail):
            # interrupted 也要落库并推给前端，否则界面上只会看到"停在那一步"，
            # 不知道是中断了还是卡住了。
            db.set_workflow_step(workflow_id, key, status, detail,
                                 detail if status == "failed" else "")
            ev = {"type": "step", "key": key, "status": status, "detail": detail}
            if runner_ref.get("runner"):
                ev["iteration"] = runner_ref["runner"].iteration
            events.put(ev)
            # 验证步骤一结束就把结论推出去：前端要立刻显示 pass/revise/reject
            # 以及"是否进入下一轮迭代"，而不是等最后快照一次性揭晓。
            if key == "verify" and status == "done" and runner_ref.get("runner"):
                r = runner_ref["runner"]
                v = (r.ctx.get("verify") or {})
                events.put({
                    "type": "verdict", "verdict": v.get("verdict"),
                    "reason": v.get("reason"), "replay": v.get("replay"),
                    "fixes": (v.get("fixes") or [])[:5],
                    "iteration": r.iteration, "max_iterations": r.max_iterations,
                    "iterations": list(r.iterations),
                })

        # 注意：人工修正的文献要写进 ctx_prev 本身。
        # 之前写的是 ctx_prev = dict(...) 的副本，等于白改 ——
        # 续跑时编排器从 synthesize 开始，会直接复用旧的 papers，改动被丢掉。
        if papers_override:
            ctx_prev["papers"] = papers_override
        runner_ref = {}
        runner = wf.WorkflowRunner(plan, on_step=on_step, should_stop=should_stop,
                                   dataset=dataset, dataset_kind=dataset_kind,
                                   user_id=user_id)
        runner_ref["runner"] = runner
        # 续跑时把上一轮的迭代记录接上，避免报告里迭代表从零开始
        if ctx_prev.get("iterations"):
            runner.iterations = list(ctx_prev["iterations"])
            runner.iteration = int(ctx_prev.get("iteration") or 0)
        db.update_workflow_status(workflow_id, "running")

        def run_pipeline():
            try:
                runner.run(start_from=start_from, previous=ctx_prev,
                           previous_state=state_prev)
            except Exception as e:  # noqa: BLE001 - 异常要送回生成器
                error_box["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                db.update_workflow_status(workflow_id, "failed")
            finally:
                events.put(None)      # 结束哨兵

        threading.Thread(target=run_pipeline, daemon=True).start()

        # 边跑边推。每收到一步就立刻下发，前端面板随即更新。
        while True:
            ev = events.get()
            if ev is None:
                break
            yield sse_pack(ev).encode("utf-8")

        if error_box.get("error"):
            yield sse_pack({"type": "error", "message": error_box["error"]}).encode("utf-8")

        snap = runner.snapshot()
        ctx = {
            "papers": runner.ctx.get("papers") or [],
            "stats": runner.ctx.get("stats"),
            "synth": runner.ctx.get("synth"),
            # AI4S 闭环的产物要一起落库：刷新/续跑/导出报告都要用到
            "hypothesis": runner.ctx.get("hypothesis"),
            "experiment": runner.ctx.get("experiment"),
            "computation": runner.ctx.get("computation"),
            "verify": runner.ctx.get("verify"),
            "iterations": runner.iterations,
            "iteration": runner.iteration,
            "plan": plan,
        }
        report = (runner.ctx.get("format") or {}).get("report", "")
        db.save_workflow_result(workflow_id, ctx, report)

        failed = [s for s in snap["steps"] if s["status"] == "failed"]
        interrupted = any(s["status"] == "interrupted" for s in snap["steps"])
        final = "interrupted" if interrupted else ("partial" if failed else "done")
        db.update_workflow_status(workflow_id, final)

        # 把结论性内容归档进长周期记忆：下次做同一课题时，规划会带上这些经验。
        # 只存结论（假设/验证结论/待解问题/研究空白），不存文献列表。
        memory_archived = 0
        if user_id and not interrupted:
            try:
                goal_for_memory = plan.get("topic") or goal or workflow_goal or ""
                got = memory.remember_workflow(
                    user_id, goal_for_memory, workflow_id=workflow_id,
                    hypothesis=runner.ctx.get("hypothesis"),
                    experiment=runner.ctx.get("experiment"),
                    verify=runner.ctx.get("verify"),
                    synth=runner.ctx.get("synth"),
                    papers=runner.ctx.get("papers"),
                )
                memory_archived = len([g for g in got if g.get("id")])
            except Exception:  # noqa: BLE001 - 记忆归档失败不该影响结果返回
                memory_archived = 0

        with _RUNNING_LOCK:
            _RUNNING.pop(workflow_id, None)

        yield sse_pack({"type": "snapshot", "team": agents.team_overview(),
                        "memory_archived": memory_archived, **snap}).encode("utf-8")
        yield sse_pack({"type": "done", "status": final,
                        "workflow_id": workflow_id}).encode("utf-8")

        # 工作流收尾：只记"规模 + 结论"，不记研究目标原文
        try:
            runner_obj = locals().get("runner")
            ctx = runner_obj.ctx if runner_obj is not None else {}
            verify = ctx.get("verify") or {}
            runlog.log("WARN" if final == "failed" else "INFO", "workflow",
                       "工作流结束：%s（%d 篇文献）" % (final, len(ctx.get("papers") or [])),
                       user_id=user_id,
                       detail={"workflow_id": workflow_id,
                               "status": final,
                               "verdict": verify.get("verdict"),
                               "iterations": len(ctx.get("iterations") or []),
                               "resumed": bool(resume_id),
                               "goal_len": len(goal or "")})
        except Exception:  # noqa: BLE001
            pass

    return Response(generate(), headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }, mimetype="text/event-stream", direct_passthrough=True)


@app.get("/api/workflow/<int:workflow_id>")
@login_required
def get_workflow(workflow_id):
    """读工作流状态（仅本人）。

    这里**故意不做"读不到就按 id 兜底再读一次"** ——
    工作流 id 是自增的（1、2、3…），任何人都能猜。
    兜底那次查询不带 user_id，等于匿名就能把别人整份报告、检索式、
    文献列表读走。这和之前"整目录托管 static 导致 /app.py 可下载"
    是同一类漏洞，所以四个工作流接口统一要求登录且校验归属。
    """
    row = db.get_workflow(workflow_id, session["user_id"])
    if not row:
        return jsonify({"success": False, "error": "工作流不存在"}), 404
    return jsonify({"success": True, "workflow": _wf_state_payload(row)})


@app.post("/api/workflow/<int:workflow_id>/interrupt")
@login_required
def interrupt_workflow(workflow_id):
    """请求中断。

    编排器在每次步骤内汇报进度时都会查这个标志，所以批处理中的长步骤
    （分批抽取、多节综述）也能被打断；已经跑完的步骤保持 done 不丢，
    中断标记落在"本来要开始却没开始"的那一步上，续跑即以它为起点。
    """
    with _RUNNING_LOCK:
        if workflow_id in _RUNNING:
            _RUNNING[workflow_id] = True
            return jsonify({"success": True, "message": "已请求中断，将在当前步骤结束后停止"})
    return jsonify({"success": True, "message": "该工作流当前没有在运行"})


@app.delete("/api/workflow/<int:workflow_id>")
@login_required
def remove_workflow(workflow_id):
    if db.delete_workflow(workflow_id, session["user_id"]):
        return jsonify({"success": True, "message": "已删除"})
    return jsonify({"success": False, "error": "工作流不存在"}), 404


# ═══════════════════════════════════════════════════════════════
#  路由：AI4S 能力面（模型栈 / MCP 工具 / 科研团队 / 技能 / 记忆）
# ═══════════════════════════════════════════════════════════════
#
# 这一组接口存在的意义：让"用了 DeepSeek 大模型、真的走 MCP、真的多智能体分工、
# 真的有长周期记忆"这几件事**可以被当场验证**，而不是只写在 PPT 里。
# 全部只读（记忆除外），不需要登录也能看 —— 评审打开页面就能自证。

@app.get("/api/models")
def api_models():
    """当前模型栈（DeepSeek 的两个模型在四个用途上的分配）。

    `?probe=1` 会真的向 DeepSeek /models 打一次，用来证明"确实连的是 DeepSeek"。
    默认不探活：启动/刷新页面时不该发网络请求。
    """
    info = llm_layer.model_info()
    if (request.args.get("probe") or "").strip() in ("1", "true", "yes"):
        info["probe"] = llm_layer.probe()
    # ?check=1 会按用途各真打一次，回答"这个 token 到底能用哪些模型"
    # （/models 只说明平台上有哪些，不代表 token 有权限调）
    if (request.args.get("check") or "").strip() in ("1", "true", "yes"):
        info["check"] = llm_layer.check_models()
    # catalog：界面上那个"模型下拉列表"的数据源（默认项 + 每个模型的说明）
    return jsonify({"success": True,
                    "models": {**info, "catalog": llm_layer.model_catalog()},
                    "team": agents.team_overview()})


@app.get("/api/memory")
@login_required
def api_memory_list():
    """长周期研究记忆：列表 + 统计。query 参数可用于看召回效果。"""
    user_id = session["user_id"]
    query = (request.args.get("q") or "").strip()
    scope = (request.args.get("scope") or "").strip() or None
    try:
        items = memory.recall(user_id, query=query, scope=scope, limit=50)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"success": True, "items": items,
                    "stats": memory.stats(user_id)})


@app.post("/api/memory")
@login_required
def api_memory_add():
    """手动写一条记忆（前端"记住这条"按钮用）。"""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"success": False, "error": "text 不能为空"}), 400
    res = memory.remember(session["user_id"], text,
                          scope=str(data.get("scope") or ""),
                          kind=str(data.get("kind") or "note"),
                          tags=data.get("tags") or [])
    return jsonify({"success": bool(res.get("id")), **res})


@app.delete("/api/memory")
@login_required
def api_memory_delete():
    """删一条记忆（id 参数）或清空（clear=1）。"""
    if (request.args.get("clear") or "") in ("1", "true", "yes"):
        n = memory.clear(session["user_id"])
        return jsonify({"success": True, "deleted": n})
    raw = request.args.get("id")
    try:
        mid = int(raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "请提供合法的 id"}), 400
    return jsonify({"success": memory.forget(mid, session["user_id"])})


# ═══════════════════════════════════════════════════════════════
#  朗读（TTS）
# ═══════════════════════════════════════════════════════════════
# 三件事：告诉前端「能读什么」（options）、合成一段（POST /api/tts）、
# 把缓存文件吐回去（audio）。合成结果按 sha1(音色+语速+正文) 落盘，
# 同一段话第二次点按钮是 0 毫秒 —— 长回答重听不用再等。
@app.get("/api/tts/options")
def api_tts_options():
    """前端判断"要不要显示朗读按钮"、以及设置里那几个音色能不能选。

    故意不要求登录：登录页还没进会话时也要能决定按钮的可见性，
    这个接口只暴露"装没装引擎"，不暴露任何用户数据。
    """
    try:
        return jsonify(tts_layer.options())
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "enabled": False,
                        "error": f"{type(e).__name__}: {e}"}), 500


@app.post("/api/tts")
@login_required
def api_tts():
    """合成一段朗读音频。请求体：{text, voice?, rate?}"""
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text") or "")
    if not text.strip():
        return jsonify({"success": False, "error": "没有可朗读的文字"}), 400
    started = time.time()
    try:
        res = tts_layer.synthesize(text,
                                   voice=str(data.get("voice") or ""),
                                   rate=str(data.get("rate") or ""))
    except tts_layer.TtsError as e:
        runlog.log("WARN", "system", "朗读失败：%s" % e,
                   user_id=session.get("user_id"), detail={"chars": len(text)})
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"success": False,
                        "error": f"朗读失败：{type(e).__name__}: {e}"}), 500

    name = os.path.basename(res["path"])
    # 只记规模，不记正文：日志是给管理员看"系统跑得怎么样"，不是内容仓库
    runlog.log("INFO", "system",
               "朗读 %d 字（%s%s，%d ms）" % (res["chars"], res["voice"],
                                             "，缓存" if res["cached"] else "",
                                             int((time.time() - started) * 1000)),
               user_id=session.get("user_id"))
    return jsonify({"success": True, "id": name,
                    "url": "/api/tts/audio/" + quote(name),
                    "cached": res["cached"], "engine": res["engine"],
                    "voice": res["voice"], "rate": res["rate"],
                    "chars": res["chars"], "ms": res["ms"]})


@app.get("/api/tts/audio/<name>")
@login_required
def api_tts_audio(name: str):
    """取合成好的音频。

    文件名必须严格是 `<20 位十六进制>.<mp3|wav>` —— 这是自己生成的命名规则，
    把它当白名单，就不存在 `../` 或目录穿越的问题。
    """
    if not re.fullmatch(r"[0-9a-f]{20}\.(?:mp3|wav)", name or ""):
        return jsonify({"success": False, "error": "非法的音频名"}), 400
    resp = send_from_directory(tts_layer.CACHE_DIR, name, conditional=True)
    # 同一段文本永远对应同一个文件（内容寻址），可以让浏览器放心缓存
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/api/health")
def health():
    return jsonify({"status": "ok",
                    "backend": llm_layer.backend() or "unconfigured",
                    "model": llm_layer.model_for("chat")})


# ═══════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        db.init_db()
        print("✅ 数据库已就绪")
    except Exception as e:
        print(f"⚠️ 数据库初始化警告: {e}")

    # 没有 API Key 也照常启动：站内检索之外的功能本来就能用，而且页面右上角
    # 有「填写 API Key」按钮，填完立刻生效。
    if not envconfig.api_key():
        print("\n" + "-" * 54)
        print("   ⚠️  还没有填写 DeepSeek API Key")
        print("   网页可以正常打开；点右上角「🔑 填写 API Key」填入即可，")
        print("   保存后立刻生效，不需要重启服务。")
        print("-" * 54 + "\n")

    host = (os.getenv("APP_HOST") or "127.0.0.1").strip()
    try:
        port = int(os.getenv("APP_PORT") or "8000")
    except ValueError:
        port = 8000

    if _port_in_use(host, port):
        print("=" * 62)
        print(f"❌ 端口 {port} 已被占用，服务可能已经在运行。")
        print("   先关掉旧实例（scripts/restart.ps1 或关闭旧窗口），")
        print("   或者换一个端口：set APP_PORT=8080")
        print("=" * 62)
        raise SystemExit(1)

    info = llm_layer.model_info()
    mcp_boot = mcp_client.warm_up()

    # 账号是复用的关键：数据库就在项目目录里，重启不会丢。
    try:
        account_count = db.count_users()
        account_names = [a["username"] for a in db.list_users(5)]
    except Exception:  # noqa: BLE001
        account_count, account_names = 0, []

    print("=" * 62)
    print("   🔬 DeepSeek AI4S 科学智能体（搜 · 读 · 想 · 做 · 算 · 验 · 写）")
    print(f"   🌐 本机 http://127.0.0.1:{port}")
    print(f"   🧠 模型后端：{info['backend_label']}")
    for role in info["roles"]:
        print(f"      - {role['label']:<6} {role['model']}"
              + ("（思考开）" if role["thinking"] else ""))
    if mcp_boot.get("ok"):
        print(f"   🔧 MCP：{mcp_boot['tools']} 个工具"
              f"（科学计算 + 文献检索，{mcp_boot['mode']} 传输）")
    else:
        print(f"   ⚠️ MCP 未就绪：{mcp_boot.get('error')}")
    if info["backend"] != "deepseek":
        print("   ⚠️ 当前未配置 DeepSeek API Key（DEEPSEEK_API_KEY），"
              "请在 .env 里填写后重新启动")
    print(f"   👤 账号文件：accounts.json（{account_count} 个账号"
          + (f"：{', '.join(account_names)}" if account_names else "，还没有账号，页面上注册即可")
          + "）")
    print(f"   🍪 保持登录：{LOGIN_DAYS} 天（勾选「记住我」后生效，可设 LOGIN_DAYS 调整）")
    if DEV_AUTO_LOGIN:
        print(f"   ⚠️ DEV_AUTO_LOGIN={DEV_AUTO_LOGIN}：本机未登录请求会自动登录该账号"
              "（仅回环地址生效，公网部署请去掉）")
    if PUBLIC_PASSWORD:
        print("   🔒 已启用公网访问口令（PUBLIC_PASSWORD）")
    print("=" * 62)

    # 端口可用 APP_PORT 覆盖：natapp 这类内网穿透转发的是**隧道里配置的那个
    # 本地端口**，如果隧道填的是 80，就用 APP_PORT=80 启动本服务去对齐。
    # 默认关 debug：Werkzeug 调试器只应在本地显式开启（APP_DEBUG=1）。
    # 隧道/公网部署时开着它，未捕获异常会暴露调试页面与交互式控制台。
    debug = (os.getenv("APP_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)
