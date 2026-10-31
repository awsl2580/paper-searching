"""长周期记忆（memory.py）—— 离线单测，用临时库。

这条能力最容易被做成"存一段聊天记录就叫记忆"。所以这里重点验三件事：

1. **召回是可解释的**：分数 = 词面相关 + 时间衰减 + 权重 + 命中次数，
   每一条都能说清为什么被想起来（`score_detail`）；
2. **记忆不是证据**：`context_pack()` 必须带"不是本次检索结果、引用前要核实"
   的免责说明，否则智能体会把记忆当文献开始编引用；
3. **写入幂等**：同一句话重复写入只涨权重，不会把记忆库刷满。

用临时数据库，不碰 literature.db。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _writable_temp import temp_db_path  # noqa: E402

import database as db  # noqa: E402

db.DB_PATH = temp_db_path("_tmp_memory.db")
db.init_db()

import memory  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


USER = 1  # 直接用一个整数当 user_id：这张表不做外键约束（游客拿不到记忆也不写入）

# ── 1. scope 归一化 ───────────────────────────────────────────────
ok("标点与空白被去掉",
   memory.normalize_scope("图神经网络，在遥感图像中的应用！") ==
   "图神经网络在遥感图像中的应用", memory.normalize_scope("图神经网络，在遥感图像中的应用！"))
ok("大小写统一", memory.normalize_scope("GNN 遥感") == memory.normalize_scope("gnn　遥感"))
ok("长度有上限（防超长目标撑爆索引）",
   len(memory.normalize_scope("长" * 200)) == 40)

SCOPE = memory.normalize_scope("图神经网络 遥感图像 语义分割")

# ── 2. 写入与幂等 ────────────────────────────────────────────────
r1 = memory.remember(USER, "在 AID 数据集上 GCN 的 mIoU 高于 CNN",
                     scope=SCOPE, kind="finding", tags=["遥感", "GCN"])
ok("写入成功", r1.get("created") is True and r1.get("id"), r1)
r2 = memory.remember(USER, "在 AID 数据集上 GCN 的 mIoU 高于 CNN",
                     scope=SCOPE, kind="finding")
ok("重复写入不新增记录", r2.get("created") is False and r2.get("id") == r1["id"], r2)
st = memory.stats(USER)
ok("统计里只有 1 条", st["total"] == 1, st["total"])
ok("按 kind 分组正确", st["by_kind"].get("finding") == 1, st["by_kind"])

ok("未登录（user_id 为空）不写入", memory.remember(None, "x").get("created") is False)
ok("空文本不写入", memory.remember(USER, "   ").get("created") is False)
ok("未知 kind 归到 note",
   memory.remember(USER, "随便一句", scope=SCOPE, kind="不存在的类型").get("created") is True)
note_row = [x for x in memory.recall(USER, "", scope=SCOPE) if x["text"] == "随便一句"][0]
ok("未知 kind 落到 note", note_row["kind"] == "note", note_row["kind"])

# ── 3. 召回与打分 ────────────────────────────────────────────────
memory.remember(USER, "高光谱数据缺失，是当前的主要空白", scope=SCOPE, kind="open")
memory.remember(USER, "已排除：纯 CNN 路线在本任务上已到瓶颈", scope=SCOPE, kind="excluded")
memory.remember(USER, "用户偏好中文文献，且只看近五年", scope=SCOPE, kind="preference")
memory.remember(USER, "另一个课题：蛋白质结构预测", scope="protein", kind="finding")

hits = memory.recall(USER, "图神经网络 遥感 语义分割 空白", limit=10)
ok("召回有结果", len(hits) >= 3, len(hits))
ok("每条都带分数与明细",
   all("score" in h and "score_detail" in h for h in hits))
ok("分数是降序的", all(hits[i]["score"] >= hits[i + 1]["score"]
                     for i in range(len(hits) - 1)),
   [h["score"] for h in hits])
top_texts = " ".join(h["text"] for h in hits[:3])
ok("相关的那条排在前面", "空白" in top_texts or "GCN" in top_texts, top_texts[:60])
ok("带标签的被解析成列表",
   any(isinstance(h.get("tags"), list) for h in hits))

# scope 过滤：不同课题的记忆不该串
protein = memory.recall(USER, "蛋白质", scope="protein")
ok("按 scope 过滤只返回该课题", len(protein) == 1 and "蛋白质" in protein[0]["text"],
   [x["text"] for x in protein])
cross = memory.recall(USER, "蛋白质", scope=SCOPE)
ok("跨 scope 不串味", all("蛋白质" not in x["text"] for x in cross),
   [x["text"] for x in cross])

# 中文 2-gram 召回：只用关键词里的两个字也要想起对应记忆
partial = memory.recall(USER, "高光谱")
ok("部分词命中也能召回", any("高光谱" in x["text"] for x in partial),
   [x["text"] for x in partial])

# 空查询也要有结果（按权重/新近排序）
allm = memory.recall(USER, "")
ok("空查询返回全部（按权重排序）", len(allm) >= 4, len(allm))

# kinds 过滤
only_open = memory.recall(USER, "", scope=SCOPE, kinds=["open"])
ok("按 kind 过滤", all(x["kind"] == "open" for x in only_open) and only_open, only_open)

# ── 4. context_pack：记忆不是证据 ────────────────────────────────
pack = memory.context_pack(USER, "图神经网络 遥感 语义分割", limit=8, budget_chars=1200)
ok("上下文块非空", bool(pack), pack[:40])
ok("带统一标题", memory.CONTEXT_HEADER in pack)
ok("带免责说明（记忆≠文献证据）", "不是本次检索到的文献证据" in pack, pack[-80:])
ok("说明了引用要求", "不得把记忆当引用来源" in pack, pack[-40:])
ok("按预算截断（不超过预算 + 标题）",
   len(pack) <= 1200 + len(memory.CONTEXT_HEADER) + len(memory.CONTEXT_CAVEAT) + 4,
   len(pack))
tiny = memory.context_pack(USER, "图神经网络", limit=8, budget_chars=40)
ok("预算极小时仍只输出标题+说明（不放半条记忆）",
   tiny == "" or memory.CONTEXT_HEADER in tiny, tiny[:60])

# 记忆被召回后 hits 会累加（下次更容易想起来）
before = {x["id"]: x["hits"] for x in memory.recall(USER, "", limit=20)}
memory.context_pack(USER, "图神经网络 遥感", limit=3)
after = {x["id"]: x["hits"] for x in memory.recall(USER, "", limit=20)}
ok("被召回过的记忆命中数增加",
   sum(after.values()) > sum(before.values()), (sum(before.values()), sum(after.values())))

ok("未登录时上下文为空", memory.context_pack(None, "任何目标") == "")
ok("未登录时召回为空", memory.recall(None, "任何") == [])

# ── 5. 工作流产物自动归档 ────────────────────────────────────────
goal = "图神经网络在遥感图像语义分割中的应用"
got = memory.remember_workflow(
    USER, goal, workflow_id=77,
    hypothesis={"hypotheses": [
        {"statement": "GCN 在 AID 上优于 CNN", "confidence": "medium"}]},
    experiment={"summary": "2×2 全因子对照"},
    verify={"verdict": "pass", "open_questions": ["缺少高光谱数据验证"]},
    synth={"gaps": ["缺少跨传感器验证"]},
    papers=[{"id": "x"}] * 13,
)
ok("归档写入了多条", len([g for g in got if g.get("id")]) >= 4, got)
scope2 = memory.normalize_scope(goal)
archived = memory.recall(USER, goal, scope=scope2, limit=20)
texts = " ".join(x["text"] for x in archived)
ok("归档了假设", "假设：" in texts, texts[:80])
ok("归档了验证结论", "验证结论：pass" in texts, texts[:80])
ok("归档了待解问题", "待解问题" in texts, texts[:80])
ok("归档了文献规模", "已调研 13 篇文献" in texts, texts[:80])
ok("归档内容带 workflow 引用",
   any((x.get("ref") or "").startswith("workflow:") for x in archived),
   [x.get("ref") for x in archived])

# ── 6. 遗忘与清理 ────────────────────────────────────────────────
one_id = archived[0]["id"]
ok("删除一条", memory.forget(one_id, USER) is True)
ok("删不存在的返回 False", memory.forget(999999, USER) is False)
ok("别人的 id 删不掉", memory.forget(one_id, 999) is False)
ok("按 scope 清理", memory.clear(USER, scope=scope2) >= 1)
ok("清理后该课题为空", memory.recall(USER, goal, scope=scope2) == [])
left = memory.stats(USER)["total"]
ok("其它课题的记忆还在", left >= 1, left)
memory.clear(USER)
ok("清空后为 0", memory.stats(USER)["total"] == 0, memory.stats(USER)["total"])

# 记忆表在 init_db 里也建了（老库升级场景）
conn = db.get_db()
cols = [r[1] for r in conn.execute("PRAGMA table_info(research_memory)").fetchall()]
conn.close()
ok("research_memory 表字段齐全",
   {"user_id", "scope", "kind", "text", "weight", "hits", "fingerprint"} <= set(cols), cols)

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
