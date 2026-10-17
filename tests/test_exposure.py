"""公网暴露前的安全检查：源码/密钥/数据库不能通过 HTTP 拿到。

为什么要单独一个用例：这个项目会挂到公网域名（natapp 内网穿透）上，
而它历史上真的漏过 —— 早期用 `Flask(static_folder="static", static_url_path="")`
把整个 static/ 目录托管出去，`GET /app.py` 能直接下载到 21715 字节的源码。
现在改成白名单了，但"以后有人往 static/ 里丢个 .bak/.sql 就又漏了"，
所以把这条固化成测试，而不是靠记性。

同时检查：登录态签名密钥不能是代码里那个公开默认值。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")

from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_sec.db")

import app as appmod  # noqa: E402

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ── 1. 会话密钥必须是随机值 ────────────────────────────────
sk = appmod.app.secret_key or ""
ok("会话密钥已设置", bool(sk))
ok("不是代码里的公开默认值",
   sk != "your-secret-key-change-this-in-production", sk[:8] + "...")
ok("长度足够（>=32 字符）", len(sk) >= 32, len(sk))

# ── 2. 源码 / 密钥 / 数据库不能通过 HTTP 读到 ──────────────
c = appmod.app.test_client()
FORBIDDEN = [
    "/app.py", "/workflow.py", "/database.py", "/Agent.py", "/view.py",
    "/.env", "/.env.example", "/requirements.txt", "/literature.db",
    "/.gitignore", "/.git/config", "/README.md",
    "/providers/__init__.py", "/providers/base.py", "/providers/http.py",
    "/tests/run_all.py", "/tests/test_smoke.py",
    "/static/index.html.bak", "/static/app.py", "/backup.sql",
]
for p in FORBIDDEN:
    st = c.get(p).status_code
    ok(f"源码/密钥不外泄 {p}", st == 404, st)

# 历史事故的具体形态：旧版本 static/ 里真的躺着一份 app.py
ok("静态白名单只放行 3 个文件",
   appmod.STATIC_FILES == {"index.html", "favicon-paper.svg", "logo-paper.svg"},
   appmod.STATIC_FILES)

# ── 3. 该能访问的仍然能访问 ────────────────────────────────
for p in ("/", "/api/health", "/favicon-paper.svg", "/logo-paper.svg"):
    st = c.get(p).status_code
    ok(f"正常页面可访问 {p}", st == 200, st)

# ── 4. 需要登录的接口在未登录时拦得住 ──────────────────────
for p in ("/api/history", "/api/favorites", "/api/conversations"):
    st = c.get(p).status_code
    ok(f"未登录拦得住 {p}", st == 401, st)

# ── 5. 反代支持：ProxyFix 已装配（natapp/ngrok 场景）────────
ok("ProxyFix 已装配", type(appmod.app.wsgi_app).__name__ == "ProxyFix",
   type(appmod.app.wsgi_app).__name__)

# ── 6. 公网口令开关：默认关闭，设了就生效 ──────────────────
ok("默认不开口令（PUBLIC_PASSWORD 为空）", appmod.PUBLIC_PASSWORD == "",
   appmod.PUBLIC_PASSWORD)

# 临时打开口令，确认门禁真的拦
import importlib  # noqa: E402
appmod.PUBLIC_PASSWORD = "test-pass"
try:
    c2 = appmod.app.test_client()
    st_home = c2.get("/").status_code
    ok("开口令后首页被拦", st_home == 401, st_home)
    st_health = c2.get("/api/health").status_code
    ok("健康检查仍放行（监控/存活探测需要）", st_health == 200, st_health)
    st_bad = c2.post("/gate", data={"password": "wrong"}).status_code
    ok("口令错误被拒", st_bad == 401, st_bad)
    st_ok = c2.post("/gate", data={"password": "test-pass"})
    ok("口令正确放行", st_ok.status_code in (301, 302, 303), st_ok.status_code)
    st_after = c2.get("/").status_code
    ok("过关后可以正常访问", st_after == 200, st_after)
finally:
    appmod.PUBLIC_PASSWORD = ""

n_pass = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    if not cond:
        print(f"FAIL  {name}  -> got: {got!r}")
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
