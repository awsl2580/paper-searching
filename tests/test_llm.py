"""DeepSeek 大模型接入层（llm.py）—— 离线单测。

这里锁死四件事：

1. 默认配置**就是** DeepSeek（base_url / 两个模型 / 四种用途），
   没有被改回别家，也没有多余的模型；
2. 界面可选模型**只有** deepseek-V4.1-flash 与 deepseek-v4-pro；
3. DeepSeek 不接收 thinking_mode：请求体里绝不能出现这个字段，
   「深度思考」只是把模型切到 deepseek-v4-pro；
4. 宽松 JSON 解析、限流、pro→flash 的自动退回、check_models。

全程不打网络：`_post_chat` 被替换成假实现。
"""
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ["DEEPSEEK_API_KEY"] = ""            # 用例自己控制 key，不读 .env 里的真 key
os.environ.setdefault("DEEPSEEK_RPM", "6000")   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）

import llm  # noqa: E402

R = []
FLASH = "deepseek-V4.1-flash"
PRO = "deepseek-v4-pro"


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ── 1. 默认就是 DeepSeek，且只用两个模型 ──────────────────────────
ok("默认 base_url 指向 DeepSeek", llm.BASE_URL == "https://api.deepseek.com/v1",
   llm.BASE_URL)
ok("默认对话模型是 flash", llm.model_for("chat") == FLASH, llm.model_for("chat"))
ok("轻量批处理用 flash", llm.model_for("fast") == FLASH, llm.model_for("fast"))
ok("读图用 flash", llm.model_for("vision") == FLASH, llm.model_for("vision"))
ok("深度推理用 pro", llm.model_for("reason") == PRO, llm.model_for("reason"))
ok("四种用途都配了", set(llm.PURPOSES) == {"chat", "fast", "vision", "reason"},
   list(llm.PURPOSES))

# base_url 的几种写法都要收敛成同一个地址（用户很容易只写域名）
for raw, want in [
    ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1"),
]:
    ok(f"base_url 归一化 {raw[-28:]}", llm._normalize_base(raw) == want,
       llm._normalize_base(raw))

# ── 2. 模型清单：只能选两个 ──────────────────────────────────────
ids = [m["id"] for m in llm.MODEL_CATALOG]
ok("可选模型正好两个", ids == [FLASH, PRO], ids)
ok("SELECTABLE_MODELS 与清单一致", set(llm.SELECTABLE_MODELS) == {FLASH, PRO},
   llm.SELECTABLE_MODELS)
ok("默认模型是 flash", llm._resolve_model(None, "chat") == FLASH)
ok("陌生模型名回落到默认", llm._resolve_model("gpt-4", "chat") == FLASH)
# 连环境变量也不能绕过白名单（需求：界面/配置都只能选这两个）
os.environ["DEEPSEEK_MODEL"] = "gpt-4"
ok("环境变量写了清单外的名字也会回落", llm.model_for("chat") == FLASH,
   llm.model_for("chat"))
os.environ.pop("DEEPSEEK_MODEL", None)
ok("清单外的模型名不是白名单",
   not llm.is_selectable("some-old-model") and not llm.is_selectable("deepseek-v4"))
ok("is_selectable 接受 pro", llm.is_selectable(PRO))
ok("is_selectable 拒绝空值",
   not llm.is_selectable("") and not llm.is_selectable(None))

# ── 3. 深度思考：只切模型，不传 thinking_mode ─────────────────────
ok("flash 支持深度思考开关", llm._supports_thinking(FLASH))
ok("pro 支持深度思考开关", llm._supports_thinking(PRO))
ok("auto：chat 用 flash 不推理", llm.thinking_mode_for("chat", FLASH) is False)
ok("auto：reason 用 pro 算推理", llm.thinking_mode_for("reason", PRO) is True)
ok("auto：手动选 pro 也算推理", llm.thinking_mode_for("chat", PRO) is True)

llm.THINKING_SETTING = "reason"
ok("THINKING_MODE=reason：只有推理用途算开",
   llm.thinking_mode_for("reason", PRO) is True
   and llm.thinking_mode_for("chat", FLASH) is False)
llm.THINKING_SETTING = "1"
ok("THINKING_MODE=1：chat 也算开", llm.thinking_mode_for("chat", FLASH) is True)
llm.THINKING_SETTING = "0"
ok("THINKING_MODE=0：reason 也关", llm.thinking_mode_for("reason", PRO) is False)
llm.THINKING_SETTING = "auto"

# ── 4. 请求体：不带 thinking_mode / response_format ────────────────
_orig_post = llm._post_chat
_orig_key = llm.API_KEY
llm.API_KEY = "sk-test"
seen_payloads = []


def spy(payload, timeout=None):
    seen_payloads.append(dict(payload))
    return True, {"choices": [{"message": {"content": "ok"}}]}


llm._post_chat = spy
txt, _reason = llm.chat([{"role": "user", "content": "hi"}],
                        kind="chat", max_tokens=128)
ok("正常返回正文", txt == "ok", txt)
first = seen_payloads[0]
ok("请求体不带 thinking_mode（DeepSeek 没这个参数）", "thinking_mode" not in first,
   list(first))
ok("请求体不带 response_format", "response_format" not in first, list(first))
ok("请求体模型名来自配置", first.get("model") == FLASH, first.get("model"))
ok("请求体带 stream=False", first.get("stream") is False, first.get("stream"))

# 深度思考 = 换 pro；force_no_thinking = 换回 flash
llm.chat([{"role": "user", "content": "hi"}], kind="chat", model=PRO,
         max_tokens=128)
ok("显式选 pro 时请求体用 pro", seen_payloads[-1].get("model") == PRO,
   seen_payloads[-1].get("model"))
llm._chat_impl([{"role": "user", "content": "hi"}], kind="reason", model=PRO,
               max_tokens=128, force_no_thinking=True)
ok("强制不推理时改用 flash", seen_payloads[-1].get("model") == FLASH,
   seen_payloads[-1].get("model"))
ok("请求体始终不带 thinking_mode",
   all("thinking_mode" not in p for p in seen_payloads))

# ── 5. pro 正文为空 -> 自动换 flash 重试（实测过的坑）─────────────
_seen_models = []


def fake_empty_when_pro(payload, timeout=None):
    _seen_models.append(payload.get("model"))
    if payload.get("model") == PRO:
        return True, {"choices": [{"message": {
            "content": "", "reasoning_content": "思考" * 300}}]}
    return True, {"choices": [{"message": {"content": '{"ok": true}'}}]}


llm._post_chat = fake_empty_when_pro
before_retries = llm.thinking_retries()["count"]
llm._MODEL_FALLBACKS.clear()
payload_text, _reason = llm.chat([{"role": "user", "content": "hi"}],
                                 kind="reason", model=PRO, max_tokens=300)
ok("pro 正文为空时会换 flash 重试并拿到内容", '{"ok": true}' in payload_text,
   payload_text)
ok("重试时确实换了模型", _seen_models == [PRO, FLASH], _seen_models)
ok("这次重试被计数（可在 /api/models 观察）",
   llm.thinking_retries()["count"] == before_retries + 1, llm.thinking_retries())
ok("这次降级被记进 model_fallbacks",
   any(f["from"] == PRO and f["to"] == FLASH for f in llm.model_fallbacks()),
   llm.model_fallbacks())

# ── 6. 宽松 JSON 解析（模型会包 markdown / 推理里夹示例）──────────
cases = [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 2}\n```', {"a": 2}),
    ('好的，结果如下：\n{"a": 3}\n以上。', {"a": 3}),
    ('[{"a": 4}]', [{"a": 4}]),
    ('{"a": "含 } 的字符串"}', {"a": "含 } 的字符串"}),
    ('{"a": "he said \\"x\\""}', {"a": 'he said "x"'}),
]
for raw, want in cases:
    ok(f"宽松解析 {raw[:14]}", llm.loads_json(raw) == want, llm.loads_json(raw))
ok("取最后一个 JSON（推理里夹示例时）",
   llm.loads_json('推理示例 {"a": 1}，最终 {"a": 2}') == {"a": 2},
   llm.loads_json('推理示例 {"a": 1}，最终 {"a": 2}'))
ok("嵌套与字符串里的括号不破坏切分",
   llm.loads_json('{"a": "含 } 的字符串", "b": {"c": [1, 2]}}')
   == {"a": "含 } 的字符串", "b": {"c": [1, 2]}})
ok("空串解析为 None", llm.loads_json("") is None)
ok("纯散文解析为 None", llm.loads_json("这是一段没有任何 JSON 的话") is None)

# ── 7. backend / require_llm ──────────────────────────────────────
llm.API_KEY = ""
ok("没有 key 时 backend 为 None", llm.backend() is None, llm.backend())
ok("没有 key 时 available 为 False", llm.available() is False)
try:
    llm.require_llm()
    ok("未配置时 require_llm 抛错", False)
except RuntimeError as e:
    msg = str(e)
    ok("未配置时 require_llm 抛错", True)
    ok("报错里有 API Key 申请地址", "platform.deepseek.com/api_keys" in msg, msg[:80])
    ok("报错里有环境变量名", "DEEPSEEK_API_KEY" in msg, msg[:80])

llm.API_KEY = "sk-dummy"
ok("配了 key 时 backend 是 deepseek", llm.backend() == "deepseek", llm.backend())
ok("配了 key 时 available 为 True", llm.available() is True)

# ── 8. model_info 的形状（前端徽章/接口都吃它）──────────────────────
info = llm.model_info()
ok("model_info 有 backend/roles", "backend" in info and len(info["roles"]) == 4,
   list(info))
ok("backend 如实报告 deepseek", info["backend"] == "deepseek", info["backend"])
ok("backend_label 是 DeepSeek", "DeepSeek" in info["backend_label"],
   info["backend_label"])
ok("每个角色都带 model/thinking/label",
   all(set(r) >= {"kind", "label", "model", "thinking"} for r in info["roles"]))
ok("model_info 报告限流值", info.get("rpm_limit", 0) > 0, info.get("rpm_limit"))
ok("model_info 带上下文提示", bool(info.get("max_context_note")),
   info.get("max_context_note"))
ok("model_info 带 DeepSeek 文档地址", "deepseek" in (info.get("docs") or ""),
   info.get("docs"))
ok("model_info 里没有旧厂商字段", "deepseek_available" not in info, list(info))

# ── 9. call_json：用假 _post_chat 验重试与解析 ─────────────────────
calls = {"n": 0}


def fake_post(payload, timeout=None):
    calls["n"] += 1
    if calls["n"] == 1:
        return True, {"choices": [{"message": {"content": "我无法给出 JSON"}}]}
    return True, {"choices": [{"message": {"content": '{"ok": true, "value": 42}'}}]}


llm._post_chat = fake_post
got = llm.call_json("sys", "user", kind="fast", retries=2)
ok("call_json 首次失败会重试并成功", got == {"ok": True, "value": 42}, got)
ok("重试确实发生了（2 次调用）", calls["n"] == 2, calls["n"])

calls["n"] = 0
llm._post_chat = lambda payload, timeout=None: (True, {
    "choices": [{"message": {"content": '```json\n{"x": 1}\n```'}}]})
ok("call_json 能剥掉 markdown 代码块", llm.call_json("s", "u") == {"x": 1})

# 思考内容与正文要分开取
llm._post_chat = lambda payload, timeout=None: (True, {
    "choices": [{"message": {"content": "正文", "reasoning_content": "思考"}}]})
text, reasoning = llm.chat([{"role": "user", "content": "hi"}])
ok("正文与思考内容分开返回", text == "正文" and reasoning == "思考",
   (text, reasoning))

# 失败时不抛异常（工作流要靠 None 走降级路径）
llm._post_chat = lambda payload, timeout=None: (False, "接口返回 HTTP 500")
ok("调用失败返回 None 而不是抛异常", llm.call_json("s", "u", retries=1) is None)
llm._note_error("接口返回 HTTP 500")
ok("失败原因被记进 last_error", "500" in (llm.last_error().get("text") or ""),
   llm.last_error())
ok("last_error 带时间戳", llm.last_error().get("at", 0) > 0)

# ── 10. 选中模型调不通时：自动退回该用途默认模型 ───────────────────
llm._MODEL_FALLBACKS.clear()
_calls = []


def fake_by_model(payload, timeout=None):
    _calls.append(payload.get("model"))
    if payload.get("model") == PRO:
        return False, "接口返回 HTTP 404：model not found"
    return True, {"choices": [{"message": {"content": "好"}}]}


llm._post_chat = fake_by_model
txt, _reason = llm.chat([{"role": "user", "content": "hi"}], kind="chat",
                        model=PRO)
ok("未授权模型失败后退回默认模型重试", txt == "好", txt)
ok("确实先试了用户选的、再试默认",
   _calls[:2] == [PRO, llm.model_for("chat")], _calls)
ok("这次降级被记进 model_fallbacks（界面上能提示）",
   any(f["from"] == PRO for f in llm.model_fallbacks()), llm.model_fallbacks())

# 对话主模型自己失败时不该再打一次（换了还是同一个模型，纯浪费）
_calls.clear()


def always_fail(payload, timeout=None):
    _calls.append(payload.get("model"))
    return False, "接口返回 HTTP 500"


llm._post_chat = always_fail
text2, _ = llm.chat([{"role": "user", "content": "hi"}], kind="chat")
ok("对话主模型失败不重复打（避免无效重试）", text2 == "" and len(_calls) == 1,
   _calls)

# ── 11. check_models：按用途真打一次 ──────────────────────────────
llm._post_chat = fake_by_model       # pro 404、其余可用
rep = llm.check_models()
ok("check_models 逐个用途都试了", len(rep["results"]) == 4, len(rep["results"]))
ok("check_models 报告可用/不可用", rep["usable"] and rep["unusable"],
   (rep["usable"], rep["unusable"]))
ok("不可用的用途给出该改哪一行环境变量",
   rep["suggested_env"].get("DEEPSEEK_REASON_MODEL") == FLASH,
   rep["suggested_env"])
ok("check_models 有给人看的结论", bool(rep.get("note")), rep.get("note"))
ok("有不可用项时整体 ok=False", rep["ok"] is False, rep["ok"])

llm._post_chat = lambda payload, timeout=None: (True, {
    "choices": [{"message": {"content": "pong"}, "reasoning_content": ""}]})
rep2 = llm.check_models()
ok("全通时 ok=True", rep2["ok"] is True and not rep2["suggested_env"], rep2["ok"])
ok("全通时结论说能调通", "能调通" in (rep2.get("note") or ""), rep2.get("note"))

llm.API_KEY = ""
rep3 = llm.check_models()
ok("没配 key 时明确告知无法检查",
   rep3["ok"] is False and "无法检查" in (rep3.get("note") or ""),
   rep3.get("note"))

llm.API_KEY = "sk-dummy"
llm._MODEL_FALLBACKS.clear()

# ── 12. 可选模型清单（界面上那个下拉列表的数据源）──────────────────
cat = llm.model_catalog()
cat_ids = [m["id"] for m in cat["models"]]
ok("清单里只有两个模型", cat_ids == [FLASH, PRO], cat_ids)
ok("默认模型是 flash", cat["default"] == FLASH, cat["default"])
ok("默认项被标成 default",
   [m["default"] for m in cat["models"] if m["id"] == FLASH] == [True])
ok("每个模型都有中文说明", all(m.get("desc") for m in cat["models"]),
   [m["id"] for m in cat["models"] if not m.get("desc")])
ok("每个模型都标了用途",
   all(m.get("purpose") in ("chat", "fast", "reason", "vision")
       for m in cat["models"]), [m["purpose"] for m in cat["models"]])
ok("两个模型都标 configured=True",
   all(m["configured"] for m in cat["models"]))
ok("flash 带 multimodal 标记（发图时用它）",
   [m.get("multimodal") for m in cat["models"] if m["id"] == FLASH] == [True])
ok("pro 带 thinking 标记",
   [m.get("thinking") for m in cat["models"] if m["id"] == PRO] == [True])
ok("catalog 里带 supports_thinking", all("supports_thinking" in m
                                          for m in cat["models"]))
ok("两个模型都支持深度思考",
   all(m["supports_thinking"] for m in cat["models"]))

# ── 13. chat_model：DeepSeek 的 OpenAI 兼容对象 ───────────────────
llm.API_KEY = "sk-test"
m_think = llm.chat_model("chat", streaming=True, thinking=True)
ok("开深度思考时切到 pro", m_think.model_name == PRO, m_think.model_name)
ok("DeepSeek 的 pro 也支持流式", m_think.streaming is True, m_think.streaming)
ok("不再往 extra_body 塞 thinking_mode",
   not (getattr(m_think, "extra_body", None) or {}), getattr(m_think, "extra_body", None))
ok("chat_model 指向 DeepSeek base_url",
   (getattr(m_think, "openai_api_base", "") or "").rstrip("/") == llm.BASE_URL,
   getattr(m_think, "openai_api_base", ""))

m_nothink = llm.chat_model("chat", streaming=True, thinking=False)
ok("关深度思考时用 flash", m_nothink.model_name == FLASH, m_nothink.model_name)
ok("关深度思考时保留真流式", m_nothink.streaming is True, m_nothink.streaming)

m_pro = llm.chat_model("chat", model=PRO, thinking=False)
ok("手动选 pro 时就用 pro", m_pro.model_name == PRO, m_pro.model_name)
m_unknown = llm.chat_model("chat", model="gpt-4")
ok("陌生模型名回落到默认", m_unknown.model_name == FLASH, m_unknown.model_name)
m_all = llm.chat_model("chat").model_name
ok("不传 model 时用默认", m_all == llm.model_for("chat"), m_all)

# ── 14. 限流器：间隔够用时不该多等 ────────────────────────────────
fast = llm.RateLimiter(rpm=6000)
t0 = time.time()
for _ in range(20):
    fast.acquire()
elapsed = time.time() - t0
# 20 次、每次最小间隔 0.01s，理论下限约 0.19s；给慢机器留一点余量。
ok("高频限额下不额外等待（20 次 < 0.35s）", elapsed < 0.35, round(elapsed, 3))
ok("限流器记录了阻塞次数", hasattr(fast, "waits"))

slow = llm.RateLimiter(rpm=120)     # 0.5s 一次
t0 = time.time()
slow.acquire()
slow.acquire()
ok("限额低时会真的等待", time.time() - t0 >= 0.4, round(time.time() - t0, 2))

# ── 15. probe：没配 key 时明确拒绝而不是假成功 ────────────────────
llm.API_KEY = ""
p = llm.probe()
ok("没配 key 时不谎报探活成功", p.get("ok") is False, p)

llm.API_KEY = "sk-test"
llm._post_chat = _orig_post
llm.API_KEY = _orig_key
llm._MODEL_FALLBACKS.clear()

# 接入层里只允许出现两个模型 ID
src = io.open(os.path.join(ROOT, "core", "llm.py"), encoding="utf-8").read()
ok("llm.py 里只有两个模型 ID", src.count('"id":') == 2, src.count('"id":'))

# 不写文件、不残留
ok("本用例没有创建任何数据库文件",
   not os.path.exists(os.path.join(ROOT, "_tmp_llm.db")))

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
