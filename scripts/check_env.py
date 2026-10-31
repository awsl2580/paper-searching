"""环境自检（对方电脑跑不起来时，先跑这个）。

用法（在项目目录里）：
    python scripts/check_env.py   # 或双击 一键启动.bat，它会自动跑一遍

检查项：Python 版本 / 依赖是否装齐 / .env 是否配好 / 数据库与账号 /
MCP 工具能否加载 / 端口是否被占。全部只读，不改任何文件（除了写一份
诊断结果.txt 方便你复制粘贴）。
"""

from __future__ import annotations

import importlib
import os
import socket
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import paths  # noqa: E402,F401  （把 core/ science/ 等分类目录加入 sys.path）
LINES = []
PROBLEMS = []


def say(text=""):
    print(text)
    LINES.append(text)


def ok(label, detail=""):
    say("  [OK]   " + label + (("  ->  " + str(detail)) if detail else ""))


def warn(label, detail="", fix=""):
    say("  [警告] " + label + (("  ->  " + str(detail)) if detail else ""))
    if fix:
        say("        修复：" + fix)


def bad(label, detail="", fix=""):
    say("  [错误] " + label + (("  ->  " + str(detail)) if detail else ""))
    if fix:
        say("        修复：" + fix)
    PROBLEMS.append(label)


def main():
    say("=" * 66)
    say("  DeepSeek AI4S 科学智能体 · 环境自检")
    say("=" * 66)

    # ── 1. Python 版本 ──────────────────────────────────────
    say("\n[1] Python")
    ver = sys.version_info
    say("  版本：%d.%d.%d（%s）" % (ver.major, ver.minor, ver.micro, sys.executable))
    if ver < (3, 10):
        bad("Python 版本过低", "需要 3.10 及以上",
            "去 python.org 装 3.12 / 3.13，安装时勾选 Add to PATH")
    elif ver >= (3, 14):
        ok("版本可用（3.14 上各依赖都有官方 wheel）",
           "若某个包装不上，改用 3.12/3.13 最稳")
    else:
        ok("版本满足要求（>=3.10）")

    # ── 2. 依赖 ────────────────────────────────────────────
    say("\n[2] 依赖")
    must = [
        ("flask", "flask"),
        ("langchain", "langchain"),
        ("langchain_openai", "langchain-openai"),
        ("langchain_core", "langchain-core"),
        ("requests", "requests"),
        ("dotenv", "python-dotenv"),
        ("bcrypt", "bcrypt"),
    ]
    missing = []
    for mod, pkg in must:
        try:
            m = importlib.import_module(mod)
            ok(mod, getattr(m, "__version__", ""))
        except Exception as e:
            missing.append(pkg)
            bad("缺少 " + mod, "%s: %s" % (type(e).__name__, e),
                "pip install -r requirements.txt")
    # 可选依赖
    try:
        import pymupdf  # noqa: F401
        ok("pymupdf（PDF 转图片，可选）")
    except Exception:
        warn("没有 pymupdf", "PDF 附件功能不可用，其它功能正常",
             "pip install pymupdf")

    # ── 3. .env 配置 ───────────────────────────────────────
    say("\n[3] 配置（.env）")
    env_path = os.path.join(HERE, ".env")
    if not os.path.isfile(env_path):
        bad("没有 .env", "项目根目录应有 .env",
            "把 .env.example 复制成 .env（或双击 一键启动.bat 自动生成）")
        token = ""
        secret = ""
    else:
        ok(".env 存在", env_path)
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        token = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        secret = (os.getenv("FLASK_SECRET_KEY") or "").strip()
        if token:
            ok("DEEPSEEK_API_KEY 已填", "%d 个字符（%s…）" % (len(token), token[:6]))
        else:
            bad("没有 DeepSeek API Key", "DEEPSEEK_API_KEY 为空",
                "打开 .env 填 DEEPSEEK_API_KEY（申请：https://platform.deepseek.com/api_keys）")
        if len(secret) >= 32:
            ok("FLASK_SECRET_KEY 已设置", "%d 个字符" % len(secret))
        else:
            bad("FLASK_SECRET_KEY 缺失或太短", "现在 %d 个字符" % len(secret),
                "python -c \"import secrets;print('FLASK_SECRET_KEY='+secrets.token_hex(32))\" 写进 .env")

    # 常见误填：带了引号或 Bearer
    if token.startswith('"') or token.startswith("'"):
        warn("API Key 首字符是引号", "引号会被当成 key 的一部分", ".env 里去掉引号")
    if token.lower().startswith("bearer"):
        warn("API Key 里带了 Bearer", "程序会自己加 Authorization: Bearer", "去掉 Bearer 前缀")

    # ── 4. 数据与账号 ──────────────────────────────────────
    say("\n[4] 数据与账号")
    acc_path = os.path.join(HERE, "accounts.json")
    if os.path.isfile(acc_path):
        try:
            import json
            with open(acc_path, encoding="utf-8") as f:
                users = (json.load(f) or {}).get("users") or []
            ok("accounts.json（账号文件）", "%d 个账号：%s" % (
                len(users), "、".join(u.get("username", "?") for u in users[:5]) or "无"))
            if any("@@" in (u.get("password") or "") for u in users):
                warn("密码字段看起来不像哈希", "请勿手工改成明文密码")
        except Exception as e:
            warn("读 accounts.json 失败", "%s: %s" % (type(e).__name__, e))
    else:
        warn("还没有 accounts.json",
             "第一次注册账号时会自动创建（密码只存 bcrypt 哈希）；备份时拷它就行")

    db_path = os.path.join(HERE, "literature.db")
    if os.path.isfile(db_path):
        size = os.path.getsize(db_path)
        ok("literature.db 存在", "%.1f KB（收藏 / 对话 / 研究记忆）" % (size / 1024))
    else:
        warn("还没有 literature.db", "第一次启动会自动创建（收藏、对话记录、研究记忆）",
             "启动一次后就会生成，注意别删它")

    # ── 5. 科学计算工具（MCP）──────────────────────────────
    say("\n[5] 科学计算工具")
    try:
        import science
        names = science.names()
        ok("science 包可加载", "%d 个工具" % len(names))
        r = science.call_tool("stats_ttest", {"x": [1, 2, 3, 4, 5], "y": [2, 3, 4, 5, 7]})
        if r.get("ok"):
            ok("试算一个 t 检验", "p = %s" % r["result"].get("p"))
        else:
            warn("试算失败", r.get("error"))
    except Exception as e:
        bad("science 包加载失败", "%s: %s" % (type(e).__name__, e),
            "确认 science/ 目录完整（打包时别漏掉）")

    # ── 6. 端口 ────────────────────────────────────────────
    say("\n[6] 端口")
    port = 8000
    try:
        port = int(os.getenv("APP_PORT") or "8000")
    except ValueError:
        pass
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        ok("端口 %d 可用" % port)
    except OSError:
        warn("端口 %d 已被占用" % port, "可能已经开着一个服务",
             "直接打开 http://127.0.0.1:%d ；或换端口 APP_PORT=8080" % port)
    finally:
        s.close()

    # ── 7. 目录完整性 ──────────────────────────────────────
    say("\n[7] 目录完整性")
    need_dirs = ["core", "web", "cli", "mcp", "providers", "science",
                 "skills", "static", "tests", "scripts", "docs"]
    need_files = ["paths.py", "web/app.py", "core/llm.py", "core/workflow.py",
                  "core/agents.py", "core/agent_tools.py", "core/skills.py",
                  "core/memory.py", "core/accounts.py", "core/database.py",
                  "core/attachments.py", "core/env_boot.py",
                  "mcp/mcp_server.py", "mcp/mcp_client.py",
                  "requirements.txt", "static/index.html", "一键启动.bat"]
    lack = [d for d in need_dirs if not os.path.isdir(os.path.join(HERE, d))]
    lack += [f for f in need_files if not os.path.isfile(os.path.join(HERE, f))]
    if lack:
        bad("缺文件/目录", "、".join(lack),
            "重新解压一份完整的压缩包（别只拷一部分）")
    else:
        ok("项目文件齐全")

    # ── 汇总 ───────────────────────────────────────────────
    say("\n" + "=" * 66)
    if PROBLEMS:
        say("发现 %d 个问题：%s" % (len(PROBLEMS), "、".join(PROBLEMS)))
        say("按上面每条后面的「修复」做一遍，再重新跑本脚本。")
    else:
        say("全部通过，可以直接启动：")
        say("    .venv\\Scripts\\python.exe web\\app.py     （或双击 一键启动.bat）")
        say("然后浏览器打开 http://127.0.0.1:%d" % port)
    say("=" * 66)

    out = os.path.join(HERE, "诊断结果.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(LINES) + "\n")
        print("\n（以上内容已保存到 %s，遇到问题可以把这个文件发给作者）" % out)
    except Exception:
        pass
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
