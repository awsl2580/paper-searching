"""账号文件存储（accounts.json）的用例。

需求是"账号数据直接存在项目里，暂时不用存数据库"，所以这里专门验证：
* 注册/登录/改名/改密/改头像都落到 **accounts.json**，而不是 literature.db；
* 文件里**没有明文密码**（bcrypt 哈希）；
* 重启（重新 import / 重新读文件）后账号还在；
* 老数据库里的账号能**迁移**过来，且已有账号文件时不会覆盖；
* 写入是原子的（先 .tmp 再替换），坏文件会被改名留证而不是直接丢；
* 测试环境（设了 WF_TEST_TMP）不会污染真实账号文件。
"""
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _writable_temp import temp_dir, temp_file_path  # noqa: E402

import accounts  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ── 1. 路径与隔离 ────────────────────────────────────────────
ACC = temp_file_path("_tmp_accounts_store.json")
accounts.set_path(ACC)
ok("set_path 能改账号文件路径", accounts.path() == ACC, accounts.path())
ok("初始账号数为 0", accounts.count_users() == 0, accounts.count_users())

# ── 2. 注册 / 登录 ──────────────────────────────────────────
ok("注册成功", accounts.register_user("文件甲", "Passw0rd1")[0] is True)
ok("重名注册被拒", accounts.register_user("文件甲", "Passw0rd1")[0] is False)
ok("账号数 1", accounts.count_users() == 1, accounts.count_users())
ok("用户名存在性查询", accounts.user_exists("文件甲") and not accounts.user_exists("不存在"))
ok("登录成功", (accounts.login_user("文件甲", "Passw0rd1") or {}).get("username") == "文件甲")
ok("密码错登录失败", accounts.login_user("文件甲", "WrongPass1") is None)

# ── 3. 落盘位置与内容 ───────────────────────────────────────
ok("账号文件已生成", os.path.isfile(ACC))
raw = io.open(ACC, encoding="utf-8").read()
ok("文件里是 JSON（能直接打开看）", json.loads(raw).get("version") == 1)
data = json.loads(raw)
ok("文件里有 users 数组", isinstance(data.get("users"), list) and len(data["users"]) == 1)
pwd = data["users"][0]["password"]
ok("密码不是明文", pwd != "Passw0rd1", pwd[:10])
ok("密码是 bcrypt 哈希", pwd.startswith("$2"), pwd[:7])
ok("文件里没有 literature.db 之类的东西", "sqlite" not in raw.lower())

# ── 4. 重启后仍在（重新读文件）────────────────────────────────
accounts.reset_path()
accounts.set_path(ACC)
ok("重新读取后账号还在", accounts.count_users() == 1)
ok("重新读取后能登录", (accounts.login_user("文件甲", "Passw0rd1") or {}).get("id") == 1)
accounts.set_path(ACC)

# ── 5. 改名 / 改密 / 改头像 ─────────────────────────────────
uid = accounts.get_user_by_username("文件甲")["id"]
ok("改名成功", accounts.update_username(uid, "文件乙")[0] is True)
ok("改名后旧名字查不到", accounts.get_user_by_username("文件甲") is None)
ok("改名后新名字能查到", accounts.get_user_by_username("文件乙") is not None)

accounts.register_user("文件丙", "Passw0rd2")
ok("改成别人占用的名字被拒", accounts.update_username(uid, "文件丙")[0] is False)
ok("改成自己现在的名字允许", accounts.update_username(uid, "文件乙")[0] is True)

ok("当前密码校验（对）", accounts.verify_password(uid, "Passw0rd1") is True)
ok("当前密码校验（错）", accounts.verify_password(uid, "x") is False)
ok("改密成功", accounts.update_password(uid, "Newpass123")[0] is True)
ok("旧密码失效", accounts.login_user("文件乙", "Passw0rd1") is None)
ok("新密码可登录", accounts.login_user("文件乙", "Newpass123") is not None)

AVATAR = "data:image/jpeg;base64,AAAA"
ok("头像写入成功", accounts.update_avatar(uid, AVATAR)[0] is True)
ok("头像能读回", (accounts.get_user_by_id(uid) or {}).get("avatar") == AVATAR)
ok("对外接口不返回密码字段", "password" not in (accounts.get_user_by_id(uid) or {}))
ok("清空头像", accounts.update_avatar(uid, "")[0] is True
   and not (accounts.get_user_by_id(uid) or {}).get("avatar"))

# ── 6. 列表与计数 ───────────────────────────────────────────
lst = accounts.list_users()
ok("列表只有用户名与时间", all(set(x) <= {"username", "created_at", "last_login_at"} for x in lst), lst[:1])
ok("列表按最近活跃排序（登录过的在前）",
   lst[0]["username"] in ("文件乙", "文件丙"), [x["username"] for x in lst])

# ── 7. 坏文件不会让程序崩 ───────────────────────────────────
bad = temp_file_path("_tmp_accounts_bad.json")
io.open(bad, "w", encoding="utf-8").write("{ 这不是合法 JSON")
accounts.set_path(bad)
ok("坏文件读成空结构（不抛异常）", accounts.count_users() == 0)
ok("坏文件被改名留证", os.path.isfile(bad + ".broken"))

# ── 8. 原子写：不该留下 .tmp ────────────────────────────────
accounts.set_path(ACC)
accounts.register_user("文件丁", "Passw0rd3")
ok("写入后没有残留 .tmp", not os.path.isfile(ACC + ".tmp"))

# ── 9. 从老数据库迁移 ───────────────────────────────────────
try:
    import sqlite3
    dbp = temp_file_path("_tmp_accounts_migrate.db")
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT,"
                 " email TEXT, created_at TEXT, last_login_at TEXT)")
    conn.execute("INSERT INTO users (username, password, email, created_at) VALUES (?,?,?,?)",
                 ("老账号", "$2b$12$abcdefghijklmnopqrstuv", "", "2026-01-01 00:00:00"))
    conn.commit()
    conn.close()

    fresh = temp_file_path("_tmp_accounts_fresh.json")
    accounts.set_path(fresh)
    rep = accounts.migrate_from_db(dbp)
    ok("迁移报告了数量", rep.get("migrated") == 1, rep)
    ok("迁移后账号可用", accounts.user_exists("老账号"))
    ok("迁移后密码哈希原样保留",
       (accounts.get_user_raw("老账号") or {}).get("password").startswith("$2b$"))

    # 已存在账号文件时不能再迁移（否则会覆盖后来的修改）
    rep2 = accounts.migrate_from_db(dbp)
    ok("已有账号文件时跳过迁移", rep2.get("skipped") is True, rep2)
    ok("跳过迁移后账号数没变", accounts.count_users() == 1)
finally:
    accounts.set_path(ACC)

# ── 10. 测试环境不污染真实账号文件 ───────────────────────────
saved = os.environ.get("WF_TEST_TMP")
try:
    os.environ["WF_TEST_TMP"] = temp_dir()
    accounts.reset_path()
    p = accounts.path()
    ok("设了 WF_TEST_TMP 时账号文件落到临时目录",
       p.startswith(temp_dir()) and p != os.path.join(ROOT, "accounts.json"), p)
finally:
    if saved is None:
        os.environ.pop("WF_TEST_TMP", None)
    else:
        os.environ["WF_TEST_TMP"] = saved
    accounts.reset_path()
    accounts.set_path(ACC)

# 清理
for f in (ACC, ACC + ".broken", ACC + ".tmp"):
    try:
        os.remove(f)
    except Exception:
        pass

n_pass = sum(1 for _, cond, _ in R if cond)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
