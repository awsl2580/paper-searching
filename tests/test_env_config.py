"""环境变量加载：`.env` 不含 API Key，Key 由使用者在 `.env.local` 自己写入。

这条设计是涉密的，必须有测试守着，否则很容易被一次"顺手提交"破坏：
  * `.env`        进版本库 → 里面**绝不能**出现真实 API Key
  * `.env.local`  不进版本库 → 每个人自己的 Key 写这里

测的点：
1. 只有 `.env` 时，读不到 Key，且 `require_api_key()` 给出可照做的提示
2. 写入 `.env.local` 后，Key 生效
3. `.env.local` 只覆盖它写了的项，其余（如模型名）仍来自 `.env`
4. `.env.local` 存在但不含 Key 时，仍然是"没有 Key"（证明 `.env` 没在兜底）
5. 版本控制边界：`.env` 不被忽略、`.env.local` 被忽略
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
LOCAL = os.path.join(ROOT, ".env.local")
BACKUP = os.path.join(ROOT, ".env.local.testbak")

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# 探针：清掉继承来的环境变量，只看程序从文件里读到了什么
PROBE = r"""
import os, sys
sys.path.insert(0, r'%s')
for k in ("DEEPSEEK_API_KEY", "API_TIMEOUT", "DEEPSEEK_MODEL"):
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
    r = subprocess.run([sys.executable, "-c", PROBE % ROOT], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] in (
                "LOADED", "KEY_LEN", "KEY_TAIL", "TIMEOUT", "MODEL"):
            out[parts[0]] = parts[1].strip()
    print(f"\n[{label}]")
    print("  " + "  ".join(f"{k}={v}" for k, v in out.items()))
    if not out:
        print("  !! 探针无输出:", (r.stderr or "")[-400:])
    return out


# 保护使用者的真实 .env.local：全程用测试值，结束后还原
had_local = os.path.exists(LOCAL)
if had_local:
    shutil.copy2(LOCAL, BACKUP)
    os.remove(LOCAL)

try:
    # ── 0. 仓库里的 .env 本身不能含真实 Key ────────────────
    env_text = io.open(ENV, encoding="utf-8-sig").read()
    import re
    m = re.search(r"^DEEPSEEK_API_KEY=(.*)$", env_text, re.M)
    in_repo_key = (m.group(1) if m else "").strip()
    ok(".env 里的 DEEPSEEK_API_KEY 为空（不随仓库分发）", in_repo_key == "", in_repo_key[:6])
    ok(".env 仍是提交进仓库的（不被忽略）",
       subprocess.run(["git", "check-ignore", "-v", ".env"], cwd=ROOT,
                      capture_output=True).returncode != 0)
    ok(".env.local 被 git 忽略",
       subprocess.run(["git", "check-ignore", "-v", ".env.local"], cwd=ROOT,
                      capture_output=True).returncode == 0)

    # ── 1. 没有 .env.local：读不到 Key ──────────────────────
    a = probe("没有 .env.local：应没有 Key")
    ok("没有 .env.local 时 Key 为空", a.get("KEY_LEN") == "0", a.get("KEY_LEN"))
    ok("其余配置仍来自 .env（模型名）", a.get("MODEL") == "deepseek-flash", a.get("MODEL"))
    ok("其余配置仍来自 .env（超时）", a.get("TIMEOUT") == "30", a.get("TIMEOUT"))

    # require_api_key 的提示必须可照做
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
    envc = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=envc,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    msg = (r.stdout or "")
    ok("没有 Key 时会抛出错误（不静默继续）", "NO_ERROR" not in msg)
    ok("提示里指出了 .env.local 的确切路径", ".env.local" in msg, msg[:120])
    ok("提示里给出了文件内容示例", "DEEPSEEK_API_KEY=sk-" in msg)
    ok("提示里给了申请地址", "platform.deepseek.com" in msg)

    # ── 2. 写入 .env.local：Key 生效 ────────────────────────
    FAKE = "sk-testonly1234567890"
    io.open(LOCAL, "w", encoding="utf-8", newline="\n").write(
        f"DEEPSEEK_API_KEY={FAKE}\nAPI_TIMEOUT=77\n")
    b = probe("有 .env.local：Key 生效并覆盖超时")
    ok("Key 来自 .env.local", b.get("KEY_LEN") == str(len(FAKE)), b.get("KEY_LEN"))
    ok("Key 内容正确（尾部=7890）", b.get("KEY_TAIL") == "7890", b.get("KEY_TAIL"))
    ok(".env.local 覆盖了 API_TIMEOUT", b.get("TIMEOUT") == "77", b.get("TIMEOUT"))
    ok("未覆盖的项仍来自 .env（模型名）", b.get("MODEL") == "deepseek-flash", b.get("MODEL"))
    ok("两个文件都被加载", b.get("LOADED") == ".env,.env.local", b.get("LOADED"))

    # ── 3. .env.local 存在但没有 Key：仍应视为没有 Key ────
    io.open(LOCAL, "w", encoding="utf-8", newline="\n").write("API_TIMEOUT=55\n")
    c = probe(".env.local 不含 Key：仍应没有 Key")
    ok(".env 没有在兜底提供 Key", c.get("KEY_LEN") == "0", c.get("KEY_LEN"))
    ok("覆盖项仍生效", c.get("TIMEOUT") == "55", c.get("TIMEOUT"))

finally:
    # 还原使用者真实的 .env.local
    if os.path.exists(LOCAL):
        os.remove(LOCAL)
    if had_local:
        shutil.move(BACKUP, LOCAL)
        print(f"\n已还原你的 .env.local（{os.path.getsize(LOCAL)} 字节）")
    else:
        print("\n原本没有 .env.local，已清理测试文件")

n = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n}/{len(R)} checks passed")
sys.exit(0 if n == len(R) else 1)
