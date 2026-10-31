"""启动引导（bootstrap_env）：.venv 必须跟随当前主机的 Python。

覆盖三件事：
1. `pyvenv.cfg` 的读写 / 重定向；
2. 版本判断（同 major.minor 只改路径，不同则重建）；
3. 重建是"先建 .venv.new 再替换"——失败不能弄丢原来的 .venv。

不依赖 pytest，跑完打印 `N/M checks passed`。
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_dir  # noqa: E402

sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
import bootstrap_env as b  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ── 1. 版本工具 ───────────────────────────────────────────────
ok("解析 3.14.2", b.version_tuple("3.14.2") == (3, 14, 2), b.version_tuple("3.14.2"))
ok("解析带前缀的版本串", b.version_tuple("Python 3.10.0") == (3, 10, 0),
   b.version_tuple("Python 3.10.0"))
ok("空串返回空元组", b.version_tuple("") == ())
ok("minor_of 取前两位", b.minor_of((3, 14, 9)) == (3, 14))
ok("3.14.2 与 3.14.9 同 minor", b.same_minor((3, 14, 2), (3, 14, 9)))
ok("3.13 与 3.14 不同 minor",
   not b.same_minor((3, 13, 9), (3, 14, 0)))
ok("空版本不算同 minor", not b.same_minor((), (3, 14, 0)))

# ── 2. pyvenv.cfg 读写 ────────────────────────────────────────
work = os.path.join(temp_dir(), "_tmp_boot_cfg")
shutil.rmtree(work, ignore_errors=True)
os.makedirs(work)
try:
    cfg = os.path.join(work, "pyvenv.cfg")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("home = C:\\old-python\n"
                "include-system-site-packages = true\n"
                "version = 3.14.2\n"
                "unknown_key = keep-me\n"
                "this line has no equals sign\n")
    data = b.read_cfg(cfg)
    ok("读到 home", data.get("home") == "C:\\old-python", data.get("home"))
    ok("读到未知键", data.get("unknown_key") == "keep-me")
    ok("忽略无等号的行", "this line has no equals sign" not in data)

    b.write_cfg(cfg, data)
    again = b.read_cfg(cfg)
    ok("写回后内容一致", again == data, again)
    first = open(cfg, encoding="utf-8").read().splitlines()[0]
    ok("home 写在第一行", first.startswith("home = "), first)
    ok("未知键被保留", again.get("unknown_key") == "keep-me")

    b.rewrite_cfg(work, "D:\\HostPython\\python.exe", (3, 12, 4))
    re = b.read_cfg(cfg)
    ok("重定向 home", re.get("home") == "D:\\HostPython", re.get("home"))
    ok("重定向 executable", re.get("executable") == "D:\\HostPython\\python.exe",
       re.get("executable"))
    ok("重定向 version", re.get("version") == "3.12.4", re.get("version"))
    ok("重定向保留 include 设置",
       re.get("include-system-site-packages") == "true",
       re.get("include-system-site-packages"))
finally:
    shutil.rmtree(work, ignore_errors=True)

# ── 3. 探测与重建 ─────────────────────────────────────────────
ok("venv_python 路径正确",
   b.venv_python("X").replace("\\", "/").endswith("Scripts/python.exe"))
ok("不存在的 venv 探测为 None",
   b.venv_works(os.path.join(temp_dir(), "_tmp_no_such_venv")) is None)

root = os.path.join(temp_dir(), "_tmp_boot_venv")
shutil.rmtree(root, ignore_errors=True)
os.makedirs(root)
venv = os.path.join(root, "venv")
os.makedirs(venv)
try:
    with open(os.path.join(venv, "pyvenv.cfg"), "w", encoding="utf-8") as f:
        f.write("home = Z:\\no-such-python\nversion = 3.99.0\n")

    host = b.host_executable()
    host_ver = b.version_tuple(sys.version)
    ok("宿主 Python 存在", os.path.isfile(host), host)
    ok("拒绝 Python 3.9",
       b.ensure_venv(venv, host, (3, 9, 0), quiet=True) is False)

    ok("版本不同会重建",
       b.ensure_venv(venv, host, host_ver, quiet=True) is True)
    ok("重建后解释器存在", os.path.isfile(b.venv_python(venv)))
    ok("重建后解释器可启动", b.venv_works(venv) is not None)
    ok("没有 .venv.new 残留", not os.path.isdir(venv + ".new"))
    ok("没有 .venv.old 残留", not os.path.isdir(venv + ".old"))
    ok("再次对齐（幂等）",
       b.ensure_venv(venv, host, host_ver, quiet=True) is True)
finally:
    shutil.rmtree(root, ignore_errors=True)

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
