"""启动前把 .venv 对齐到"当前主机上真实存在的 Python"。

## 为什么需要它

标准 venv 的 `pyvenv.cfg` 里 `home` / `executable` 是**创建时那台机器的绝对路径**。
项目整体拷到别的机器或别的目录后，`.venv\\Scripts\\python.exe` 会直接报
`Failed to import encodings` —— 因为 `home` 指向的 Python 已经不在了。
`pyvenv.cfg` 里的相对路径也不行（实测会报 `did not find executable`）。

所以每次启动先跑这个脚本（由 `一键启动.bat` 用宿主 Python 调用）：

* **同 major.minor**：只把 `home` / `executable` / `version` / `command` 重写成
  当前宿主 Python 的绝对路径，依赖不重装；
* **版本不同 / venv 真坏了**：删掉 `.venv` 重建（依赖由 `launch.py` 重新安装）；
* **没有 .venv**：用当前宿主 Python 创建。

只依赖标准库；不导入项目里的任何模块，保证在坏掉的 venv 之外也能跑。

用法：
    python scripts/bootstrap_env.py [venv_dir]

不传 `venv_dir` 时用项目下的 `.venv`。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
DEFAULT_VENV = os.path.join(HERE, ".venv")
MIN_PYTHON = (3, 10)

CFG_ORDER = ("home", "include-system-site-packages", "version", "executable", "command")


def _say(text=""):
    """打印一行；控制台编码不支持某些符号时降级，不让脚本自己崩掉。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = str(text).encode(enc, "replace").decode(enc, "replace")
        print(safe, flush=True)


def _capture(cmd, timeout, cwd=HERE):
    """跑子进程并把输出收进临时文件（返回码, 文本）。

    不用 `capture_output=True`：某些受限环境会禁止创建匿名管道
    （Windows 上报 `PermissionError: [WinError 5]`），那样会把
    "探测失败" 误判成 "venv 坏了"，进而误删一个本来能用的 venv。
    写临时文件没有这个限制。
    """
    fd, path = tempfile.mkstemp(prefix="ai4s_boot_", suffix=".log")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as out:
            r = subprocess.run(cmd, cwd=cwd, stdout=out,
                               stderr=subprocess.STDOUT, timeout=timeout)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        return r.returncode, text
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════
#  版本与 pyvenv.cfg
# ═══════════════════════════════════════════════════════════════

def version_tuple(text):
    """从 '3.14.2 (tags/...)' 里取出 (3, 14, 2)；取不到返回 ()。"""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(x) for x in nums[:3])


def minor_of(ver):
    """(3, 14, 2) -> (3, 14)；不足两位返回原样。"""
    return tuple(ver[:2])


def same_minor(a, b):
    """两个版本是否同一 major.minor（venv 只跨小版本兼容，不跨大版本）。"""
    return bool(a) and bool(b) and minor_of(a) == minor_of(b)


def read_cfg(path):
    """读 pyvenv.cfg；兼容偶发的非 UTF-8 旧文件。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except UnicodeDecodeError:
        enc = "mbcs" if os.name == "nt" else "latin-1"
        with open(path, "r", encoding=enc, errors="replace") as f:
            raw = f.read()
    out = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def write_cfg(path, data):
    """按固定顺序写回 pyvenv.cfg（先写临时文件再替换，避免写一半）。"""
    keys = [k for k in CFG_ORDER if k in data]
    keys += sorted(k for k in data if k not in CFG_ORDER)
    text = "\n".join("%s = %s" % (k, data[k]) for k in keys) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def venv_python(venv_dir):
    return os.path.join(venv_dir, "Scripts", "python.exe")


def venv_works(venv_dir):
    """venv 里的 python 能否真正启动；能则返回它的版本，否则 None。"""
    py = venv_python(venv_dir)
    if not os.path.isfile(py):
        return None
    try:
        rc, out = _capture(
            [py, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            timeout=20,
        )
    except Exception:  # noqa: BLE001 - 坏 venv / 超时 / 权限问题都算探测失败
        return None
    if rc != 0:
        return None
    ver = version_tuple(out)
    return ver if len(ver) >= 2 else None


def rewrite_cfg(venv_dir, host_exe, host_ver):
    """把 venv 的基座指向 host_exe（不改已装的依赖）。"""
    cfg_path = os.path.join(venv_dir, "pyvenv.cfg")
    cfg = read_cfg(cfg_path) if os.path.isfile(cfg_path) else {}
    cfg["home"] = os.path.dirname(host_exe)
    cfg["executable"] = host_exe
    cfg["version"] = ".".join(str(x) for x in host_ver[:3])
    cfg["command"] = "%s -m venv %s" % (host_exe, venv_dir)
    cfg.setdefault("include-system-site-packages", "false")
    write_cfg(cfg_path, cfg)


# ═══════════════════════════════════════════════════════════════
#  创建 / 重建
# ═══════════════════════════════════════════════════════════════

def _force_remove(func, path, _exc_info):
    """rmtree 的回调：清只读位再删，兼容 Windows 上被标记只读的文件。"""
    try:
        os.chmod(path, 0o700)
        func(path)
    except Exception:  # noqa: BLE001
        pass


def remove_venv(venv_dir):
    """删掉 venv；被占用时重试几次。返回 True/False。"""
    if not os.path.isdir(venv_dir):
        return True
    last = None
    for _ in range(3):
        try:
            shutil.rmtree(venv_dir, onerror=_force_remove)
            if not os.path.isdir(venv_dir):
                return True
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.4)
    if last is not None:
        _say("  删除旧目录失败：%s（%s）" % (venv_dir, last))
    return not os.path.isdir(venv_dir)


def create_venv(venv_dir, host_exe):
    """先把新 venv 建在 .venv.new，验证通过后再替换 .venv。

    这样创建失败时**不会弄丢**原来的 .venv —— 换主机/换目录最怕的就是
    "修不好还把能用的环境删了"。
    """
    new_dir = venv_dir + ".new"
    old_dir = venv_dir + ".old"
    if not remove_venv(new_dir) or not remove_venv(old_dir):
        _say("  清理临时目录失败，请手动删除 .venv.new / .venv.old 后重试。")
        return False

    _say("  正在创建 .venv（%s）…" % host_exe)
    try:
        rc, out = _capture([host_exe, "-m", "venv", new_dir], timeout=180)
    except Exception as e:  # noqa: BLE001
        _say("  创建失败：%s: %s" % (type(e).__name__, e))
        remove_venv(new_dir)
        return False
    if rc != 0:
        # 有些环境（受限权限、精简发行版）里 ensurepip 会返回非 0，
        # 但 python.exe 已经建好了；只要解释器能跑就继续，让 launch.py 再补依赖。
        if os.path.isfile(venv_python(new_dir)) and venv_works(new_dir) is not None:
            _say("  提示：venv 已创建，但 pip 初始化返回 %d（启动器会再尝试安装依赖）。" % rc)
        else:
            _say("  创建失败（退出码 %d）：" % rc)
            if (out or "").strip():
                _say("  " + (out or "").strip()[:600])
            remove_venv(new_dir)
            return False

    py = venv_python(new_dir)
    if not os.path.isfile(py):
        _say("  创建后找不到 %s" % py)
        remove_venv(new_dir)
        return False
    # 少数 Python 发行版不带 ensurepip；补一次，失败也不致命（launch.py 会给提示）
    try:
        rc, _ = _capture([py, "-m", "pip", "--version"], timeout=30)
        if rc != 0:
            _capture([py, "-m", "ensurepip", "--upgrade"], timeout=120)
    except Exception:  # noqa: BLE001
        pass

    # 交换：旧 .venv -> .old，新 .new -> .venv
    moved_old = False
    if os.path.isdir(venv_dir):
        try:
            os.replace(venv_dir, old_dir)
            moved_old = True
        except Exception as e:  # noqa: BLE001
            _say("  无法替换旧 .venv（可能还在运行）：%s" % e)
            remove_venv(new_dir)
            return False
    try:
        os.replace(new_dir, venv_dir)
    except Exception as e:  # noqa: BLE001
        _say("  新 .venv 就位失败：%s" % e)
        if moved_old:
            try:
                os.replace(old_dir, venv_dir)
            except Exception:  # noqa: BLE001
                pass
        remove_venv(new_dir)
        return False

    remove_venv(old_dir)  # 删不掉也不影响 .venv 本身
    return True


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

def _rewrite_or_note(venv_dir, host_exe, host_ver, note):
    """重写 pyvenv.cfg；失败时说明原因并返回 False（交给重建兜底）。"""
    try:
        rewrite_cfg(venv_dir, host_exe, host_ver)
        return True
    except Exception as e:  # noqa: BLE001
        note("  重写 pyvenv.cfg 失败：%s: %s" % (type(e).__name__, e))
        return False


def ensure_venv(venv_dir, host_exe, host_ver, quiet=False):
    """让 venv_dir 可用；返回 True/False。"""
    def note(text):
        if not quiet:
            _say(text)

    venv_dir = os.path.abspath(venv_dir)
    host_exe = os.path.abspath(host_exe)
    host_ver = tuple(host_ver)

    if len(host_ver) < 2 or minor_of(host_ver) < MIN_PYTHON:
        note("  Python 版本过低：需要 %d.%d 及以上。" % MIN_PYTHON)
        return False

    cfg_path = os.path.join(venv_dir, "pyvenv.cfg")
    current = venv_works(venv_dir)

    # ① venv 能启动，且与宿主 Python 同一 major.minor：只重定向
    if current is not None and same_minor(current, host_ver):
        if _rewrite_or_note(venv_dir, host_exe, host_ver, note) \
                and venv_works(venv_dir) is not None:
            note("  .venv 已对齐：%s" % host_exe)
            return True
        note("  重定向后 .venv 仍不可用，改为重建。")

    # ② venv 启动不了，但配置里记录的版本与宿主一致：先尝试重定向（项目被整体搬走）
    elif current is None and os.path.isfile(cfg_path):
        cfg_ver = version_tuple(read_cfg(cfg_path).get("version", ""))
        if same_minor(cfg_ver, host_ver):
            note("  检测到 .venv 指向的 Python 不在了，按当前主机 Python 重定向…")
            if _rewrite_or_note(venv_dir, host_exe, host_ver, note) \
                    and venv_works(venv_dir) is not None:
                note("  已重定向到：%s" % host_exe)
                return True
            note("  重定向后仍不可用，改为重建。")
        else:
            note("  .venv 的 Python 版本（%s）与当前主机（%s）不同，重建。"
                 % (".".join(map(str, cfg_ver)) or "未知",
                    ".".join(map(str, host_ver[:3]))))
    else:
        note("  没有可用的 .venv，创建。")

    # ③ 重建
    # 重建 = 旧 venv 里的依赖全部作废，下次启动要重新 pip install（1–3 分钟）。
    # 这一步以前只在上面那几行里轻描淡写地提一句"重建"，用户会误以为是启动变慢，
    # 实际上是"环境被换掉了、包要重装"。所以这里明确说清楚 + 给出避免它的办法。
    note("  ⚠️ 需要重建 .venv：已装的第三方包不会保留，")
    note("     下次启动会自动重新安装依赖（首次约 1–3 分钟，之后秒开）。")
    note("     如果只是把项目从别的目录拷过来、Python 版本没变，通常不该走到这一步；")
    note("     真走到了，也可以手动把旧的 .venv 改名回来再启动。")
    if not create_venv(venv_dir, host_exe):
        return False
    if venv_works(venv_dir) is None:
        note("  新 .venv 仍无法启动，请检查 Python 安装是否完整。")
        return False
    note("  .venv 已重建：%s" % host_exe)
    return True


def host_executable():
    """当前宿主 Python 的绝对路径。

    在 venv 里运行时 sys.executable 是 venv 的 python，要用 _base_executable
    才能拿到真正的基座；3.10 没有这个属性时退回 sys.executable。
    """
    return os.path.abspath(getattr(sys, "_base_executable", None) or sys.executable)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    venv_dir = os.path.abspath(argv[0]) if argv else DEFAULT_VENV
    host_exe = host_executable()
    host_ver = version_tuple(sys.version)

    _say("=" * 62)
    _say("  准备 .venv（跟随当前主机的 Python）")
    _say("  宿主 Python：%s" % host_exe)
    _say("  版本：%s" % ".".join(str(x) for x in host_ver[:3]))
    _say("=" * 62)

    if len(host_ver) < 2 or minor_of(host_ver) < MIN_PYTHON:
        _say("  需要 Python %d.%d 及以上，当前是 %s。"
             % (MIN_PYTHON[0], MIN_PYTHON[1],
                ".".join(str(x) for x in host_ver[:3]) or "未知"))
        return 1

    if ensure_venv(venv_dir, host_exe, host_ver):
        py = venv_python(venv_dir)
        _say("")
        _say("✓ .venv 可用：%s" % py)
        return 0
    _say("")
    _say("✗ .venv 准备失败。常见处理：")
    _say("  1) 关闭正在运行的服务；")
    _say("  2) 手动删除 .venv 文件夹；")
    _say("  3) 重新双击 一键启动.bat。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
