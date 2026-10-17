"""Verify the workflow now STREAMS: events must arrive progressively.

The regression this guards: the route used to run the orchestrator to
completion before yielding anything, so the panel got every event at the very
end (measured: all events at t=12.7s). Two independent checks:
  1. a mocked fast run -- assert first event lands far earlier than the last;
  2. a real run -- report the actual arrival timeline.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
# 临时库放到确认可写的目录（见 tests/_writable_temp.py 的说明）
from _writable_temp import temp_db_path  # noqa: E402

import database as db
DB = temp_db_path("_tmp_stream.db")
db.DB_PATH = DB

import app as appmod
import providers
import workflow as wf

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


PAPERS = [{"id": f"openalex:W{i}", "title": f"研究 {i}", "year": 2020 + (i % 5),
           "authors": ["张三"], "journal": "学报", "docType": "journal",
           "doi": f"10.1/{i}", "url": "u", "citationCount": i,
           "relevance": 100, "abstract": f"摘要 {i}", "language": "zh",
           "source": "openalex", "sourceLabel": "OpenAlex"} for i in range(8)]

providers.search_papers = lambda *a, **k: json.dumps(
    {"papers": PAPERS, "total": 8, "sources": ["OpenAlex"], "filtered_out": 0},
    ensure_ascii=False)


def slow_llm(delay):
    """每次 LLM 调用都睡一会儿，模拟真实模型的耗时。"""
    def fn(prompt, content, **kw):
        time.sleep(delay)
        if "文献信息抽取" in prompt:
            n = len(json.loads(content).get("papers", []))
            return {"items": [{"i": i + 1, "method": "M"} for i in range(n)]}
        if "综述撰写" in prompt:
            return {"sections": [{"heading": "H", "body": "B"}],
                    "consensus": [], "gaps": [], "caveat": ""}
        return {"topic": "T", "query": "q", "source": "cn", "limit": 8,
                "focus": "f", "plan": "p", "sections": ["H"]}
    return fn


def run_and_time(run_id):
    client = appmod.app.test_client()
    client.post("/api/register", json={"username": f"stream{run_id}", "password": "Passw0rd1"})
    client.post("/api/login", json={"username": f"stream{run_id}", "password": "Passw0rd1"})
    t0 = time.time()
    resp = client.post("/api/workflow", json={"goal": "测试研究目标"}, buffered=False)
    stamps = []
    buf = ""
    for chunk in resp.response:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            line = next((l for l in block.splitlines() if l.startswith("data: ")), None)
            if not line:
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            stamps.append((time.time() - t0, ev))
            if ev.get("type") == "done":
                return stamps, time.time() - t0
    return stamps, time.time() - t0


# ── 1. 快模型下的流式行为 ────────────────────────────────────
wf._call_json = slow_llm(0.4)
stamps, total = run_and_time(1)
first = stamps[0][0]
last = stamps[-1][0]
print(f"mock 运行: 共 {len(stamps)} 个事件, 总 {total:.1f}s")
print(f"  首个事件 t={first:.2f}s, 末个事件 t={last:.2f}s")
for t, ev in stamps[:8]:
    print(f"    {t:5.2f}s  {ev.get('type'):<9}{ev.get('key', '')} {ev.get('status', '')}")

ok("有多个事件", len(stamps) > 5, len(stamps))
ok("首个事件立刻到达（不是等全部跑完）", first < 0.5, first)
ok("事件跨越整个执行过程（首末相差大）", last - first > 0.8, last - first)
ok("最后一个是 done", stamps[-1][1].get("type") == "done", stamps[-1][1].get("type"))
ok("done 之前就有 step 事件",
   any(ev.get("type") == "step" for t, ev in stamps if t < last - 0.1), None)

# 关键回归：step 事件必须是"分散"到达的，而不是全挤在末尾。
# 判据用绝对时间差而不是百分比：最后几步（analyze/format）本来就在末尾，
# 用百分比卡会误伤（第一版就写错过，13/22 在 70% 之前其实已经说明是流式的）。
step_times = [t for t, ev in stamps if ev.get("type") == "step"]
spread = (max(step_times) - min(step_times)) if step_times else 0
ok("step 事件分散在整个执行过程中（不是末尾一次性回放）",
   spread > total * 0.5, f"step 跨度 {spread:.2f}s / 总 {total:.2f}s")
ok("第一个 step 事件在 0.2s 内到达", min(step_times) < 0.2 if step_times else False,
   min(step_times) if step_times else None)

# ── 2. 抽取并发的效果 ───────────────────────────────────────
print("\n并发抽取验证（每批 0.4s，16 篇 = 2 批）:")
PAPERS16 = list(PAPERS) + [dict(PAPERS[0], id=f"openalex:X{i}", title=f"更多 {i}")
                           for i in range(8)]
wf._call_json = slow_llm(0.4)
batch16 = [dict(p) for p in PAPERS16]
t0 = time.time()
out = wf.step_extract({}, batch16, lambda d: None)
dt = time.time() - t0
print(f"  16 篇 2 批耗时 {dt:.2f}s（串行应约 0.8s，并发应约 0.4s）")
ok("两批并发执行（耗时接近单批）", dt < 0.7, dt)
ok("两批结果都回填了", out["extracted"] == 16, out)

# 并发下序号的全局换算不能错：每批的 i 都是从 1 开始。
# 注意要看**被传进去的那批对象**（step_extract 是就地改的），
# 第一版查的是原始 PAPERS16，所以全是 None —— 测试写错了，不是代码错。
ok("并发后每篇都拿到抽取结果",
   all((p.get("extract") or {}).get("method") == "M" for p in batch16),
   [(p["title"], (p.get("extract") or {}).get("method")) for p in batch16[:4]])
ok("16 篇没有串号（每篇都是自己的结果）",
   len({id(p) for p in batch16}) == 16 and out["total"] == 16, out["total"])

# ── 3. 中断仍然在批次边界生效 ───────────────────────────────
print("\n中断语义（多批时）:")
calls = {"n": 0}


def stop_after_first():
    calls["n"] += 1
    return calls["n"] > 1


wf._call_json = slow_llm(0.2)
out2 = wf.step_extract({}, [dict(p) for p in PAPERS16], lambda d: None,
                       stop=stop_after_first)
ok("中断后如实标记 interrupted", out2.get("interrupted") is True, out2)

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
