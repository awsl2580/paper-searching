"""运行日志 + 管理员日志窗口（core/runlog.py + /admin/?tab=logs）。

这个文件锁四件事：

1. **写入与查询**：级别/模块/关键词过滤、分页、统计、清空、保留上限；
2. **请求自动埋点**：任何非静态请求都会落一条 `request` 日志；
3. **不记录用户内容**：聊天日志只记"多长、几篇、多久"，不记问题原文
   —— 日志是给管理员排障的，不该变成"偷看别人提问"的后门；
4. **管理员接口鉴权**：没有 admin_ok 会话时，查/清日志都是 403。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")
os.environ.setdefault("MCP_TRANSPORT", "inproc")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_runlog.db")
import app as appmod  # noqa: E402
import runlog  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

db.init_db()
runlog.clear()

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ── 1. 写入 / 查询 ──────────────────────────────────────────
runlog.info("auth", "测试登录", user_id=7, ip="127.0.0.1", detail={"scene": "unit"})
runlog.warn("chat", "测试警告")
runlog.error("workflow", "测试错误")

data = runlog.query()
ok("三条日志都写进去了", data["total"] == 3, data["total"])
ok("默认新的在前", data["rows"][0]["level"] == "ERROR",
   [r["level"] for r in data["rows"]])
ok("字段齐全（ts/level/module/message/user_id/ip/detail）",
   all(k in data["rows"][0] for k in ("ts", "level", "module", "message",
                                      "user_id", "ip", "detail")),
   list(data["rows"][0].keys()))
ok("user_id / ip / detail 落库", data["rows"][-1]["user_id"] == 7
   and data["rows"][-1]["ip"] == "127.0.0.1"
   and '"scene"' in (data["rows"][-1]["detail"] or ""), data["rows"][-1])

ok("按级别过滤（大小写都认）", runlog.query(level="warn")["total"] == 1,
   runlog.query(level="warn")["total"])
ok("按模块过滤", runlog.query(module="auth")["total"] == 1)
ok("关键词能搜到 detail", runlog.query(keyword="scene")["total"] == 1)
ok("关键词能搜到消息", runlog.query(keyword="测试警告")["total"] == 1)
ok("非法级别被忽略（不会查出空集以外的怪结果）",
   runlog.query(level="NOT-A-LEVEL")["total"] == 3)

p1 = runlog.query(size=2, page=1)
p2 = runlog.query(size=2, page=2)
ok("分页：第 1 页 2 条", len(p1["rows"]) == 2 and p1["pages"] == 2, p1["pages"])
ok("分页：第 2 页 1 条", len(p2["rows"]) == 1, len(p2["rows"]))
ok("页码越界会夹到最后一页", runlog.query(size=2, page=99)["page"] == 2)

st = runlog.stats()
ok("统计：总数", st["total"] == 3, st)
ok("统计：按级别", st["by_level"].get("ERROR") == 1, st["by_level"])
ok("统计：近 24 小时", st["last24h"] == 3, st["last24h"])

# ── 2. 保留上限 / 清空 ─────────────────────────────────────
for i in range(30):
    runlog.info("system", "批量 %d" % i)
removed = runlog.prune(keep=10)
ok("prune 删掉多余的行", removed > 0 and runlog.query()["total"] == 10,
   (removed, runlog.query()["total"]))
ok("prune 保留的是最新的", any("批量 29" in r["message"]
                            for r in runlog.query()["rows"]))

ok("清空返回删除条数", runlog.clear() == 10)
ok("清空后为空", runlog.query()["total"] == 0)

# ── 3. 请求自动埋点 ────────────────────────────────────────
client = appmod.app.test_client()
client.get("/api/models")
req_rows = runlog.query(module="request")["rows"]
ok("普通请求自动记一条 request 日志",
   any("/api/models" in r["message"] for r in req_rows),
   [r["message"] for r in req_rows][:3])
ok("request 日志带状态码与耗时",
   '"status"' in (req_rows[0]["detail"] or "") and '"ms"' in (req_rows[0]["detail"] or ""),
   req_rows[0]["detail"] if req_rows else None)
ok("静态资源不记（避免刷屏）",
   not any("/favicon" in (r["message"] or "") for r in req_rows))

# ── 4. 聊天日志：记规模，不记内容 ─────────────────────────
MARK = "机密问题MARK9F3"


class TinyAgent:
    async def astream_events(self, payload, version="v2"):
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": AIMessage(content="好")}}


_real_agent = appmod.create_agent
appmod.create_agent = lambda **kw: TinyAgent()
try:
    resp = client.post("/api/chat", json={"message": MARK, "history": []}, buffered=True)
finally:
    appmod.create_agent = _real_agent

chat_rows = runlog.query(module="chat")["rows"]
ok("聊天接口照常返回", resp.status_code == 200, resp.status_code)
ok("聊天记了开始与收尾两条",
   any("收到问答" in (r["message"] or "") for r in chat_rows)
   and any("问答完成" in (r["message"] or "") for r in chat_rows),
   [r["message"] for r in chat_rows])
ok("日志里**不含**问题原文（只记长度）",
   all(MARK not in ((r["message"] or "") + (r["detail"] or "")) for r in chat_rows),
   [r["message"] for r in chat_rows if MARK in (r["message"] or "")])
ok("收尾日志带规模信息（字数/篇数）",
   any("字回答" in (r["message"] or "") and "篇文献" in (r["message"] or "")
       for r in chat_rows), [r["message"] for r in chat_rows])

# ── 5. 管理员接口鉴权 ────────────────────────────────────
anon = appmod.app.test_client()
ok("未登录查日志 -> 403", anon.get("/api/admin/logs").status_code == 403)
ok("未登录清日志 -> 403", anon.delete("/api/admin/logs").status_code == 403)
ok("未登录看日志页 -> 登录页（不是控制台）",
   "管理员登录" in anon.get("/admin/?tab=logs").get_data(as_text=True))

with anon.session_transaction() as sess:
    sess["admin_ok"] = True

resp = anon.get("/api/admin/logs?size=5")
payload = resp.get_json() or {}
ok("管理员能查日志", resp.status_code == 200 and payload.get("success"), resp.status_code)
ok("接口返回分页字段", all(k in payload for k in ("rows", "total", "page", "pages")),
   list(payload.keys()))
ok("接口带统计", isinstance(payload.get("stats"), dict) and "total" in payload["stats"])

resp_warn = anon.get("/api/admin/logs?level=WARN&size=5").get_json() or {}
ok("接口支持级别过滤",
   all(r["level"] == "WARN" for r in resp_warn.get("rows") or []),
   [r["level"] for r in resp_warn.get("rows") or []])

page_logs = anon.get("/admin/?tab=logs").get_data(as_text=True)
ok("管理员页有「运行日志」标签", "运行日志" in page_logs)
ok("日志页有筛选控件（级别/模块/关键词）",
   'name="level"' in page_logs and 'name="module"' in page_logs and 'name="q"' in page_logs,
   None)
ok("日志页有统计卡（总数/近24小时/ERROR/WARN）",
   "日志总数" in page_logs and "近 24 小时" in page_logs and "ERROR" in page_logs)
ok("日志页有自动刷新与清空入口",
   "autoRefresh" in page_logs and "clearLogs" in page_logs)
ok("日志页渲染出了具体日志行（含模块名）",
   "request" in page_logs and "chat" in page_logs)

page_users = anon.get("/admin/").get_data(as_text=True)
ok("用户总览标签仍然正常", "当前用户" in page_users and "运行日志" in page_users)

# 清空接口
before = runlog.query()["total"]
cleared = anon.delete("/api/admin/logs").get_json() or {}
after_rows = runlog.query()["rows"]
ok("管理员能清空日志", cleared.get("success") is True and cleared.get("deleted") == before,
   (cleared, before))
# 清空之后还会留下两条"痕迹"：清空动作本身 + 这次 DELETE 请求的 request 日志
ok("清空后只剩留痕（清空动作 + 这条请求）", len(after_rows) <= 2, len(after_rows))
ok("留痕里写着清空了多少条",
   any("清空了运行日志" in (r["message"] or "") for r in after_rows),
   [r["message"] for r in after_rows])

n_pass = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
