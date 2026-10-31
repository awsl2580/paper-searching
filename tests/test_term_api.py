# -*- coding: utf-8 -*-
"""名词解释接口 /api/term：参数校验、结构归一化、模型失败时的降级。

这个功能不需要登录（读论文时随手查词），所以这里专门验：不带 session 也能调；
但输入有硬上限，模型返回脏数据不会把 500 抛给前端。
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

db.DB_PATH = temp_db_path("_tmp_term.db")

import app as appmod  # noqa: E402

db.init_db()

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


client = appmod.app.test_client()

# ── 1. 参数校验 ──────────────────────────────────────────────
r = client.post("/api/term", json={})
ok("空名词 -> 400", r.status_code == 400, r.status_code)

r = client.post("/api/term", json={"term": "   "})
ok("只有空格也算空 -> 400", r.status_code == 400, r.status_code)

r = client.post("/api/term", json={"term": "术" * 61})
ok("名词超过 60 字 -> 400", r.status_code == 400, r.status_code)

# ── 2. 正常返回（把模型调用换成桩，不联网） ───────────────────
orig = appmod.llm_layer.call_json
captured = {}


def fake_call_json(system, user, **kw):
    captured["system"] = system
    captured["user"] = user
    captured["kw"] = kw
    return {
        "term": "对比学习（Contrastive Learning）",
        "en": "Contrastive Learning",
        "definition": "把同类样本拉近、异类样本推远的一种表示学习方法。",
        "plain": "像把同班同学排在一起、别班同学排远一点。",
        "context": "在 few-shot 场景里，它常用来自监督预训练编码器。",
        "related": ["自监督学习", "孪生网络", "InfoNCE"],
        "misuse": "常被和“迁移学习”混为一谈。",
        "read_more": "可以先看 SimCLR 与 MoCo 两篇。",
    }


appmod.llm_layer.call_json = fake_call_json
try:
    r = client.post("/api/term", json={"term": "对比学习",
                                       "context": "我们在 few-shot 设定下用对比学习预训练编码器"})
    data = r.get_json() or {}
finally:
    appmod.llm_layer.call_json = orig

ok("正常请求 -> 200", r.status_code == 200, r.status_code)
ok("返回 success", data.get("success") is True, data.get("success"))
ok("term 原样带回", data.get("term") == "对比学习（Contrastive Learning）", data.get("term"))
ok("定义/通俗/语境都在", all(data.get(k) for k in ("definition", "plain", "context")))
ok("相关概念是列表且非空", isinstance(data.get("related"), list) and len(data["related"]) == 3,
   data.get("related"))
ok("常见误解与延伸阅读都在", bool(data.get("misuse")) and bool(data.get("read_more")))
ok("原文语境被带进提示词", "few-shot" in captured.get("user", ""), captured.get("user", "")[:60])
ok("用的是轻量模型槽位（fast）", captured.get("kw", {}).get("kind") == "fast",
   captured.get("kw"))

# ── 3. 模型返回脏数据 / 失败 ─────────────────────────────────
appmod.llm_layer.call_json = lambda *a, **kw: None
try:
    r = client.post("/api/term", json={"term": "p 值"})
finally:
    appmod.llm_layer.call_json = orig
ok("模型没给出 JSON -> 502（不是 500 也不是 200）", r.status_code == 502, r.status_code)

# related 给成字符串时也能归一化成列表
appmod.llm_layer.call_json = lambda *a, **kw: {
    "term": "p 值", "definition": "在原假设成立时，观测到当前或更极端结果的概率。",
    "plain": "越小越可疑。", "context": "常与显著性水平 0.05 一起用。",
    "related": "显著性水平、置信区间、多重比较", "misuse": "暂无", "read_more": "",
}
try:
    r = client.post("/api/term", json={"term": "p 值"})
    data2 = r.get_json() or {}
finally:
    appmod.llm_layer.call_json = orig
ok("related 是字符串时拆成列表",
   data2.get("related") == ["显著性水平", "置信区间", "多重比较"], data2.get("related"))

bad = [x for x in R if not x[1]]
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{len(R) - len(bad)}/{len(R)} checks passed")
sys.exit(1 if bad else 0)
