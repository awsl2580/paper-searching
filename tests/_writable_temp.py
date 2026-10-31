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


def isolate_accounts(name="_test_accounts.json"):
    """把账号文件（accounts.json）指到临时目录。

    直接 `python tests/xxx.py` 跑单个用例时，run_all 设的 WF_TEST_TMP 并不存在，
    于是 accounts.py 会退回项目根目录 —— 用例里注册的 smoke/tester 就会写进
    你真实的账号文件（本项目已经被这么污染过一次）。
    所以在模块导入时就把 ACCOUNTS_FILE 钉到临时目录。
    """
    if os.environ.get("ACCOUNTS_FILE"):
        return os.environ["ACCOUNTS_FILE"]
    p = os.path.join(temp_dir(), name)
    os.environ["ACCOUNTS_FILE"] = p
    return p


def temp_file_path(name):
    p = os.path.join(temp_dir(), name)
    if os.path.exists(p):
        os.remove(p)
    return p


# 导入即生效：任何一个用例只要 import 了本模块，账号文件就不会落到项目根目录。
isolate_accounts()
