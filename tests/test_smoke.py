"""Final pre-commit smoke test: static whitelist, auth gates, route inventory."""
import json, os, sys

# 项目根目录：不要写死某台机器的绝对路径 —— 复制到别的目录就跑不起来了
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
# app.py 没有 FLASK_SECRET_KEY 会拒绝启动（会话签名密钥不能是可猜的默认值）
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
sys.path.insert(0, ROOT)
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
ok("站点图标 200", c.get("/favicon-paper.svg").status_code == 200)
ok("logo 200", c.get("/logo-paper.svg").status_code == 200)
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

# 3. 登录能过
c.post("/api/register", json={"username": "smoke", "password": "Passw0rd1"})
lg = c.post("/api/login", json={"username": "smoke", "password": "Passw0rd1"})
ok("注册+登录 200", lg.status_code == 200, lg.status_code)

# 3b. 跨用户隔离：别人的工作流按 id 猜也读不到
wid = db.create_workflow(1, "机密研究目标", {"topic": "机密"})
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
