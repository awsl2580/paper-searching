"""会话删除的幂等语义（「❌ 会话不存在」事故的服务端一半）。

用户报的现象：**思考进行中删除会话**，聊天里堆出一串「助手：❌ 会话不存在」。

两层根因，这个文件锁服务端这一层：

* 服务端把"删除一个已经不存在的会话"当 404。可前端拿到 404 之后会把它
  当成聊天错误 `addMessage('assistant', '❌ 会话不存在')` —— 于是"这段记录
  没了"变成了"聊天里多一条报错"。
* 对调用方来说，删一个已经没了的会话，结果和删成功是一样的（它现在确实
  不存在）。所以这里改成**幂等**：仍然返回 success，并带 `already=true`。

前端那一半（404 只从侧栏移除、删除正在回答的会话要连这一轮一起停掉）见
`tests/test_chat_stream_state.js`。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")
os.environ.setdefault("MCP_TRANSPORT", "inproc")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_conv_delete.db")
import app as appmod  # noqa: E402

db.init_db()

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


OWNER = 101          # 会话的主人
OTHER = 202          # 另一个用户（用来验跨用户隔离）

conv = db.create_conversation(OWNER, "思考进行中删掉我")
db.append_message(OWNER, conv, "user", "问题")
db.append_message(OWNER, conv, "assistant", "回答")

client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = OWNER

# ── 1. 正常删除 ─────────────────────────────────────────────
resp = client.delete(f"/api/conversations/{conv}")
body = resp.get_json() or {}
ok("删自己的会话 -> 200", resp.status_code == 200, resp.status_code)
ok("返回 success", body.get("success") is True, body)
ok("第一次删除 already=false", body.get("already") is False, body)
ok("删完之后读不到了", client.get(f"/api/conversations/{conv}").status_code == 404)

# ── 2. 幂等：再删一次（连点两下 / 另一个标签页已经删了）──────────
resp2 = client.delete(f"/api/conversations/{conv}")
body2 = resp2.get_json() or {}
ok("重复删除不再是 404", resp2.status_code == 200, resp2.status_code)
ok("重复删除仍然 success（删除是幂等的）", body2.get("success") is True, body2)
ok("重复删除标出 already=true", body2.get("already") is True, body2)

# ── 3. 跨用户隔离：删别人的会话删不掉、但也不报错 ────────────────
foreign = db.create_conversation(OTHER, "别人的会话")
db.append_message(OTHER, foreign, "user", "别人的问题")
resp3 = client.delete(f"/api/conversations/{foreign}")
body3 = resp3.get_json() or {}
ok("删别人的会话不会删除它（隔离）",
   db.get_conversation_messages(OTHER, foreign)[0] is not None)
ok("删别人的会话不报错（幂等语义一致）", resp3.status_code == 200 and body3.get("success"),
   (resp3.status_code, body3))
ok("没有把别人的会话标成已删除", body3.get("already") is True, body3)

# ── 4. 未登录仍然要 401（别为了幂等把门禁弄丢）───────────────────
anon = appmod.app.test_client()
ok("未登录删除 -> 401", anon.delete(f"/api/conversations/{foreign}").status_code == 401)

# ── 5. 前端接线（这半边在 JS 用例里也有更细的断言，这里只做快速对照）──
_html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
ok("前端有 forgetConversation（把记录从列表抹掉，而不是塞进聊天）",
   "function forgetConversation(conversationId)" in _html)
ok("打开已删会话：404 时只把它从侧栏移除并返回，不写进聊天",
   re.search(r"if \(resp\.status === 404\) \{[\s\S]{0,400}"
             r"forgetConversation\(conversationId\);[\s\S]{0,60}return;", _html) is not None)
ok("删除失败的 404 也当成功（前端再兜一层）",
   re.search(r"if \(!data\.success && resp\.status !== 404\)", _html) is not None)

n_pass = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
