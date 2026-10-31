"""管理员页面：隐藏路由、登录、用户列表、普通用户拦截。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path, temp_file_path  # noqa: E402

# 账号/数据库都指向临时文件，绝不碰真实 accounts.json / literature.db
os.environ["ACCOUNTS_FILE"] = temp_file_path("_tmp_admin_accounts.json")
import accounts  # noqa: E402
import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_admin.db")
db.init_db()
accounts.register_user("alice", "AlicePass1", "a@example.com")
accounts.register_user("bob", "BobPass123", "")

import app as appmod  # noqa: E402

appmod.ADMIN_USER = "admin"
appmod.ADMIN_PASSWORD = "admin"
app = appmod.app
app.config.update(TESTING=True)
c = app.test_client()

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def body(resp):
    return resp.get_data(as_text=True)


login = c.get("/admin/")
ok("未登录访问 /admin/ 显示管理员登录页", login.status_code == 200
   and "管理员登录" in body(login), login.status_code)

no_slash = c.get("/admin")
ok("不带斜杠的 /admin 回首页", no_slash.status_code == 302
   and (no_slash.headers.get("Location") or "").endswith("/"), no_slash.headers.get("Location"))

wrong = c.post("/admin/login", data={"username": "admin", "password": "bad"})
ok("管理员密码错误返回 401", wrong.status_code == 401 and "账号或密码不正确" in body(wrong),
   wrong.status_code)

good = c.post("/admin/login", data={"username": "admin", "password": "admin"})
ok("admin/admin 登录成功并跳控制台",
   good.status_code == 302 and (good.headers.get("Location") or "").endswith("/admin/"),
   good.headers.get("Location"))

dash = c.get("/admin/")
dash_body = body(dash)
ok("控制台能看到当前用户", dash.status_code == 200
   and "管理员控制台" in dash_body and "alice" in dash_body and "bob" in dash_body,
   dash.status_code)
ok("控制台不泄露密码哈希", "$2b$" not in dash_body and "password" not in dash_body.lower())
ok("控制台禁止缓存与收录",
   "no-store" in (dash.headers.get("Cache-Control") or "")
   and "noindex" in (dash.headers.get("X-Robots-Tag") or ""),
   [dash.headers.get("Cache-Control"), dash.headers.get("X-Robots-Tag")])

out = c.post("/admin/logout")
ok("退出管理员后回首页", out.status_code == 302
   and (out.headers.get("Location") or "").endswith("/"), out.headers.get("Location"))
ok("退出后 /admin/ 回到登录页", "管理员登录" in body(c.get("/admin/")))

# 普通登录用户：访问 /admin/ 或尝试管理员登录，都直接回首页
with c.session_transaction() as s:
    s.pop("admin_ok", None)
    s["user_id"] = 1
    s["username"] = "alice"
normal = c.get("/admin/")
ok("普通用户访问 /admin/ 被弹回首页", normal.status_code == 302
   and (normal.headers.get("Location") or "").endswith("/"), normal.headers.get("Location"))
normal_login = c.post("/admin/login", data={"username": "admin", "password": "admin"})
ok("普通用户不能从 /admin/login 变成管理员", normal_login.status_code == 302
   and (normal_login.headers.get("Location") or "").endswith("/"),
   normal_login.headers.get("Location"))

html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
ok("主页没有管理员入口链接", "/admin/" not in html and 'href="/admin"' not in html)

n_pass = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
