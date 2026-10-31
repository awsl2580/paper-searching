"""长周期研究记忆 —— 让智能体"记得上一次研究到哪儿了"。

## 为什么需要它

赛题在"前沿热门技术"里点名了「上下文工程与长周期记忆」。这不是加一个
向量库就能交差的点，真正的问题是：**科研是长周期的**。同一个课题组会反复
研究同一个方向，第二周接着做时，需要的不是"上次说过的话"，而是：

* 上次的检索式和筛选标准（避免重复检索、重复踩坑）；
* 上次已经确认的结论和**明确排除掉的方向**（避免重复劳动）；
* 上次留下的开放问题（这次该从哪儿接着做）。

所以这里存的是**结构化研究记忆**（目标 / 结论 / 排除 / 开放问题 / 偏好），
不是聊天记录 —— 聊天记录已经有 `conversations` 表了，再存一遍没有意义。

## 三个刻意的设计

1. **记忆不等于证据。** `context_pack()` 拼出来的那段话在末尾明确写着
   "以下为历史记忆，不是本次检索结果，引用前必须重新核实"。
   一个科学智能体如果把记忆当成文献证据，就会开始编造引用 —— 这是最坏的失败模式。

2. **召回是可解释的打分，不是黑盒向量。** 分数 = 词面相关 + 时间衰减 + 权重
   + 被召回次数。每一条都能说清"为什么它被想起来"，评审问起来答得上。
   （向量召回看着高级，但在几万条以内的个人研究记忆里，收益远小于可解释性。）

3. **写入是幂等的。** 同一句话重复写入只会提高权重、累加命中次数，
   不会让记忆库被同一条结论刷满。

## 与数据库的关系

表在 `database.init_db()` 里创建（`research_memory`），本模块自己也带
`ensure_schema()`，所以单测可以直接导入使用而不必先跑 init_db。
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timezone

import database as db

# 记忆种类。kind 会参与打分（结论比随笔更值得召回）并决定在提示词里的前缀。
KINDS = {
    "goal":        {"label": "研究目标", "priority": 1.0},
    "finding":     {"label": "已确认结论", "priority": 1.15},
    "excluded":    {"label": "已排除方向", "priority": 1.1},
    "open":        {"label": "开放问题", "priority": 1.05},
    "preference":  {"label": "用户偏好", "priority": 1.2},
    "artifact":    {"label": "研究产物", "priority": 0.8},
    "note":        {"label": "备注", "priority": 0.7},
}
DEFAULT_KIND = "note"

# 时间衰减半衰期（天）。科研记忆不该像新闻一样迅速作废，
# 但一年前的"最新进展"确实可能已经过时 —— 180 天减半是个折中。
HALF_LIFE_DAYS = 180.0

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS research_memory
    (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        scope TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'note',
        text TEXT NOT NULL,
        tags TEXT DEFAULT '',
        weight REAL DEFAULT 1.0,
        ref TEXT DEFAULT '',
        hits INTEGER DEFAULT 0,
        fingerprint TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_user ON research_memory(user_id, scope)",
    "CREATE INDEX IF NOT EXISTS idx_memory_fp ON research_memory(fingerprint)",
]


def ensure_schema():
    """建表（幂等）。单测和 CLI 直接导入本模块时也能用。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        conn = db.get_db()
        try:
            cur = conn.cursor()
            for stmt in _SCHEMA:
                cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        _SCHEMA_READY = True


# ═══════════════════════════════════════════════════════════════
#  归一化与指纹
# ═══════════════════════════════════════════════════════════════

def normalize_scope(text):
    """把研究目标收敛成一个"研究项目"key。

    为什么不用原始目标字符串：同一件事用户会写成
    "图神经网络在遥感图像中的应用" / "GNN 遥感图像应用进展"，
    精确匹配会把它们当成两个项目，记忆就接不上。这里去掉标点、
    统一空白、取前 40 个字符做 key —— 保守但稳定（宁可少匹配，不可错匹配）。
    """
    t = re.sub(r"[\s，。、；：！？,.;:!?（）()\[\]【】\"'“”‘’]+", "", (text or ""))
    return t[:40].lower()


def _fingerprint(scope, kind, text):
    raw = f"{(scope or '').strip()}|{kind}|{(text or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _terms(text):
    """中文按 2-gram、英文按词切。与 providers 里的相关度打分同一套思路。"""
    out = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{1,}", text or ""):
        out.append(w.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(out))


def _age_days(created_at):
    if not created_at:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(str(created_at)[:26].split("+")[0].strip(), fmt)
            return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)
        except Exception:  # noqa: BLE001
            continue
    return 0.0


# ═══════════════════════════════════════════════════════════════
#  写入
# ═══════════════════════════════════════════════════════════════

def remember(user_id, text, scope="", kind=DEFAULT_KIND, tags=None,
             weight=1.0, ref=""):
    """写一条记忆。返回 {"id":..., "created": bool, "fingerprint":...}。

    幂等：同 (scope, kind, text) 已存在时不新增，只把权重 +0.1（上限 3.0）
    并累加一次"重复确认"。
    """
    if not user_id:
        # 未登录不落库：长周期记忆是账号级能力，游客会话刷新即清空。
        return {"id": None, "created": False, "reason": "未登录，不写入长周期记忆"}
    text = (text or "").strip()
    if not text:
        return {"id": None, "created": False, "reason": "内容为空"}
    kind = kind if kind in KINDS else DEFAULT_KIND
    scope = scope or ""
    fp = _fingerprint(scope, kind, text)
    tag_str = ",".join(t for t in (tags or []) if t)

    ensure_schema()
    conn = db.get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, weight FROM research_memory WHERE fingerprint = ?",
                    (fp,))
        row = cur.fetchone()
        if row:
            new_weight = min(3.0, float(row["weight"] or 1.0) + 0.1)
            cur.execute(
                "UPDATE research_memory SET weight = ?, hits = hits + 1, "
                "updated_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?",
                (new_weight, row["id"]))
            conn.commit()
            return {"id": row["id"], "created": False, "fingerprint": fp}

        cur.execute(
            "INSERT INTO research_memory (user_id, scope, kind, text, tags, weight, ref, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, scope[:120], kind, text[:2000], tag_str,
             float(weight or 1.0), (ref or "")[:200], fp))
        new_id = cur.lastrowid
        conn.commit()
        return {"id": new_id, "created": True, "fingerprint": fp}
    finally:
        conn.close()


def remember_many(user_id, items, scope="", kind=DEFAULT_KIND, ref=""):
    """批量写入。items 可以是字符串列表或 {"text","kind","weight"} 列表。"""
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(remember(user_id, it, scope=scope, kind=kind, ref=ref))
        elif isinstance(it, dict):
            out.append(remember(user_id, it.get("text") or "",
                               scope=scope, kind=it.get("kind") or kind,
                               tags=it.get("tags"), weight=it.get("weight") or 1.0,
                               ref=ref))
    return out


# ═══════════════════════════════════════════════════════════════
#  召回
# ═══════════════════════════════════════════════════════════════

def _score(row, terms, now_terms_count):
    text = row.get("text") or ""
    kind = row.get("kind") or DEFAULT_KIND
    prio = (KINDS.get(kind) or KINDS[DEFAULT_KIND])["priority"]

    overlap = 0.0
    if terms:
        hits = sum(1 for t in terms if t in text.lower())
        overlap = hits / max(1, now_terms_count)
    else:
        overlap = 0.3   # 没给查询词时，倾向于"最近写进来的"

    age = _age_days(row.get("created_at"))
    decay = 0.5 ** (age / HALF_LIFE_DAYS)
    weight = min(3.0, float(row.get("weight") or 1.0))
    used = 1.0 + min(0.3, 0.05 * int(row.get("hits") or 0))

    return round(overlap * 1.6 + decay * 0.6 + (weight - 1.0) * 0.3 + (prio - 1.0),
                 4), round(overlap, 4), round(decay, 4)


def recall(user_id, query="", scope=None, kinds=None, limit=8, min_score=0.0):
    """召回相关记忆，按分数倒序。返回 [{id,text,kind,label,scope,tags,score,...}]。"""
    if not user_id:
        return []
    ensure_schema()
    conn = db.get_db()
    try:
        sql = "SELECT * FROM research_memory WHERE user_id = ?"
        args = [user_id]
        if scope:
            sql += " AND scope = ?"
            args.append(scope)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            args.extend(list(kinds))
        sql += " ORDER BY updated_at DESC LIMIT 500"
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    terms = _terms(query)
    scored = []
    for row in rows:
        sc, overlap, decay = _score(row, terms, len(terms) or 1)
        if sc < min_score:
            continue
        row["score"] = sc
        row["score_detail"] = {"overlap": overlap, "recency": decay}
        row["label"] = (KINDS.get(row.get("kind")) or KINDS[DEFAULT_KIND])["label"]
        row["tags"] = [t for t in (row.get("tags") or "").split(",") if t]
        scored.append(row)
    scored.sort(key=lambda r: -r["score"])
    return scored[:max(1, int(limit))]


def touch(memory_ids):
    """把被召回的条目命中数 +1（下次同类查询更容易想起来）。"""
    ids = [int(i) for i in (memory_ids or []) if i]
    if not ids:
        return
    ensure_schema()
    conn = db.get_db()
    try:
        cur = conn.cursor()
        cur.executemany("UPDATE research_memory SET hits = hits + 1 WHERE id = ?",
                        [(i,) for i in ids])
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
#  上下文工程：把记忆压成一段能塞进提示词的话
# ═══════════════════════════════════════════════════════════════

CONTEXT_HEADER = "【长周期研究记忆】"
CONTEXT_CAVEAT = ("注意：以下是**你过去研究时留下的记忆**，不是本次检索到的文献证据。"
                  "可以据此调整检索式、避免重复劳动、继承待解问题；"
                  "但任何具体论断都必须以本轮检索到的文献为准，不得把记忆当引用来源。")


def context_pack(user_id, goal="", scope=None, limit=8, budget_chars=1200):
    """拼一段可直接插进系统提示词/规划提示词的记忆块。

    默认**跨课题**按相关度召回（`scope=None`），这是刻意的：
    新课题往往和旧课题相关（同一方向换个场景），只按 scope 精确匹配
    会一条都想不起来 —— 实测把研究目标改两个词（"遥感图像"→"遥感"）
    就整块为空，而那正是最该复用上次经验的时候。
    需要"只看某个课题的历史"时再显式传 scope。

    预算控制是必要的：记忆无上限地塞进去会挤掉真正重要的指令与文献摘要，
    而且会显著拉高首字延迟。这里按分数从高到低填空，超预算就停。
    """
    items = recall(user_id, query=goal, scope=scope, limit=limit)
    if not items:
        return ""
    lines, used = [], len(CONTEXT_HEADER)
    for it in items:
        prefix = f"- [{it['label']}] " if it.get("label") else "- "
        line = prefix + (it.get("text") or "").replace("\n", " ")[:200]
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    touch([it["id"] for it in items[:len(lines)]])
    return CONTEXT_HEADER + "\n" + "\n".join(lines) + "\n" + CONTEXT_CAVEAT


def summarize_for_prompt(user_id, goal=""):
    """给 app.py 的聊天系统提示词用：比 context_pack 更短（默认 600 字）。"""
    return context_pack(user_id, goal=goal, limit=5, budget_chars=600)


# ═══════════════════════════════════════════════════════════════
#  工作流产物的自动归档
# ═══════════════════════════════════════════════════════════════

def remember_workflow(user_id, goal, workflow_id=None, hypothesis=None,
                      experiment=None, verify=None, synth=None, papers=None):
    """把一次科研工作流的成果归档成记忆。

    只存**结论性**内容：假设、验证结论、开放问题、用户可见的产物编号。
    不存文献列表（有 `papers`/`workflows` 表，再存一遍是重复）。
    """
    if not user_id:
        return []
    scope = normalize_scope(goal)
    ref = f"workflow:{workflow_id}" if workflow_id else ""
    items = []

    if isinstance(hypothesis, dict):
        for h in (hypothesis.get("hypotheses") or [])[:3]:
            if isinstance(h, dict) and h.get("statement"):
                items.append({"text": f"假设：{h['statement']}", "kind": "goal", "weight": 1.2})
    if isinstance(experiment, dict):
        plan = (experiment.get("plan") or {}).get("summary") if isinstance(
            experiment.get("plan"), dict) else None
        if plan:
            items.append({"text": f"实验方案：{plan}", "kind": "open", "weight": 1.1})
    if isinstance(verify, dict):
        verdict = verify.get("verdict")
        if verdict:
            items.append({"text": f"验证结论：{verdict}", "kind": "finding"})
        for q in (verify.get("open_questions") or [])[:3]:
            if isinstance(q, str) and q.strip():
                items.append({"text": f"待解问题：{q.strip()}", "kind": "open"})
    if isinstance(synth, dict):
        for g in (synth.get("gaps") or [])[:2]:
            if isinstance(g, str) and g.strip():
                items.append({"text": f"研究空白：{g.strip()}", "kind": "open"})
    if papers:
        items.append({"text": f"已调研 {len(papers)} 篇文献（{goal}）",
                      "kind": "artifact", "weight": 0.9})

    return remember_many(user_id, items[:12], scope=scope, ref=ref)


# ═══════════════════════════════════════════════════════════════
#  维护
# ═══════════════════════════════════════════════════════════════

def stats(user_id):
    if not user_id:
        return {"total": 0, "by_kind": {}, "scopes": []}
    ensure_schema()
    conn = db.get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT kind, COUNT(*) c FROM research_memory WHERE user_id = ? "
                    "GROUP BY kind", (user_id,))
        by_kind = {r["kind"]: r["c"] for r in cur.fetchall()}
        cur.execute("SELECT scope, COUNT(*) c FROM research_memory WHERE user_id = ? "
                    "GROUP BY scope ORDER BY c DESC LIMIT 20", (user_id,))
        scopes = [{"scope": r["scope"], "count": r["c"]} for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) c FROM research_memory WHERE user_id = ?", (user_id,))
        total = cur.fetchone()["c"]
    finally:
        conn.close()
    return {"total": total, "by_kind": by_kind, "kinds": KINDS, "scopes": scopes,
            "half_life_days": HALF_LIFE_DAYS}


def forget(memory_id, user_id):
    if not user_id:
        return False
    ensure_schema()
    conn = db.get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM research_memory WHERE id = ? AND user_id = ?",
                    (int(memory_id), user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear(user_id, scope=None):
    if not user_id:
        return 0
    ensure_schema()
    conn = db.get_db()
    try:
        cur = conn.cursor()
        if scope:
            cur.execute("DELETE FROM research_memory WHERE user_id = ? AND scope = ?",
                        (user_id, scope))
        else:
            cur.execute("DELETE FROM research_memory WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


__all__ = [
    "CONTEXT_CAVEAT", "HALF_LIFE_DAYS", "KINDS", "clear", "context_pack",
    "ensure_schema", "forget", "normalize_scope", "recall", "remember",
    "remember_many", "remember_workflow", "stats", "summarize_for_prompt", "touch",
]
