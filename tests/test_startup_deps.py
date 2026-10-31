"""启动体验：依赖缺失时别"黑屏"等，输出编码别把启动搞崩。

这两个坑都是真机踩出来的（2026-10-28）：

1. `web/app.py` 里装依赖那条命令原本是
       pip install -q ... stdout=DEVNULL
   出问题时屏幕上只剩一行 "⏳ 正在自动安装..."，用户分不清是慢还是死了；
   pip 的真实报错还被 DEVNULL 吞掉，最后只得到一句"安装失败"。
   现在的约定：把缺了什么、大概多久、pip 的进度都打出来，
   默认源失败后自动换清华镜像重试。

2. Windows 中文控制台是 GBK，把 stdout 重定向到文件时 Python 用 gbk 编码，
   `print("✅ 数据库初始化完成")` 会抛 UnicodeEncodeError，启动死在第一屏。
   现在在被重定向时把输出切成 UTF-8。

不依赖 pytest，跑完打印 `N/M checks passed`。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_dir  # noqa: E402

APP = os.path.join(ROOT, "web", "app.py")
BOOT = os.path.join(ROOT, "scripts", "bootstrap_env.py")

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


src = read(APP)

# ── 1. 依赖安装：可见、可重试、不吞输出 ───────────────────────
ok("安装了 pip 的输出没有被丢进 DEVNULL",
   "stdout=subprocess.DEVNULL" not in src.split("REQUIRED_PACKAGES")[1][:4000])
ok("声明了超时/重试，默认源不通时能快点换镜像",
   '"--timeout"' in src and '"--retries"' in src)
ok("优先用 wheel（3.14 上不现场编译）", '"--prefer-binary"' in src)
ok("有清华镜像兜底", "pypi.tuna.tsinghua.edu.cn" in src)
ok("说明了首次要多久 / 之后很快",
   "首次通常" in src and "几秒" in src)
ok("失败时给出可复制的手动命令",
   "pip install -r requirements.txt -i" in src)
ok("装完会再验证一遍（防止装到别的解释器上）",
   "pip 说装完了" in src)
ok("缺依赖时会列出到底缺什么", "缺少 %d 个依赖" in src)


def _missing_from(list_src):
    """从源码里把 REQUIRED_PACKAGES 的模块名扣出来（不执行 app.py）。"""
    block = list_src.split("REQUIRED_PACKAGES = {", 1)[1].split("}", 1)[0]
    names = []
    for line in block.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith('"') and ":" in line:
            names.append(line.split('"')[1])
    return names


mods = _missing_from(src)
ok("依赖清单非空且都是模块名",
   len(mods) >= 6 and all(m.replace("_", "").isalnum() for m in mods), mods)
ok("依赖清单里包含 flask / langchain / bcrypt",
   {"flask", "langchain", "bcrypt"} <= set(mods), mods)

# ── 2. 输出编码：重定向到文件时不要因为 emoji 崩 ──────────────
ok("有 stdout 编码保护（只在非终端时生效）",
   "isatty()" in src and 'reconfigure(encoding="utf-8"' in src)
ok("编码保护在数据库初始化之前",
   src.find("isatty()") < src.find("db.init_db()"))

# 真跑一遍：模拟 GBK 控制台 + 重定向，验证这套写法在这台机器上确实不崩
probe = os.path.join(temp_dir(), "_enc_probe.py")
out_path = os.path.join(temp_dir(), "_enc_probe.out")
with open(probe, "w", encoding="utf-8") as f:
    f.write(
        "import sys\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    try:\n"
        "        if not s.isatty():\n"
        "            s.reconfigure(encoding='utf-8', errors='replace')\n"
        "    except Exception:\n"
        "        pass\n"
        "print('✅ 数据库初始化完成！')\n"
        "print('⚠️ 带 emoji 的告警')\n"
    )
env = dict(os.environ)
env.pop("PYTHONIOENCODING", None)          # 故意不给 UTF-8，模拟真实翻车场景
env["PYTHONUTF8"] = "0"                    # 关掉全局 UTF-8 模式，回到 GBK 默认
with open(out_path, "wb") as out:
    rc = subprocess.call([sys.executable, probe], stdout=out,
                         stderr=subprocess.STDOUT, env=env)
body = b""
if os.path.isfile(out_path):
    with open(out_path, "rb") as f:
        body = f.read()
ok("重定向 + emoji 不再抛 UnicodeEncodeError", rc == 0, "exit=%s" % rc)
ok("输出按 UTF-8 落地，可原样读回",
   "✅ 数据库初始化完成".encode("utf-8") in body, body[-60:])

# ── 3. 重建 venv 时要讲清楚"包要重装" ─────────────────────────
boot = read(BOOT)
ok("重建前会提示依赖需要重新安装",
   "需要重建 .venv" in boot and "重新安装依赖" in boot)

# 自己收拾干净：探测脚本不能留在仓库里（临时目录可能是项目根本身）
for _p in (probe, out_path):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except OSError:
        pass
ok("探测用的临时文件已清掉",
   not os.path.exists(probe) and not os.path.exists(out_path))

# ── 汇总 ──────────────────────────────────────────────────────
failed = 0
for name, good, got in R:
    print("%s %s%s" % ("[OK]  " if good else "[FAIL]", name,
                       "" if good else "  -> %r" % (got,)))
    failed += 0 if good else 1
print("%d/%d checks passed" % (len(R) - failed, len(R)))
sys.exit(1 if failed else 0)
