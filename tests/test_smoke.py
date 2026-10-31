"""Final pre-commit smoke test: static whitelist, auth gates, route inventory."""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
from _writable_temp import temp_db_path  # noqa: E402

import database as db
DB = temp_db_path("_tmp_smoke.db")
db.DB_PATH = DB

import app as appmod

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


c = appmod.app.test_client()

# 1. 静态白名单：源码绝不能通过 /<path:filename> 泄露
ok("首页 200", c.get("/").status_code == 200)
ok("站点图标 200", c.get("/favicon-owl-32.png").status_code == 200)
ok("logo 200", c.get("/logo-owl-64.png").status_code == 200)
ok("登录页 logo 200", c.get("/logo-owl-128.png").status_code == 200)
ok("欢迎插画 200", c.get("/welcome-owl.webp").status_code == 200)
ok("欢迎插画是 image/webp（别发成 octet-stream）",
   c.get("/welcome-owl.webp").headers.get("Content-Type", "").startswith("image/webp"),
   c.get("/welcome-owl.webp").headers.get("Content-Type"))
for leak in ("app.py", "database.py", "workflow.py", "Agent.py", "view.py",
             "requirements.txt", ".env", ".gitignore", "providers/__init__.py",
             "providers/openalex.py", "literature.db"):
    r = c.get("/" + leak)
    ok(f"源码不外泄: /{leak} -> {r.status_code}", r.status_code == 404, r.status_code)

# 2. 未登录的鉴权闸门
ok("未登录建工作流 401", c.post("/api/workflow", json={"goal": "x"}).status_code == 401)
ok("未登录读工作流 401", c.get("/api/workflow/1").status_code == 401)
ok("未登录中断 401", c.post("/api/workflow/1/interrupt").status_code == 401)
ok("未登录删工作流 401", c.delete("/api/workflow/1").status_code == 401)
ok("未登录润色 401", c.post("/api/polish", json={"abstract": "x"}).status_code == 401)
ok("未登录对话记录 401", c.get("/api/history").status_code == 401)
ok("未登录收藏 401", c.get("/api/favorites").status_code == 401)
ok("健康检查 200", c.get("/api/health").status_code == 200)

# 首页必须禁缓存：前端只有一个 index.html，缓存住就会出现
# "新 JS + 旧 HTML"的混合状态（点了功能一片空白就是这么来的）
_home = c.get("/")
_cc = (_home.headers.get("Cache-Control") or "").lower()
ok("首页禁止缓存（no-store）", "no-store" in _cc, _home.headers.get("Cache-Control"))
ok("首页带 Pragma: no-cache", (_home.headers.get("Pragma") or "").lower() == "no-cache",
   _home.headers.get("Pragma"))

# 3. 登录能过
c.post("/api/register", json={"username": "smoke", "password": "Passw0rd1"})
lg = c.post("/api/login", json={"username": "smoke", "password": "Passw0rd1"})
ok("注册+登录 200", lg.status_code == 200, lg.status_code)
# 账号改存 accounts.json 后，新账号的 id 不再是"必然 1"（测试环境里可能有别的账号），
# 所以这里用接口拿真实 id，别再硬编码。
UID = ((c.get("/api/me").get_json() or {}).get("user") or {}).get("id")
ok("拿到当前用户 id", isinstance(UID, int) and UID > 0, UID)

# 3b. 跨用户隔离：别人的工作流按 id 猜也读不到
wid = db.create_workflow(UID, "机密研究目标", {"topic": "机密"})
other = appmod.app.test_client()
other.post("/api/register", json={"username": "intruder", "password": "Passw0rd1"})
other.post("/api/login", json={"username": "intruder", "password": "Passw0rd1"})
ok("他人读我的工作流 -> 404", other.get(f"/api/workflow/{wid}").status_code == 404,
   other.get(f"/api/workflow/{wid}").status_code)
ok("他人删我的工作流 -> 404", other.delete(f"/api/workflow/{wid}").status_code == 404)
ok("本人读自己的 -> 200", c.get(f"/api/workflow/{wid}").status_code == 200)

# 4. 路由清单：确认工作流四个接口都在
routes = sorted(str(r) for r in appmod.app.url_map.iter_rules())
for want in ("/api/workflow", "/api/workflow/<int:workflow_id>",
             "/api/workflow/<int:workflow_id>/interrupt"):
    ok(f"路由存在 {want}", any(want in r for r in routes), want)
wf_routes = [r for r in routes if "workflow" in r]
ok("工作流路由共 4 条", len(wf_routes) == 4, wf_routes)

# 5. 数据库表结构齐全
conn = db.get_db()
tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
conn.close()
for t in ("users", "search_history", "favorites", "conversations",
          "conversation_messages", "workflows", "workflow_steps"):
    ok(f"表存在 {t}", t in tables, sorted(tables))

n_pass = sum(1 for _, ok_, _ in R if ok_)
for name, ok_, got in R:
    if not ok_:
        print(f"FAIL  {name}  -> got: {got!r}")
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
