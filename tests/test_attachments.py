"""图片 / PDF 附件链路的测试。

分三层：
1. `attachments.py` 的纯逻辑（校验、嗅探真实类型、PDF 渲染、内容块拼装）
2. `/api/chat` 路由：带图请求能不能正确变成多模态消息（用假 agent 抓入参）
3. 错误路径：格式不支持、超大文件、超数量、坏 base64

为什么要覆盖这些：附件这块最容易出的不是"报错"，而是**静默失败** ——
实测 14.7MB 的图送进去不报错但返回空内容，所以体积上限、
真实类型嗅探这些必须有用例守着。
"""
import base64
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")
os.environ.setdefault("S2_MIN_INTERVAL", "0")

from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_attach.db")

import app as appmod  # noqa: E402
import attachments as att  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ── 造素材 ─────────────────────────────────────────────────
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg==")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
PDF = b"%PDF-1.4\n" + b"x" * 200


def make_pdf(pages=2, text="Attachment Test Paper"):
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((60, 80), f"{text} - page {i + 1}", fontsize=16)
    blob = doc.tobytes()
    doc.close()
    return blob


# ── 1. 真实类型嗅探（不信任前端声明的 mime）────────────────
ok("嗅探 PNG", att._sniff_mime(PNG_1PX, "") == "image/png")
ok("嗅探 JPEG", att._sniff_mime(JPEG, "") == "image/jpeg")
ok("嗅探 GIF", att._sniff_mime(GIF, "") == "image/gif")
ok("嗅探 WEBP", att._sniff_mime(WEBP, "") == "image/webp")
ok("嗅探 PDF", att._sniff_mime(PDF, "") == "application/pdf")
ok("声明与内容不符时以内容为准（谎报成 png 的 PDF）",
   att._sniff_mime(PDF, "image/png") == "application/pdf")

# ── 2. 图片附件：拼成 image_url 内容块 ──────────────────────
blocks, notes = att.prepare_attachments(
    [{"name": "a.png", "mime": "image/png", "data": b64(PNG_1PX)}])
ok("一张图 -> 一个内容块", len(blocks) == 1, len(blocks))
ok("内容块类型是 image_url", blocks[0].get("type") == "image_url", blocks[0].get("type"))
ok("走 data URL 形式",
   str(blocks[0]["image_url"]["url"]).startswith("data:image/png;base64,"),
   str(blocks[0]["image_url"]["url"])[:40])
ok("图片附件不产生额外说明", notes == [], notes)

# ── 3. PDF：渲染成多张 PNG + 生成"共几页"的说明 ─────────────
pdf_blob = make_pdf(pages=3)
blocks_p, notes_p = att.prepare_attachments(
    [{"name": "paper.pdf", "mime": "application/pdf", "data": b64(pdf_blob)}])
ok("3 页 PDF -> 3 个内容块", len(blocks_p) == 3, len(blocks_p))
ok("PDF 页面渲染成 PNG",
   all(b["image_url"]["url"].startswith("data:image/png;base64,") for b in blocks_p))
ok("PDF 生成了页数说明", len(notes_p) == 1 and "3 页" in notes_p[0], notes_p)
for b in blocks_p:
    raw = base64.b64decode(b["image_url"]["url"].split(",", 1)[1])
    ok("渲染出的 PNG 是合法图片头", raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8])

# 大 PDF 只取前 N 页，并且要如实说明
big_pdf = make_pdf(pages=att.MAX_PDF_PAGES + 4)
blocks_big, notes_big = att.prepare_attachments(
    [{"name": "big.pdf", "mime": "application/pdf", "data": b64(big_pdf)}])
ok(f"超过 {att.MAX_PDF_PAGES} 页只取前 {att.MAX_PDF_PAGES} 页",
   len(blocks_big) == att.MAX_PDF_PAGES, len(blocks_big))
ok("并说明只读了一部分",
   notes_big and "只读了前" in notes_big[0], notes_big)

# ── 4. 校验：该拒的必须拒 ──────────────────────────────────
def expect_error(files, label, must_contain=None):
    try:
        att.prepare_attachments(files)
        ok(label, False, "没有报错")
    except att.AttachmentError as e:
        good = must_contain is None or must_contain in str(e)
        ok(label, good, str(e)[:90])


expect_error([{"name": "x.txt", "mime": "text/plain", "data": b64(b"hello")}],
             "拒绝不支持的格式", "不支持")
expect_error([{"name": "x.exe", "mime": "application/octet-stream",
               "data": b64(b"MZ\x90\x00" + b"\x00" * 10)}],
             "拒绝可执行文件", "不支持")
expect_error([{"name": "bad.png", "mime": "image/png", "data": "这不是base64!!!"}],
             "拒绝坏 base64", "base64")
expect_error([{"name": "empty.png", "mime": "image/png", "data": ""}],
             "拒绝空附件", "为空")
expect_error([{"name": f"{i}.png", "mime": "image/png", "data": b64(PNG_1PX)}
              for i in range(att.MAX_FILES + 1)],
             "拒绝超过数量上限", "最多")
expect_error([{"name": "huge.png", "mime": "image/png",
               "data": b64(PNG_1PX + b"\x00" * (att.MAX_FILE_BYTES + 1))}],
             "拒绝超过单文件上限", "上限")
expect_error([{"name": "broken.pdf", "mime": "application/pdf",
               "data": b64(b"%PDF-1.4 garbage not a real pdf")}],
             "拒绝损坏的 PDF", None)
expect_error("not-a-list", "拒绝非列表的 files", "格式不正确")

# 合法的 data URL 前缀也要能收（前端两种写法都可能出现）
blocks_du, _ = att.prepare_attachments(
    [{"name": "a.png", "mime": "image/png",
      "data": "data:image/png;base64," + b64(PNG_1PX)}])
ok("带 data URL 前缀的 base64 也能解析", len(blocks_du) == 1, len(blocks_du))

# ── 5. 拼装用户消息 ────────────────────────────────────────
text_only = att.build_user_content("你好", [], [])
ok("纯文本时返回字符串（保持原行为）", isinstance(text_only, str), type(text_only).__name__)

multimodal = att.build_user_content("看这张图", blocks, notes_p)
ok("带图时返回列表（多模态）", isinstance(multimodal, list), type(multimodal).__name__)
ok("列表里有文字块", any(p.get("type") == "text" for p in multimodal))
ok("列表里有图片块", any(p.get("type") == "image_url" for p in multimodal))
ok("文字在最前、图片在后（顺序：说明/文字 -> 图片）",
   multimodal[0]["type"] == "text" and multimodal[-1]["type"] == "image_url",
   [p["type"] for p in multimodal])

only_img = att.build_user_content("", blocks, [])
ok("只有图没有文字时也给一句默认指令",
   any("图片" in str(p.get("text") or "") for p in only_img if p["type"] == "text"),
   only_img)


# ── 6. 路由：带图请求真的变成多模态消息（用假 agent 抓入参）────
class CapturingAgent:
    def __init__(self):
        self.seen = None

    async def astream_events(self, payload, version="v2"):
        self.seen = payload
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": AIMessage(content="我看到了这张图。")}}


def run_chat(body):
    agent = CapturingAgent()
    orig = appmod.create_agent
    appmod.create_agent = lambda **kw: agent
    try:
        c = appmod.app.test_client()
        r = c.post("/api/chat", json=body, buffered=True)
        return r, agent
    finally:
        appmod.create_agent = orig


def sse_text(resp) -> str:
    """把 SSE 里所有 token 事件拼回完整正文。

    注意不能直接在响应文本里搜中文：正文是**分片**下发的
    （"我看到了这张图。" 会拆成 "我看到"/"了这张"/"图。"），
    跨分片的关键词搜不到 —— 第一版就是这么写错的。
    """
    out = []
    for line in resp.get_data(as_text=True).splitlines():
        if not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except ValueError:
            continue
        if ev.get("type") == "token":
            out.append(ev.get("token") or "")
    return "".join(out)


def sse_events(resp):
    evs = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            try:
                evs.append(json.loads(line[6:]))
            except ValueError:
                pass
    return evs


r, agent = run_chat({"message": "帮我找这篇论文", "history": [], "source_pref": "all",
                     "files": [{"name": "a.png", "mime": "image/png", "data": b64(PNG_1PX)}]})
ok("带图请求返回 200", r.status_code == 200, r.status_code)
sent = (agent.seen or {}).get("messages") or []
user_msg = [m for m in sent if m.get("role") == "user"][-1]
ok("发给模型的是多模态 content",
   isinstance(user_msg.get("content"), list), type(user_msg.get("content")).__name__)
ok("多模态里含图片块",
   any(p.get("type") == "image_url" for p in user_msg["content"]))
ok("SSE 里带上了完整回答", sse_text(r) == "我看到了这张图。", repr(sse_text(r)))
ok("SSE 不把 base64 图片回传（否则响应会暴涨）",
   "image_url" not in r.get_data(as_text=True)
   and "base64," not in r.get_data(as_text=True),
   len(r.get_data(as_text=True)))

# 纯文本请求不能受影响
r2, agent2 = run_chat({"message": "乡村振兴", "history": [], "source_pref": "all"})
sent2 = (agent2.seen or {}).get("messages") or []
user2 = [m for m in sent2 if m.get("role") == "user"][-1]
ok("纯文本请求仍是字符串 content", isinstance(user2.get("content"), str),
   type(user2.get("content")).__name__)

# 只发图不写字也要能工作
r3, agent3 = run_chat({"message": "", "history": [], "source_pref": "all",
                       "files": [{"name": "a.png", "mime": "image/png",
                                  "data": b64(PNG_1PX)}]})
ok("只发图不写字也能受理", r3.status_code == 200, r3.status_code)
sent3 = (agent3.seen or {}).get("messages") or []
ok("只发图时仍构造了多模态消息",
   any(isinstance(m.get("content"), list) for m in sent3))

# 既没文字也没图 -> 仍然拒绝
r4, _ = run_chat({"message": "", "history": [], "source_pref": "all"})
ok("既无文字也无图 -> 400", r4.status_code == 400, r4.status_code)

# 非法附件 -> 400 且错误信息可读
r5, _ = run_chat({"message": "看这个", "history": [], "source_pref": "all",
                  "files": [{"name": "x.txt", "mime": "text/plain", "data": b64(b"hi")}]})
ok("不支持的附件 -> 400", r5.status_code == 400, r5.status_code)
# 必须解析 JSON 再找中文：jsonify 默认 ensure_ascii，响应里中文是 \uXXXX 转义，
# 直接搜中文字符串永远搜不到（第一版就栽在这）。
err5 = (r5.get_json() or {}).get("error") or ""
ok("400 里带可读的中文原因", "不支持" in err5, err5[:90])
ok("400 里点名了是哪个文件", "x.txt" in err5, err5[:90])
ok("400 里说明了支持哪些格式", "PNG" in err5 and "PDF" in err5, err5[:120])

# ── 7. 视觉请求要抬高 max_tokens（否则推理吃掉额度、正文为空）──
captured_kwargs = {}


class KwAgent(CapturingAgent):
    pass


def run_chat_kw(body):
    captured_kwargs.clear()
    orig = appmod.create_agent

    def fake_create_agent(**kw):
        captured_kwargs.update(kw)
        return KwAgent()

    appmod.create_agent = fake_create_agent
    try:
        c = appmod.app.test_client()
        return c.post("/api/chat", json=body, buffered=True)
    finally:
        appmod.create_agent = orig


run_chat_kw({"message": "看图", "history": [], "source_pref": "all",
             "files": [{"name": "a.png", "mime": "image/png", "data": b64(PNG_1PX)}]})
mt = getattr(captured_kwargs.get("model"), "max_tokens", None)
ok("带图时 max_tokens 被抬高到足够大",
   mt is not None and mt >= att.VISION_MIN_MAX_TOKENS, mt)

run_chat_kw({"message": "乡村振兴", "history": [], "source_pref": "all"})
mt2 = getattr(captured_kwargs.get("model"), "max_tokens", None)
ok("纯文本时不强行抬高 max_tokens（保持原行为）", mt2 is None, mt2)

# ── 8. 不落盘：附件处理不该写任何文件 ──────────────────────
# 用 AST 找真实的 open() 调用。第一版是在源码文本里搜 "open("，
# 结果命中了我自己文档字符串里写的 "open(...,'w'/'wb')" 这句话 —— 假阳性。
import ast  # noqa: E402

_att_src = open(os.path.join(ROOT, "attachments.py"), encoding="utf-8").read()
_open_calls = [n.lineno for n in ast.walk(ast.parse(_att_src))
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"]
ok("attachments.py 里没有任何 open() 调用（图片不落盘）",
   _open_calls == [], _open_calls)

# 不只看代码，还实测一遍：处理一张图 + 一个 PDF，前后比对目录快照
def snap():
    out = {}
    for dp, dirs, fs in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv")]
        for f in fs:
            out[os.path.join(dp, f)] = os.path.getmtime(os.path.join(dp, f))
    return out

before = snap()
att.prepare_attachments([{"name": "a.png", "mime": "image/png", "data": b64(PNG_1PX)},
                         {"name": "p.pdf", "mime": "application/pdf",
                          "data": b64(make_pdf(1))}])
after = snap()
ok("处理附件后没有新增文件", set(after) - set(before) == set(), set(after) - set(before))
ok("处理附件后没有文件被改写",
   all(after[k] == before[k] for k in before if k in after), "有文件 mtime 变化")

tables = [r[0] for r in db.get_db().execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
ok("数据库里没有附件/图片相关的表",
   not any("attach" in t.lower() or "image" in t.lower() for t in tables), tables)

n_pass = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
