"""账号持久化测试：注册一次，之后每次启动直接登录。

用户的原话是"注册后把账号密码存在项目里，下次运行不用重新注册登录"。
这件事本来就有（账号在 literature.db 里，密码是 bcrypt 哈希），但踩了两个坑：

1. **数据库文件被误删** —— 我上一轮清理临时文件时把 literature.db 删了，
   于是"刚注册的账号没了"。所以这条用例专门验证：重新 init_db()、重启进程
   （用新的 test_client 模拟）之后账号仍然在。
2. **登录态没持久化** —— session 原来是"浏览器会话 cookie"，关掉浏览器就掉登录，
   每次打开都要再登一遍。现在支持「记住我」，用例会检查 Set-Cookie 里有没有
   Expires（持久 cookie 的标志）。

另外验证两条安全边界：
* /api/local-accounts 只返回用户名，**绝不能出现密码或哈希**；
* 非本机访问拿不到账号列表（公网部署不泄露用户名）。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
os.environ.pop("DEV_AUTO_LOGIN", None)
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _writable_temp import temp_db_path, temp_file_path  # noqa: E402

# 账号存在项目里的 accounts.json：测试指到临时文件，别污染真实账号。
import accounts as _accounts_boot  # noqa: E402
_accounts_boot.set_path(temp_file_path("_tmp_local_accounts.json"))

import database as db  # noqa: E402

DB = temp_db_path("_tmp_accounts.db")
db.DB_PATH = DB
db.init_db()

import app as appmod  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


USER = {"username": "persist_user", "password": "Passw0rd1"}

# ── 1. 注册即登录 ─────────────────────────────────────────────
c1 = appmod.app.test_client()
r = c1.post("/api/register", json=USER)
body = r.get_json() or {}
ok("注册成功", r.status_code == 200 and body.get("success") is True, body)
ok("注册后直接就是登录状态", bool(body.get("user", {}).get("username")), body.get("user"))
ok("注册响应里带保持登录天数", body.get("expires_days") == 30, body.get("expires_days"))
me = c1.get("/api/me").get_json()
ok("/api/me 认得出这个账号", (me.get("user") or {}).get("username") == USER["username"], me)

# ── 2. 模拟"重启"：新的 test_client（新进程里就是新会话）────────
c2 = appmod.app.test_client()
ok("新会话默认是未登录", (c2.get("/api/me").get_json() or {}).get("success") is not True)

# 数据库重新 init 一遍（模拟重启时 app.py 的 init_db()），账号不能丢
db.init_db()
ok("重新 init_db 之后账号还在", db.count_users() == 1, db.count_users())

# 用同一个账号密码登录 —— 这就是用户要的"下次启动直接登录，不用重新注册"
r2 = c2.post("/api/login", json=USER)
login_body = r2.get_json() or {}
ok("重启后能用同一套账号密码登录", r2.status_code == 200 and login_body.get("success") is True,
   login_body)
ok("登录后 /api/me 有账号", (c2.get("/api/me").get_json().get("user") or {}).get("username")
   == USER["username"])

# ── 3. 「记住我」= 持久 cookie（关掉浏览器也不掉）─────────────────
set_cookie = r2.headers.get("Set-Cookie", "")
ok("登录响应带 session cookie", "session=" in set_cookie, set_cookie[:80])
ok("勾了记住我 -> cookie 有 Expires（持久化）",
   "Expires=" in set_cookie, set_cookie[:120])

c3 = appmod.app.test_client()
r3 = c3.post("/api/login", json={**USER, "remember": False})
ok("不勾记住我也能登录", (r3.get_json() or {}).get("success") is True)
cookie3 = r3.headers.get("Set-Cookie", "")
ok("不勾记住我 -> cookie 没有 Expires（仅本次会话）",
   "Expires=" not in cookie3, cookie3[:120])

# ── 4. 登录失败的原因要具体（本机访问时）───────────────────────
r4 = c2.post("/api/login", json={"username": USER["username"], "password": "WrongPass1"})
b4 = r4.get_json() or {}
ok("密码错时 401", r4.status_code == 401, r4.status_code)
ok("密码错时提示账号存在、密码不对", "密码" in (b4.get("error") or ""), b4)

r5 = c2.post("/api/login", json={"username": "no_such_user", "password": "Passw0rd1"})
b5 = r5.get_json() or {}
ok("账号不存在时 401", r5.status_code == 401, r5.status_code)
ok("账号不存在时提示本机没有、并建议注册",
   "没有" in (b5.get("error") or "") and b5.get("action") == "register", b5)
ok("提示里列出本机已有账号", USER["username"] in (b5.get("hint") or ""), b5.get("hint"))

# ── 5. 本机账号列表：只给用户名，绝不泄露密码 ──────────────────
r6 = c2.get("/api/local-accounts")
b6 = r6.get_json() or {}
ok("本机访问能拿到账号列表", b6.get("success") is True and len(b6.get("accounts") or []) == 1, b6)
acc = (b6.get("accounts") or [{}])[0]
ok("列表里只有用户名与时间",
   set(acc) <= {"username", "created_at", "last_login_at"}, sorted(acc))
ok("列表里**没有**密码字段",
   not any(k for k in json.dumps(b6, ensure_ascii=False).lower()
           if k in ("password", "hash", "bcrypt") and k not in ("note",)),
   list(b6))
ok("响应里说明了账号存放在哪",
   "literature.db" in (b6.get("note") or ""), b6.get("note"))
ok("登录后 last_login_at 被记录", bool(acc.get("last_login_at")), acc)

# 非本机访问不给列表（用代理头伪造来源，werkzeug test client 的 remote_addr 可用 environ 覆盖）
r7 = appmod.app.test_client().get("/api/local-accounts",
                                  environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
b7 = r7.get_json() or {}
ok("非本机访问拿不到账号列表",
   b7.get("success") is True and (b7.get("accounts") or []) == [], b7)
ok("非本机访问会说明原因", bool(b7.get("reason")), b7.get("reason"))

# ── 6. 账号唯一性与登出 ───────────────────────────────────────
r8 = c2.post("/api/register", json=USER)
ok("重复注册同名账号会被拒", r8.status_code == 400
   and "已存在" in ((r8.get_json() or {}).get("error") or ""), r8.get_json())

c2.post("/api/logout")
ok("登出后 /api/me 不再有账号",
   (c2.get("/api/me").get_json() or {}).get("success") is not True)
r9 = c2.post("/api/login", json=USER)
ok("登出后还能再登录", (r9.get_json() or {}).get("success") is True)

# ── 7. 密码确实不是明文存的（直接读账号文件）───────────────────
# 账号现在存项目里的 accounts.json（不是数据库），所以直接看文件内容。
import accounts as _acc
_raw = (_acc.get_user_raw(USER["username"]) or {}).get("password") or ""
ok("账号存在文件里（不是数据库）", bool(_raw))
ok("文件里存的不是明文密码", _raw != USER["password"], _raw[:12] + "…")
ok("文件里存的是 bcrypt 哈希", _raw.startswith("$2"), _raw[:7])

# ── 8. 多账号并存（本机可以有多个号）──────────────────────────
c4 = appmod.app.test_client()
c4.post("/api/register", json={"username": "second_user", "password": "Passw0rd2"})
ok("第二个账号也能注册并自动登录",
   (c4.get("/api/me").get_json().get("user") or {}).get("username") == "second_user")
names = [a["username"] for a in db.list_users()]
ok("账号列表里有两个号", len(names) == 2 and "second_user" in names, names)
ok("总数统计正确", db.count_users() == 2, db.count_users())

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
