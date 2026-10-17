"""验证 .env -> .env.local 的覆盖机制（团队共用配置 + 个人覆盖）。

测三件事：
1. 只读 .env 时，拿到 .env 里的值
2. 存在 .env.local 时，同名项被覆盖，未写的项仍沿用 .env
3. 子系统（不是 app.py）也能看到覆盖后的值（因为是 load_dotenv 写进 os.environ）
"""
import io
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
LOCAL = os.path.join(ROOT, ".env.local")

# 读 .env 里的真实值，作为基准
base = io.open(ENV, encoding="utf-8-sig").read()
import re
real_key = re.search(r"^DEEPSEEK_API_KEY=(.*)$", base, re.M).group(1).strip()
real_model = re.search(r"^DEEPSEEK_MODEL=(.*)$", base, re.M)
real_model = real_model.group(1).strip() if real_model else "<未设置>"

print(f"基准：DEEPSEEK_API_KEY 长度={len(real_key)}  DEEPSEEK_MODEL={real_model!r}")

# 探针：导入 app 之前先不设任何环境变量，只看它从文件读到什么
PROBE = r"""
import os, sys
sys.path.insert(0, r'%s')
# 清掉可能从父进程继承的同名变量
for k in ("DEEPSEEK_API_KEY", "API_TIMEOUT"):
    os.environ.pop(k, None)
import app
print("KEY_LEN", len(os.environ.get("DEEPSEEK_API_KEY") or ""))
print("KEY_TAIL", (os.environ.get("DEEPSEEK_API_KEY") or "")[-4:])
print("TIMEOUT", os.environ.get("API_TIMEOUT"))
"""


def run_probe(label):
    r = subprocess.run([sys.executable, "-c", PROBE % ROOT], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] in ("KEY_LEN", "KEY_TAIL", "TIMEOUT"):
            out[parts[0]] = parts[1].strip()
    print(f"\n[{label}]")
    print(f"  KEY_LEN={out.get('KEY_LEN')}  KEY_TAIL={out.get('KEY_TAIL')}  "
          f"TIMEOUT={out.get('TIMEOUT')}")
    if not out:
        print("  !! 探针无输出，stderr:", (r.stderr or "")[-400:])
    return out


R = []
def ok(name, cond, got=None):
    R.append((name, bool(cond), got))

# ── 1. 没有 .env.local ────────────────────────────────────
if os.path.exists(LOCAL):
    os.remove(LOCAL)
a = run_probe("无 .env.local：应完全来自 .env")
ok("读到 .env 里的 key（长度一致）", a.get("KEY_LEN") == str(len(real_key)),
   f"{a.get('KEY_LEN')} vs {len(real_key)}")
ok("读到 .env 里的 API_TIMEOUT=30", a.get("TIMEOUT") == "30", a.get("TIMEOUT"))

# ── 2. 有 .env.local：覆盖 key 与超时 ─────────────────────
try:
    io.open(LOCAL, "w", encoding="utf-8", newline="\n").write(
        "DEEPSEEK_API_KEY=sk-localoverride4444\n"
        "API_TIMEOUT=99\n"
    )
    b = run_probe("有 .env.local：应覆盖")
    ok(".env.local 覆盖了 key（尾部=4444）", b.get("KEY_TAIL") == "4444", b.get("KEY_TAIL"))
    ok(".env.local 覆盖了 API_TIMEOUT=99", b.get("TIMEOUT") == "99", b.get("TIMEOUT"))
    ok("覆盖后 key 长度来自 .env.local", b.get("KEY_LEN") == "20", b.get("KEY_LEN"))

    # ── 3. 只覆盖一项：其它项仍应来自 .env ────────────────
    io.open(LOCAL, "w", encoding="utf-8", newline="\n").write("API_TIMEOUT=7\n")
    c = run_probe("只写 API_TIMEOUT：key 应仍来自 .env")
    ok("未覆盖的项仍取自 .env", c.get("KEY_LEN") == str(len(real_key)),
       f"{c.get('KEY_LEN')} vs {len(real_key)}")
    ok("覆盖的项生效 =7", c.get("TIMEOUT") == "7", c.get("TIMEOUT"))
finally:
    if os.path.exists(LOCAL):
        os.remove(LOCAL)

# ── 4. 版本控制层面 ───────────────────────────────────────
r = subprocess.run(["git", "check-ignore", "-v", ".env"], cwd=ROOT,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
ok(".env 不再被 git 忽略", r.returncode != 0, r.stdout.strip())

r2 = subprocess.run(["git", "check-ignore", "-v", ".env.local"], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
ok(".env.local 仍被 git 忽略", r2.returncode == 0, r2.stdout.strip())

n = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n}/{len(R)} checks passed")
sys.exit(0 if n == len(R) else 1)
