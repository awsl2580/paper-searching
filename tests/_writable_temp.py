"""Point every test's temp artifacts at a writable directory.

Why this helper exists: the suites default to writing `_tmp_*.db` next to
themselves, i.e. inside `AIagent2/AIagent1/tests/`. That path is not writable
under the current DSH workspace grant, so every suite failed with
`sqlite3.OperationalError: unable to open database file` -- a sandbox/permission
problem, not a code problem.

Resolution order, first writable wins:
  1. WF_TEST_TMP                （run_all.py 会设成工作区的 .dsh_testtmp）
  2. TMPDIR / TEMP              （DSH 给每次作业建的临时目录）
  3. 目录自身                   （正常机器上就是这么用的，行为不变）

Import this at the TOP of a test file, before overriding `db.DB_PATH`:
    from _writable_temp import temp_db_path
    db.DB_PATH = temp_db_path("smoke.db")
"""
import atexit
import os
import sqlite3
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _usable(d):
    """这个目录能不能真的建库？能就返回它。"""
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, "_probe.db")
        conn = sqlite3.connect(probe)
        conn.execute("CREATE TABLE t(a)")
        conn.commit()
        conn.close()
        os.remove(probe)
        return d
    except Exception:
        return None


def temp_dir():
    for cand in (os.environ.get("WF_TEST_TMP"),
                 os.environ.get("TMPDIR"),
                 os.environ.get("TEMP"),
                 tempfile.gettempdir(),
                 _HERE):
        got = _usable(cand)
        if got:
            return got
    raise RuntimeError("找不到任何可写目录来放测试用的临时数据库")


def temp_db_path(name):
    """返回一个可写的 _tmp_*.db 绝对路径（存在则先删掉，保证干净）。"""
    p = os.path.join(temp_dir(), name)
    if os.path.exists(p):
        os.remove(p)
    return p


def temp_file_path(name):
    p = os.path.join(temp_dir(), name)
    if os.path.exists(p):
        os.remove(p)
    return p


# 账号备份也要重定向。为什么：测试里 register 一次就会 export 一份
# accounts.json，而它默认落在**项目目录**——跑完测试，项目里就多出一堆
# "测试账号"（还会被下一次 import 自动合并进临时库，把用户 id 搅乱）。
# database.py 读 PAPER_ACCOUNTS_FILE，这里在 import 时就把它指到临时目录。
# 文件名带 PID：每个测试进程一份，避免 A 用例注册的账号被 B 用例的
# init_db() 合并进来，把 user id 顶掉（好几个用例假设第一个注册用户 id=1）。
# 注意：本模块必须在 `import database` 之前导入（现有用例都是这么写的）。
_ACCOUNTS_TMP = os.path.join(temp_dir(), f"_tmp_accounts_{os.getpid()}.json")
os.environ.setdefault("PAPER_ACCOUNTS_FILE", _ACCOUNTS_TMP)


def _cleanup_accounts_tmp():
    try:
        if os.path.exists(_ACCOUNTS_TMP):
            os.remove(_ACCOUNTS_TMP)
    except OSError:
        pass


atexit.register(_cleanup_accounts_tmp)
