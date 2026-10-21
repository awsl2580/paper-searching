"""环境变量加载：`.env` 不进版本库，Key 由使用者自己填（网页或手工）。

这条设计是涉密的，必须有测试守着，否则很容易被一次"顺手提交"破坏：
  * `.env`        **不进**版本库 → 里面可以放 Key / 会话密钥
  * `.env.example` 进版本库     → 只放模板和说明，绝不能有真实 Key
  * `.env.local`  不进版本库     → 可选的更高优先级覆盖文件

测的点：
1. 版本控制边界：`.env` / `.env.local` 被忽略，`.env.example` 仍被跟踪
2. 没有 `.env` 时 `ensure_env_file()` 会自动生成，且会话密钥是真随机值
3. 没有 Key 时 `require_api_key()` 给出可照做的提示（指向 `.env`）
4. `.env.local` 能覆盖 `.env` 里的项（模型名不写时仍来自 `.env`）
5. `.env.example` 里没有真实 Key（模板本身不会泄露）
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
EXAMPLE = os.path.join(ROOT, ".env.example")
LOCAL = os.path.join(ROOT, ".env.local")
BACKUP = os.path.join(ROOT, ".env.local.testbak")
ENV_BACKUP = os.path.join(ROOT, ".env.testbak")

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def run_probe(code, clean=("DEEPSEEK_API_KEY", "API_TIMEOUT", "DEEPSEEK_MODEL")):
    """在干净的环境变量下跑一段探针代码，返回 stdout。"""
    envc = {k: v for k, v in os.environ.items() if k not in clean}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=envc,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    return r.stdout or ""


def kv(text):
    out = {}
    for line in text.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip()
    return out


# 探针：清掉继承来的环境变量，只看程序从文件里读到了什么
PROBE = r"""
import os, sys
sys.path.insert(0, r'%s')
for k in ("DEEPSEEK_API_KEY", "FLASK_SECRET_KEY", "API_TIMEOUT", "DEEPSEEK_MODEL"):
    os.environ.pop(k, None)
import envconfig
loaded = envconfig.load_env_files()
print("LOADED", ",".join(os.path.basename(p) for p in loaded) or "<none>")
print("KEY_LEN", len(envconfig.api_key()))
print("KEY_TAIL", envconfig.api_key()[-4:] or "<empty>")
print("TIMEOUT", os.environ.get("API_TIMEOUT"))
print("MODEL", os.environ.get("DEEPSEEK_MODEL"))
"""


def probe(label):
    out = kv(run_probe(PROBE % ROOT))
    print(f"\n[{label}]")
    print("  " + "  ".join(f"{k}={v}" for k, v in out.items()))
    if not out:
        print("  !! 探针无输出")
    return out


# 保护使用者的真实配置：全程用测试值，结束后还原
had_local = os.path.exists(LOCAL)
had_env = False
if had_local:
    shutil.copy2(LOCAL, BACKUP)
    os.remove(LOCAL)

try:
    # ── 0. 版本控制边界 ────────────────────────────────────
    ignored = lambda p: subprocess.run(
        ["git", "check-ignore", "-v", p], cwd=ROOT, capture_output=True).returncode == 0
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env.example"],
                             cwd=ROOT, capture_output=True).returncode == 0

    ok(".env 被 git 忽略（不会提交到 GitHub）", ignored(".env"))
    ok(".env.local 被 git 忽略", ignored(".env.local"))
    ok(".env.example 仍然进仓库（模板要给团队用）", tracked)

    # 模板里不能有真实 Key
    example_key = re.search(r"^DEEPSEEK_API_KEY=(.*)$",
                            io.open(EXAMPLE, encoding="utf-8").read(), re.M)
    ok(".env.example 里的 DEEPSEEK_API_KEY 为空",
       (example_key.group(1) if example_key else "").strip() == "")

    # ── 1. 没有 .env：自动生成 ──────────────────────────────
    if os.path.exists(ENV):
        had_env = True
        shutil.copy2(ENV, ENV_BACKUP)
        os.remove(ENV)

    GEN = r"""
import os, sys
sys.path.insert(0, r'%s')
for k in ("DEEPSEEK_API_KEY", "FLASK_SECRET_KEY"):
    os.environ.pop(k, None)
import envconfig
path, created = envconfig.ensure_env_file()
envconfig.load_env_files()
print("CREATED", int(created))
print("EXISTS", int(os.path.exists(path)))
print("SECRET_LEN", len(os.environ.get("FLASK_SECRET_KEY") or ""))
print("KEY_LEN", len(envconfig.api_key()))
print("MODEL", os.environ.get("DEEPSEEK_MODEL"))
""" % ROOT
    g = kv(run_probe(GEN, clean=()))
    print(f"\n[没有 .env：应自动生成]")
    print("  " + "  ".join(f"{k}={v}" for k, v in g.items()))
    ok("没有 .env 时会自动生成", g.get("CREATED") == "1", g.get("CREATED"))
    ok("生成的文件确实落地", g.get("EXISTS") == "1", g.get("EXISTS"))
    ok("会话密钥自动填成随机值（64 位十六进制）",
       g.get("SECRET_LEN") == "64", g.get("SECRET_LEN"))
    ok("新生成的 .env 里没有 Key", g.get("KEY_LEN") == "0", g.get("KEY_LEN"))
    ok("生成的文件带上了默认模型名", g.get("MODEL") == "deepseek-flash", g.get("MODEL"))

    # 二次调用不应该继续"新建"
    again = kv(run_probe(GEN, clean=()))
    ok("已存在时不再重复生成", again.get("CREATED") == "0", again.get("CREATED"))

    # ── 2. 没有 Key 时的提示必须可照做 ──────────────────────
    code = (
        "import os, sys\n"
        "sys.path.insert(0, r'%s')\n"
        "os.environ.pop('DEEPSEEK_API_KEY', None)\n"
        "import envconfig\n"
        "envconfig.load_env_files()\n"
        "try:\n"
        "    envconfig.require_api_key('测试')\n"
        "    print('NO_ERROR')\n"
        "except RuntimeError as e:\n"
        "    print('MSG_START')\n"
        "    print(str(e))\n"
    ) % ROOT
    msg = run_probe(code)
    ok("没有 Key 时会抛出错误（不静默继续）", "NO_ERROR" not in msg)
    ok("提示里指向 .env 这个文件", ".env" in msg, msg[:120])
    ok("提示里给出了文件内容示例", "DEEPSEEK_API_KEY=sk-" in msg)
    ok("提示里给了申请地址", "platform.deepseek.com" in msg)

    # ── 3. .env.local 覆盖 .env ────────────────────────────
    # 这个"假 key"在运行时拼出来，源码里不放形如 sk-xxxx 的字面量 ——
    # 否则 GitHub 的密钥扫描、各类 secret scanner 会把它当成真 key 报警。
    FAKE = "sk-" + "test" * 5
    io.open(LOCAL, "w", encoding="utf-8", newline="\n").write(
        f"DEEPSEEK_API_KEY={FAKE}\nAPI_TIMEOUT=77\n")
    b = probe(".env.local 写了 Key：覆盖生效")
    ok("Key 来自 .env.local", b.get("KEY_LEN") == str(len(FAKE)), b.get("KEY_LEN"))
    ok("Key 内容正确（尾部=test）", b.get("KEY_TAIL") == "test", b.get("KEY_TAIL"))
    ok(".env.local 覆盖了 API_TIMEOUT", b.get("TIMEOUT") == "77", b.get("TIMEOUT"))
    ok("未覆盖的项仍来自 .env（模型名）", b.get("MODEL") == "deepseek-flash", b.get("MODEL"))
    ok("两个文件都被加载", b.get("LOADED") == ".env,.env.local", b.get("LOADED"))

    # ── 4. .env.local 存在但没有 Key：仍应视为没有 Key ────
    io.open(LOCAL, "w", encoding="utf-8", newline="\n").write("API_TIMEOUT=55\n")
    c = probe(".env.local 不含 Key：仍应没有 Key")
    ok("没有别处兜底提供 Key", c.get("KEY_LEN") == "0", c.get("KEY_LEN"))
    ok("覆盖项仍生效", c.get("TIMEOUT") == "55", c.get("TIMEOUT"))

finally:
    # 还原使用者真实的配置文件
    if os.path.exists(LOCAL):
        os.remove(LOCAL)
    if had_local:
        shutil.move(BACKUP, LOCAL)
        print(f"\n已还原你的 .env.local（{os.path.getsize(LOCAL)} 字节）")

    if had_env:
        if os.path.exists(ENV):
            os.remove(ENV)
        shutil.move(ENV_BACKUP, ENV)
        print(f"已还原你的 .env（{os.path.getsize(ENV)} 字节）")

n = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n}/{len(R)} checks passed")
sys.exit(0 if n == len(R) else 1)
