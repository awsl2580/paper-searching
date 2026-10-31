# -*- coding: utf-8 -*-
"""润色接口 /api/polish：**必须真的改写**，不能原样返回。

老版本的毛病就是"润色前后一模一样"：提示词只说"保持原意不变"，轻量模型直接复述。
现在三重保险：硬约束提示词 + 相似度检测 + 太像就重试一次。这里把这三条钉住。
"""
import os
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

db.DB_PATH = temp_db_path("_tmp_polish.db")

import app as appmod  # noqa: E402

db.init_db()

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


ORIGINAL = (
    "本文提出了一种新的遥感影像分割方法。实验表明该方法效果很好，"
    "能够有效提升分割精度，并且在多个数据集上都取得了不错的表现，"
    "说明该方法具有一定的通用性和实用价值。"
)


class FakeResp:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, outputs, calls):
        self.outputs = list(outputs)
        self.calls = calls

    def invoke(self, messages):
        self.calls.append(messages)
        return FakeResp(self.outputs.pop(0) if self.outputs else "")


def run_with(outputs, style="academic", abstract=ORIGINAL, logged_in=True):
    """用一组假输出跑一次请求，返回 (status, json, 调用次数)"""
    calls = []
    model = FakeModel(outputs, calls)
    orig = appmod.llm_layer.chat_model
    appmod.llm_layer.chat_model = lambda *a, **k: model
    try:
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            if logged_in:
                sess["user_id"] = 7
        r = client.post("/api/polish", json={"abstract": abstract, "style": style})
        return r.status_code, (r.get_json() or {}), len(calls)
    finally:
        appmod.llm_layer.chat_model = orig


# ── 1. 门槛与参数校验 ────────────────────────────────────────
code, _, _ = run_with(["x"], logged_in=False)
ok("未登录 -> 401", code == 401, code)

code, data, _ = run_with(["x"], abstract="")
ok("空摘要 -> 400", code == 400, code)

code, data, _ = run_with(["x"], abstract="太短了")
ok("少于 20 字 -> 400", code == 400, code)

code, data, _ = run_with(["x"], abstract="长" * 5001)
ok("超过 5000 字 -> 400", code == 400, code)

# ── 2. 正常改写：正文 + 改动说明 ─────────────────────────────
GOOD = (
    "本文给出一套面向遥感影像的分割方法：先用多尺度特征对齐把道路、建筑与植被分开，"
    "再让模型在边界处加一层约束。在三个数据集上，边界处的误分明显减少，"
    "整体分割精度随之提高，说明这套做法在跨数据集时也能站得住。\n"
    "【改动说明】\n"
    "- 把\u201c效果很好\u201d拆成具体现象\n"
    "- 补上方法的两步动作\n"
    "- 结论落到跨数据集\n"
)
code, data, calls = run_with([GOOD])
ok("正常请求 -> 200", code == 200, code)
ok("返回 success", data.get("success") is True, data.get("success"))
ok("正文已去掉【改动说明】", "【改动说明】" not in data.get("polished", ""), data.get("polished", "")[:40])
ok("正文是新写的（不是原文）", data.get("polished") != ORIGINAL and len(data.get("polished", "")) > 20)
ok("带 3 条改动说明", len(data.get("notes") or []) == 3, data.get("notes"))
ok("标记为已改写", data.get("changed") is True, data.get("changed"))
ok("相似度落在合理区间", 0 <= float(data.get("similarity", 1)) <= 1, data.get("similarity"))
ok("只调了一次模型", calls == 1, calls)

# ── 3. 原样返回 -> 自动重试一次 ─────────────────────────────
SAME = ORIGINAL + "\n【改动说明】\n- 没改\n"
code, data, calls = run_with([SAME, GOOD])
ok("第一次原样返回时会重试", calls == 2, calls)
ok("重试后拿到真正改写的结果", data.get("polished") != ORIGINAL and data.get("changed") is True,
   data.get("changed"))

# 两次都原样返回：仍然返回结果，但标记 changed=False（前端据此提示）
code, data, calls = run_with([SAME, SAME])
ok("两次都没改 -> changed=False", data.get("changed") is False, data.get("changed"))
ok("两次都试过了", calls == 2, calls)

# ── 3.5 编造数字：宁可少改，也不许编 ─────────────────────────
BAD_NUM = ("本文提出了一种新的遥感影像分割方法，精度提升了 15%，达到 92.3%。\n"
           "【改动说明】\n- 补了数字\n")
code, data, calls = run_with([BAD_NUM, BAD_NUM])
ok("两次都编造数字 -> 502 拒绝返回", code == 502 and "编造" in (data.get("error") or ""),
   (code, data.get("error")))

code, data, calls = run_with([BAD_NUM, GOOD])
ok("第一次编数字、重试后干净 -> 用干净的那版",
   code == 200 and "15%" not in (data.get("polished") or "") and data.get("changed") is True,
   data.get("polished", "")[:30])

# ── 4. 风格与异常 ───────────────────────────────────────────
code, data, _ = run_with([GOOD], style="vivid")
ok("支持\u201c生动有画面感\u201d风格", code == 200 and data.get("style") == "vivid", data.get("style"))

code, data, _ = run_with([GOOD], style="不存在的风格")
ok("陌生风格回落 academic", data.get("style") == "academic", data.get("style"))

code, data, _ = run_with(["", ""])
ok("模型返回空 -> 502", code == 502, code)

# ── 5. 提示词本身要带上硬约束（防止以后被改回"保持原意"）──────
app_src = open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()
ok("提示词要求逐句重写", "逐句重写" in app_src and "不允许整句照抄" in app_src)
ok("提示词要求具体化/逻辑/画面感", all(k in app_src for k in ("更具体", "有逻辑", "有画面感")))
ok("提示词禁止编造数据", "不许编" in app_src)
ok("服务端有相似度与句子级检测", "_text_similarity" in app_src and "_sentence_overlap" in app_src)
ok("服务端拦截编造的数字", "_new_numbers" in app_src and "编造了原文没有的数据" in app_src)
ok("相似度/句子复用会触发重试", "_polish_retry_note" in app_src and "上一版没有通过检查" in app_src)

bad = [x for x in R if not x[1]]
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{len(R) - len(bad)}/{len(R)} checks passed")
sys.exit(1 if bad else 0)
