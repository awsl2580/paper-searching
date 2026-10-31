"""历史对话里的论文卡片：papers 必须跟着消息一起落库。

用户报的 bug（很典型）：正常检索出论文 → 点左侧另一段历史对话 → 再切回来，
论文卡片变成**纯文字**，点不开、也跳不回原文。

根因：检索结果（标题/作者/来源/链接/摘要）只在 SSE 流里发给前端渲染，
`conversation_messages` 只存了正文。重新打开历史对话时前端只有正文，
于是只剩模型写的那串标题文字。

这里锁住四件事：
  1. append_message 能存结构化 papers，get_conversation_messages 能原样读回；
  2. papers 列是坏 JSON 时读成 []，不炸整个会话；
  3. /api/conversations/<id> 会把 papers 带给前端（卡片靠它重建）；
  4. 老库（没有 papers 列）启动时自动补列，老消息仍能读。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_history_papers.db")

import app as appmod  # noqa: E402

db.init_db()

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


PAPERS = [
    {
        "id": "abc123",
        "title": "Graph Neural Networks for Remote Sensing Segmentation",
        "authors": ["Zhang San", "Li Si"],
        "year": 2023,
        "venue": "Remote Sensing",
        "source": "openalex",
        "sourceLabel": "OpenAlex",
        "url": "https://doi.org/10.1000/xyz",
        "openAccessPdf": "https://example.org/paper.pdf",
        "abstract": "We propose a GNN-based segmentation model ...",
        "citationCount": 42,
        "isOa": True,
    },
    {
        "id": "def456",
        "title": "A Survey on Remote Sensing Foundation Models",
        "authors": ["Wang Wu"],
        "year": 2024,
        "source": "crossref",
        "sourceLabel": "Crossref",
        "url": "https://example.org/survey",
        "abstract": "Survey ...",
        "citationCount": 7,
    },
]

USER = 7
cid = db.create_conversation(USER, "论文卡片")
ok("建会话成功", bool(cid), cid)
ok("用户消息写入", db.append_message(USER, cid, "user", "图神经网络在遥感影像分割里的进展"))
ok("助手消息连同 papers 一起写入",
   db.append_message(USER, cid, "assistant", "检索到 2 篇，见下方卡片：", papers=PAPERS))

conv, msgs = db.get_conversation_messages(USER, cid)
ok("读回两条消息", len(msgs) == 2, len(msgs))
ok("assistant 的 papers 原样读回",
   [p.get("id") for p in msgs[1]["papers"]] == ["abc123", "def456"],
   msgs[1]["papers"])
ok("原文链接没丢", msgs[1]["papers"][0].get("url") == PAPERS[0]["url"])
ok("摘要没丢", msgs[1]["papers"][0].get("abstract") == PAPERS[0]["abstract"])
ok("user 消息的 papers 是空列表", msgs[0]["papers"] == [], msgs[0]["papers"])
ok("别的用户读不到这段会话", db.get_conversation_messages(8, cid) == (None, []))

# ── 脏数据：papers 列是坏 JSON 时，读成 [] 而不是抛异常 ──
conn = sqlite3.connect(db.DB_PATH)
conn.execute("UPDATE conversation_messages SET papers = ? WHERE role = 'assistant'", ("{not json",))
conn.commit()
conn.close()
_, msgs2 = db.get_conversation_messages(USER, cid)
ok("坏 JSON 当空处理（不抛异常）", msgs2[1]["papers"] == [], msgs2[1]["papers"])

# ── API：/api/conversations/<id> 要把 papers 带给前端 ──
db.append_message(USER, cid, "assistant", "再来一次：", papers=PAPERS[:1])
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = USER
resp = client.get(f"/api/conversations/{cid}")
payload = resp.get_json() or {}
ok("接口返回 200", resp.status_code == 200, resp.status_code)
rows = payload.get("messages") or []
ok("接口把 papers 带出来（前端据此重建卡片）",
   bool(rows) and rows[-1].get("papers")
   and rows[-1]["papers"][0].get("id") == "abc123",
   rows[-1] if rows else None)

# ── 老库升级：没有 papers 列的库，init_db 后自动补列 ──
legacy = temp_db_path("_tmp_history_legacy.db")
conn = sqlite3.connect(legacy)
conn.executescript(
    "CREATE TABLE conversation_messages ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " conversation_id INTEGER NOT NULL,"
    " role TEXT NOT NULL,"
    " content TEXT NOT NULL,"
    " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
)
conn.execute("INSERT INTO conversation_messages (conversation_id, role, content) "
             "VALUES (1, 'assistant', '这是升级前的老消息')")
conn.commit()
conn.close()

db.DB_PATH = legacy
db.init_db()
cols = [row[1] for row in sqlite3.connect(legacy).execute(
    "PRAGMA table_info(conversation_messages)")]
ok("老库自动补上了 papers 列", "papers" in cols, cols)

lcid = db.create_conversation(1, "老会话")
ok("老库补列后还能建会话", lcid == 1, lcid)
_, old_msgs = db.get_conversation_messages(1, lcid)
ok("老消息仍能读出来", len(old_msgs) == 1, len(old_msgs))
ok("老消息没有 papers 时按空列表返回", old_msgs[0]["papers"] == [], old_msgs[0]["papers"])

# ── 落库入口：_persist_turn 要把 papers 一起写进去 ──
db.DB_PATH = temp_db_path("_tmp_history_persist.db")
db.init_db()
pcid = db.create_conversation(USER, "落库入口")
appmod._persist_turn(USER, pcid, "问：遥感分割", "答：见卡片", 2, PAPERS)
_, pmsgs = db.get_conversation_messages(USER, pcid)
ok("_persist_turn 写了两条消息", len(pmsgs) == 2, len(pmsgs))
ok("_persist_turn 把 papers 落到了 assistant 消息上",
   bool(pmsgs[1]["papers"]) and pmsgs[1]["papers"][0]["id"] == "abc123",
   pmsgs[1]["papers"])

# 聊天路由里必须真的把本轮 state["papers"] 传进去（否则前面全白搭）
app_src = open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()
ok("聊天路由把本轮 papers 传给落库函数",
   "_persist_turn(user_id, conversation_id, message, answer" in app_src
   and 'len(state["papers"]), state["papers"]' in app_src,
   None)

bad = [r for r in R if not r[1]]
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{len(R) - len(bad)}/{len(R)} checks passed")
sys.exit(1 if bad else 0)
