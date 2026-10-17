"""升级检查：init_db() 必须能给**已存在**的库补上新表。

老用户的 literature.db 里没有 workflows / workflow_steps 两张表。
如果 init_db() 不补，升级后一调 /api/workflow 就报 "no such table"。
这里复制一份真实的库来跑（绝不改动真库本身）。
"""
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "literature.db")
# 副本放到确认可写的目录（见 tests/_writable_temp.py 的说明）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path  # noqa: E402

COPY = temp_db_path("_tmp_migrate.db")

sys.path.insert(0, ROOT)
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")

import database as db  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


if not os.path.exists(REAL):
    print("SKIP  没有 literature.db，跳过升级检查")
    sys.exit(0)

shutil.copyfile(REAL, COPY)
try:
    db.DB_PATH = COPY

    conn = sqlite3.connect(COPY)
    tables_before = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    users_before = conn.execute("SELECT COUNT(1) FROM users").fetchone()[0]
    conn.close()

    db.init_db()

    conn = sqlite3.connect(COPY)
    tables_after = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    users_after = conn.execute("SELECT COUNT(1) FROM users").fetchone()[0]
    conn.close()

    ok("升级后有了 workflows 表", "workflows" in tables_after, sorted(tables_after))
    ok("升级后有了 workflow_steps 表", "workflow_steps" in tables_after)
    ok("原有用户数据没被动过", users_before == users_after,
       f"{users_before} -> {users_after}")

    # 新表真的可写可读（只建表不能算通过）
    wid = db.create_workflow(1, "迁移验证", {"topic": "t"})
    db.set_workflow_step(wid, "search", "done", "ok")
    row = db.get_workflow(wid, user_id=1)
    ok("新表可写可读", bool(row) and row["steps"]["search"]["status"] == "done", row)
finally:
    if os.path.exists(COPY):
        os.remove(COPY)

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
