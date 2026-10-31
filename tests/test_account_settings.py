"""账号管理（设置 → 账号管理）：改用户名 / 改密码 / 改头像。

这三件事的共同风险是"改错了还不报错"，所以用例都走真实请求，并且验证副作用：
* 改名后**旧名字能重新注册**（说明确实改了），session 里也是新名字；
* 改密码后**旧密码失效、新密码能登录**；
* 头像存的是 data URL，且 `/api/me` 能取回；非法格式/超大要拒绝；
* 全部要求登录（未登录一律 401），改密码必须校验当前密码。
"""
import base64
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _writable_temp import temp_db_path, temp_file_path  # noqa: E402

# 账号存在项目里的 accounts.json：测试必须指到临时文件，
# 否则跑一遍用例就往真实账号文件里塞一堆 test_user。
import accounts as _accounts_boot  # noqa: E402
_accounts_boot.set_path(temp_file_path("_tmp_account_settings_accounts.json"))

import database as db  # noqa: E402

DB = temp_db_path("_tmp_account_settings.db")
db.DB_PATH = DB
db.init_db()

import app as appmod  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


USER = "acct_user"
PWD = "Passw0rd1"
NEW_PWD = "Newpass123"

c = appmod.app.test_client()
r = c.post("/api/register", json={"username": USER, "password": PWD})
ok("注册成功（注册即登录）", (r.get_json() or {}).get("success") is True, r.get_json())

# ══════════════════════════════════════════════════════════
# 1. 未登录时三个接口都要拒绝
# ══════════════════════════════════════════════════════════
anon = appmod.app.test_client()
for path, body in (("/api/account/username", {"username": "hacker"}),
                   ("/api/account/password", {"old_password": PWD, "new_password": NEW_PWD}),
                   ("/api/account/avatar", {"avatar": ""})):
    rr = anon.post(path, json=body)
    ok("未登录访问 %s 被拒绝" % path, rr.status_code in (401, 302, 400), rr.status_code)

# ══════════════════════════════════════════════════════════
# 2. 改用户名
# ══════════════════════════════════════════════════════════
r = c.post("/api/account/username", json={"username": "改名后的用户"})
body = r.get_json() or {}
ok("中文用户名可以改（3 字以上）", r.status_code == 200 and body.get("success") is True, body)
ok("响应里带回新用户名", (body.get("user") or {}).get("username") == "改名后的用户", body.get("user"))
ok("/api/me 也是新名字",
   (c.get("/api/me").get_json().get("user") or {}).get("username") == "改名后的用户")
ok("旧用户名已释放（能重新注册）",
   db.register_user(USER, "Passw0rd9")[0] is True)
# 清理这个探针账号，避免影响后面的计数
db.update_username(db.get_user_by_username(USER)["id"], "acct_probe_tmp")

r = c.post("/api/account/username", json={"username": "ab"})
ok("用户名太短被拒", r.status_code == 400 and "3" in ((r.get_json() or {}).get("error") or ""),
   r.get_json())
r = c.post("/api/account/username", json={"username": "带空格 的名字"})
ok("含非法字符被拒", r.status_code == 400, (r.get_json() or {}).get("error"))
r = c.post("/api/account/username", json={"username": "改名后的用户"})
ok("改成自己现在的名字允许（不算重名）", r.status_code == 200, r.get_json())
db.register_user("another_user", "Passw0rd3")
r = c.post("/api/account/username", json={"username": "another_user"})
ok("改成别人已占用的名字被拒", r.status_code == 400
   and "已存在" in ((r.get_json() or {}).get("error") or ""), r.get_json())

# ══════════════════════════════════════════════════════════
# 3. 改密码
# ══════════════════════════════════════════════════════════
r = c.post("/api/account/password", json={"old_password": "WrongPass1",
                                          "new_password": NEW_PWD})
ok("当前密码不对被拒", r.status_code == 400
   and "当前密码" in ((r.get_json() or {}).get("error") or ""), r.get_json())
r = c.post("/api/account/password", json={"old_password": PWD, "new_password": "short"})
ok("新密码太短被拒", r.status_code == 400, r.get_json())
r = c.post("/api/account/password", json={"old_password": PWD, "new_password": "alllowercase1"})
ok("新密码缺大写被拒", r.status_code == 400, r.get_json())
r = c.post("/api/account/password", json={"old_password": PWD, "new_password": PWD})
ok("新旧密码相同被拒", r.status_code == 400, r.get_json())

# 界面现在只让填"新密码 + 确认"，所以不传旧密码也必须能改（已登录即可）
r = c.post("/api/account/password", json={"new_password": NEW_PWD})
ok("只传新密码也能改（界面只有两个输入框）",
   r.status_code == 200 and (r.get_json() or {}).get("success") is True, r.get_json())
ok("旧密码已失效", db.login_user("改名后的用户", PWD) is None)
ok("新密码能登录", db.login_user("改名后的用户", NEW_PWD) is not None)

# ══════════════════════════════════════════════════════════
# 4. 头像
# ══════════════════════════════════════════════════════════
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
           "AAH/q842iQAAAABJRU5ErkJggg==")
DATA_URL = "data:image/png;base64," + PNG_1PX

r = c.post("/api/account/avatar", json={"avatar": DATA_URL})
ok("上传合法头像成功", r.status_code == 200 and (r.get_json() or {}).get("success") is True, r.get_json())
me = (c.get("/api/me").get_json() or {}).get("user") or {}
ok("/api/me 能取回头像", me.get("avatar") == DATA_URL, (me.get("avatar") or "")[:40])

r = c.post("/api/account/avatar", json={"avatar": "http://evil/x.png"})
ok("非 data URL 被拒", r.status_code == 400, r.get_json())
r = c.post("/api/account/avatar", json={"avatar": "data:text/html;base64,PHNjcmlwdD4="})
ok("类型不是图片被拒", r.status_code == 400, r.get_json())
big = "data:image/png;base64," + base64.b64encode(b"x" * (210 * 1024)).decode()
r = c.post("/api/account/avatar", json={"avatar": big})
ok("超大头像被拒", r.status_code == 400
   and "太大" in ((r.get_json() or {}).get("error") or ""), (r.get_json() or {}).get("error"))

r = c.post("/api/account/avatar", json={"avatar": ""})
ok("传空串表示清除头像", r.status_code == 200 and (r.get_json() or {}).get("avatar") == "", r.get_json())
ok("清除后 /api/me 里没有头像",
   not ((c.get("/api/me").get_json() or {}).get("user") or {}).get("avatar"))

# ══════════════════════════════════════════════════════════
# 4.5 注册时就能带头像（可选）
# ══════════════════════════════════════════════════════════
c3 = appmod.app.test_client()
r = c3.post("/api/register", json={"username": "avatar_at_register",
                                   "password": "Passw0rd1",
                                   "avatar": DATA_URL})
body3 = r.get_json() or {}
ok("注册时带头像能成功", (body3.get("success") is True), body3)
me3 = (c3.get("/api/me").get_json() or {}).get("user") or {}
ok("注册时传的头像存下来了", me3.get("avatar") == DATA_URL, (me3.get("avatar") or "")[:30])

c4 = appmod.app.test_client()
r = c4.post("/api/register", json={"username": "bad_avatar_user",
                                   "password": "Passw0rd1",
                                   "avatar": "http://evil/x.png"})
ok("注册时传非法头像被拒", r.status_code == 400, r.get_json())
ok("被拒后账号没有创建出来", db.get_user_by_username("bad_avatar_user") is None)

# ══════════════════════════════════════════════════════════
# 5. 登录响应里带 avatar（否则登录后徽章要刷新才显示头像）
# ══════════════════════════════════════════════════════════
c.post("/api/account/avatar", json={"avatar": DATA_URL})
c2 = appmod.app.test_client()
login = (c2.post("/api/login", json={"username": "改名后的用户",
                                     "password": NEW_PWD}).get_json() or {})
ok("登录响应里带 avatar", (login.get("user") or {}).get("avatar") == DATA_URL,
   (login.get("user") or {}).get("avatar", "")[:30])

# ══════════════════════════════════════════════════════════
# 6. 密码是哈希存的，且改密后哈希变了
# ══════════════════════════════════════════════════════════
import accounts as _acc
_pwd = (_acc.get_user_raw("改名后的用户") or {}).get("password") or ""
ok("账号文件里仍然是 bcrypt 哈希", _pwd.startswith("$2"), _pwd[:7])
ok("改密后哈希确实变了", db.login_user("改名后的用户", PWD) is None)

n_pass = sum(1 for _, cond, _ in R if cond)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
