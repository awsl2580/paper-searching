"""账号持久化：注册一次，以后每次运行都能直接登录（不用重新注册）。

覆盖的行为：
  * 注册写进 literature.db，**同时**同步一份 accounts.json 到项目目录；
  * accounts.json 里只有 bcrypt 哈希，没有明文密码；
  * 库没了 / 换目录（只剩 accounts.json）时，登录会自动把账号补回来；
  * 注册即自动登录（不用再手动登一次）；
  * 登录态是 30 天持久 cookie（关浏览器/重启服务都不用重登）；
  * 换一个进程（模拟"下次运行"）仍然能登录。

用法（在项目根目录）：
    python tests/test_accounts.py
"""
import importlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")
os.environ["FLASK_SECRET_KEY"] = "test-" + "a" * 48     # 必须是固定值，否则会话不可复现

from _writable_temp import temp_db_path, temp_file_path  # noqa: E402

import database as db  # noqa: E402

# 必须在 import app 之前改路径：app.py 导入时就会建表
DB = temp_db_path("_tmp_accounts.db")
ACCOUNTS = temp_file_path("_tmp_accounts.json")
db.DB_PATH = DB
db.ACCOUNTS_FILE = ACCOUNTS

import app as appmod  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


c = appmod.app.test_client()

# ── 1. 注册 ────────────────────────────────────────────────
r = c.post("/api/register", json={"username": "alice", "password": "Passw0rd1", "email": "a@x.com"})
body = r.get_json()
ok("注册返回成功", r.status_code == 200 and body.get("success"), (r.status_code, body))
ok("注册后直接拿到 user（自动登录）", isinstance(body.get("user"), dict))
ok("注册后 /api/me 已是登录态", c.get("/api/me").get_json().get("user", {}).get("username") == "alice",
   c.get("/api/me").get_json())

# ── 2. 落盘：库 + 项目里的 accounts.json ──────────────────
ok("literature.db 已生成", os.path.exists(DB), DB)
ok("accounts.json 已生成在项目里", os.path.exists(ACCOUNTS), ACCOUNTS)

with open(ACCOUNTS, "r", encoding="utf-8") as f:
    data = json.load(f)
ok("accounts.json 结构正确", isinstance(data.get("accounts"), list) and len(data["accounts"]) == 1,
   data.keys())
acc = data["accounts"][0]
ok("accounts.json 里有用户名", acc.get("username") == "alice", acc.get("username"))
ok("accounts.json 里存的是 bcrypt 哈希", str(acc.get("password", "")).startswith("$2"), acc.get("password", "")[:8])
ok("accounts.json 里没有明文密码", "Passw0rd1" not in json.dumps(data, ensure_ascii=False))

# ── 3. 登录（正常路径）＋密码错误要拒绝 ───────────────────
c2 = appmod.app.test_client()
ok("错误密码被拒绝", c2.post("/api/login", json={"username": "alice", "password": "WrongPass1"}).status_code == 401)
ok("正确密码能登录", c2.post("/api/login", json={"username": "alice", "password": "Passw0rd1"}).status_code == 200)

# ── 4. 持久登录态：cookie 必须带过期时间（30 天）─────────
r = c2.post("/api/login", json={"username": "alice", "password": "Passw0rd1"})
cookie = r.headers.get("Set-Cookie", "")
ok("登录下发持久 cookie（带 Expires/Max-Age）",
   "Expires=" in cookie or "Max-Age=" in cookie, cookie)

# ── 5. 模拟"库丢了 / 换了目录"：只剩 accounts.json 也能登录 ──
os.remove(DB)
ok("库删除后登录仍能找回账号（自动从 accounts.json 恢复）",
   db.login_user("alice", "Passw0rd1") is not None)
ok("恢复后库里确实有 alice 了", db.find_user("alice") is not None)

# ── 6. 模拟"下次运行"：另起一个 python 进程登录 ───────────
probe = temp_file_path("_tmp_accounts_probe.py")
with open(probe, "w", encoding="utf-8") as f:
    f.write(
        "import os, sys, json\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import database as db\n"
        f"db.DB_PATH = {DB!r}\n"
        f"db.ACCOUNTS_FILE = {ACCOUNTS!r}\n"
        "print(json.dumps({'login': db.login_user('alice', 'Passw0rd1')}))\n"
    )
env = dict(os.environ, PYTHONIOENCODING="utf-8")
proc = subprocess.run([sys.executable, probe], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", env=env, cwd=ROOT)
out = (proc.stdout or "").strip().splitlines()
last = out[-1] if out else ""
ok("换进程（下次运行）仍能登录同一账号",
   '"username": "alice"' in last, (proc.returncode, last, (proc.stderr or "")[-300:]))

# ── 7. 重复注册的提示要明确（避免用户以为账号丢了）────────
r = c.post("/api/register", json={"username": "alice", "password": "Passw0rd1"})
ok("重复注册返回“用户名已存在”", r.status_code == 400 and "已存在" in (r.get_json().get("error") or ""),
   (r.status_code, r.get_json()))

# ── 8. 会话时长配置 ────────────────────────────────────────
ok("会话有效期配置为 30 天", appmod.app.config["PERMANENT_SESSION_LIFETIME"].days == 30,
   appmod.app.config["PERMANENT_SESSION_LIFETIME"])

# ── 清理临时产物 ──────────────────────────────────────────
for p in (DB, ACCOUNTS, ACCOUNTS + ".tmp", probe):
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass

n_pass = sum(1 for _, ok_, _ in R if ok_)
for name, ok_, got in R:
    if not ok_:
        print(f"FAIL  {name}  -> got: {got!r}")
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
