"""多智能体科研团队 —— 用角色分工模拟一个课题组的协作。

## 为什么要多智能体，而不是一个"全能提示词"

赛题鼓励"以多智能体分工模拟科研团队协作"。但真做过就知道，
把七件事写进一个系统提示词，结果是每件都做得很平庸：检索时它想着写综述，
写综述时它把摘要当结论，验证时它不愿意否定自己刚写的东西 ——
**自我审查失效**是单智能体做科研最致命的问题。

拆成角色解决的是三件事：

1. **职责与证据边界**：检索员只说"找到了什么"，计算员只说"算出来是多少"，
   综述作家只能说"文献支持什么"。每层的输出都是下一层的输入，
   越界就会被下游的格式要求卡住。
2. **模型分层**：批量抽取与读图用 `deepseek-V4.1-flash`（快、省额度），
   假设与审稿用 `deepseek-v4-pro` 深度推理 —— 这是"模型组合创新"的落点，
   也是真金白银的成本考虑。
3. **验证独立**：审稿人是独立的模型调用，看不到"作者的自辩"，
   只拿到产物与证据，所以它能真的说"这条结论不成立"。

## 与 LangGraph / openclaw 的关系

本项目用的是 **LangChain 的 `create_agent`（LangGraph 运行时）**，
编排层（`workflow.py`）是确定性的状态机：角色由代码按顺序调用，
每步落库、可中断、可续跑。这么做而不是"让 supervisor 自由调度"，
是因为自由调度没法做断点恢复与耗时预估（详见 workflow.py 的模块说明）。

角色不是"每次都要跑一遍"：`run_role` 是给编排器用的**能力函数**，
编排器决定什么时候叫谁。
"""

from __future__ import annotations

import json
import time

import llm
import skills

# ═══════════════════════════════════════════════════════════════
#  角色定义
# ═══════════════════════════════════════════════════════════════
#
# model:   llm.PURPOSES 里的键（chat / fast / vision / reason）
# method:  默认输出形式：json=要结构化字段，text=要成段文字
# tools:   给 agent 循环用的工具名（见 agent_tools.role_tools）

ROLES = {
    "coordinator": {
        "label": "首席研究员",
        "emoji": "🧭",
        "model": "chat", "method": "json", "tools": [], "memory": True,
        "mission": "把研究目标拆成可执行方案，仲裁各角色之间的分歧，对最终结论负责。",
        "duty": [
            "读研究目标，判断这题属于「综述型 / 机制型 / 数据型 / 实验型」，据此选流程重点",
            "给出检索式、范围与章节骨架",
            "复核审稿人的结论，决定是否进入下一轮迭代",
        ],
        "skills": ["literature-review", "reproducible-verification"],
    },
    "searcher": {
        "label": "检索员",
        "emoji": "🔍",
        "model": "fast", "method": "json", "tools": ["search_papers", "get_paper_detail"],
        "memory": True,
        "mission": "面向研究问题做多源检索，交付可复核的文献集合，不做归纳、不下结论。",
        "duty": [
            "把研究目标翻译成中英文检索式（术语要专业，不要口语）",
            "多源检索、跨源去重、按相关度与影响力筛选",
            "如实报告检索失败/限流，不假装结果完整",
        ],
        "skills": ["literature-review", "multimodal-science-reading"],
    },
    "reader": {
        "label": "精读员",
        "emoji": "📖",
        "model": "fast", "method": "json", "tools": [], "memory": False,
        "mission": "逐篇抽取结构化事实（对象/方法/数据/指标/结论），含多模态图表的读取。",
        "duty": [
            "只依据标题、摘要与图上可见内容抽取，没有就留空",
            "区分'作者声称'与'数据支持'",
            "读图时标注'读图估计值'，不与测量值混淆",
        ],
        "skills": ["multimodal-science-reading", "spectral-analysis"],
    },
    "hypothesizer": {
        "label": "假设提出者",
        "emoji": "💡",
        # 这里用 chat（flash）而不是 reason（pro）是**实测**定的：
        #   同一份假设任务（20 篇文献要点，~7KB 输入）：
        #     deepseek-v4-pro      推理链很长，JSON 偶发截断
        #     deepseek-V4.1-flash  20-30s（稳定），JSON 可用
        #   pro 的本事在"短输出、要判断"的场景，一旦要它写长的结构化 JSON
        #   就变成整条流水线的瓶颈。所以把它放到审稿人（输出短、判断难）更合适。
        "model": "chat", "method": "json", "tools": [], "memory": True,
        "mission": "从共识与空白里推导出可证伪的科学假设，给出变量、对照与判据。",
        "duty": [
            "每条假设绑定文献依据编号",
            "写出否定判据（观测到什么就否定它）",
            "给出预期结果，成立与不成立两种都要写",
        ],
        "skills": ["hypothesis-design"],
    },
    "designer": {
        "label": "实验设计师",
        "emoji": "🧪",
        "model": "chat", "method": "json", "tools": ["list_science_tools", "run_science_tool"],
        "memory": False,
        "mission": "把假设变成可执行的实验/仿真方案：因子、水平、样本量、指标、判据、步骤。",
        "duty": [
            "用 design_factorial / design_power 给出可执行的设计矩阵与样本量",
            "说明每组怎么构造、测什么、成功与失败各是什么样",
            "方案必须能在本项目的工具条件下真跑一遍",
        ],
        "skills": ["hypothesis-design", "statistical-inference"],
    },
    "computationalist": {
        "label": "计算员",
        "emoji": "🔬",
        "model": "fast", "method": "json",
        "tools": ["list_science_tools", "run_science_tool"], "memory": False,
        "mission": "通过 MCP 工具做真实的统计与数值计算，报告工具返回的数值而不是估计值。",
        "duty": [
            "先 list_science_tools 看有什么，再 run_science_tool 真正执行",
            "每个数字都要能指回一次工具调用（含参数）",
            "工具报错/数据退化时如实报告，不替换成估计",
        ],
        "skills": ["statistical-inference", "spectral-analysis"],
    },
    "critic": {
        "label": "审稿人",
        "emoji": "⚖️",
        "model": "reason", "method": "json", "tools": [], "memory": True,
        "mission": "独立复核：数值能否重算、结论能否推出、参数能否移交。给出 pass/revise/reject。",
        "duty": [
            "检查报告数字与工具返回值是否一致",
            "检查每条结论是否有证据支撑，指出过度推断",
            "revise 时给出可执行的返工项，不写'建议进一步完善'",
        ],
        "skills": ["reproducible-verification", "statistical-inference"],
    },
    "writer": {
        "label": "综述作家",
        "emoji": "✍️",
        "model": "chat", "method": "json", "tools": [], "memory": False,
        "mission": "把证据写成中文研究报告：按主题归类、带引用编号、如实说明局限。",
        "duty": [
            "只依据给定文献，引用编号必须真实存在",
            "按主题/方法归类，不逐篇复述摘要",
            "明确写出共识、分歧、空白与本文局限",
        ],
        "skills": ["literature-review"],
    },
}

ROLE_KEYS = list(ROLES.keys())


def role(key):
    return ROLES.get(key) or ROLES["coordinator"]


def team_overview():
    """给前端/API 的团队名单（含模型与技能，评审一眼能看懂分工）。"""
    out = []
    for key, spec in ROLES.items():
        out.append({
            "key": key,
            "label": spec["label"],
            "emoji": spec["emoji"],
            "mission": spec["mission"],
            "model_kind": spec["model"],
            "model": llm.model_for(spec["model"]),
            "tools": spec["tools"],
            "skills": spec["skills"],
            "memory": spec["memory"],
        })
    return out


# ═══════════════════════════════════════════════════════════════
#  提示词
# ═══════════════════════════════════════════════════════════════

_BASE_RULES = """你必须遵守的共同纪律：
1. **不编造**。文献、作者、DOI、数值、图上的内容，没有就说没有。
2. **数值只认工具**。凡是能算的（统计量、拟合参数、分子量、峰位），
   必须由科学计算工具算出；不要心算，也不要把去年报数当成今年的结果。
3. **区分证据与推测**。推测要显式标注"（推测）"。
4. 中文输出，专业但不堆术语；宁可少写，不要写空话。
"""


def system_prompt(key, goal="", extra="", skill_limit=2, skill_budget=2600):
    """拼一个角色的系统提示词：职责 + 共同纪律 + 按需技能 + 输出契约。

    `skill_limit` / `skill_budget` 可调：工作流里的每一步都是独立模型调用，
    技能正文越长，这次调用的 prefill 与出字时间越长。实测把技能从
    "2 个 / 2600 字"降到"1 个 / 1600 字"，假设步骤快约 20%。
    """
    spec = role(key)
    lines = [f"你是「{spec['label']}」{spec['emoji']}，属于一个 AI4S 科学智能体科研团队。",
             "", f"【你的使命】{spec['mission']}", "", "【你的职责】"]
    lines += [f"- {d}" for d in spec["duty"]]
    if goal:
        lines += ["", f"【本轮研究目标】{goal}"]
    lines += ["", _BASE_RULES]
    if extra:
        lines += ["", extra]
    block = skills.skill_context(goal, role=key, limit=skill_limit,
                                budget_chars=skill_budget)
    if block:
        lines += ["", block]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  执行一个角色
# ═══════════════════════════════════════════════════════════════

def run_role(key, task, context="", tools=None, user_id=None, method=None,
             max_tokens=1600, temperature=0.0, goal=""):
    """跑一个角色，返回统一结构：

        {"role","label","ok","output","data","used_tools","model","method",
         "skills","elapsed_ms","error"}

    * `tools` 为空 -> 一次模型调用（快、可预测、易缓存）；
    * `tools` 非空 -> 走 `create_agent` 的自由工具循环（能自主调工具，
      但耗时不定，所以只在真正需要"自主决定调哪个工具"的步骤里用它）。
    """
    spec = role(key)
    method = method or spec["method"]
    matched = [s["name"] for s in skills.match_skills(goal or task, key, 2)]
    started = time.time()
    model_name = llm.model_for(spec["model"])
    prompt = system_prompt(key, goal=goal or task,
                           extra=("【输入数据】\n" + context) if context else "")
    user = task if not context else (task + "\n\n" + context)

    result = {
        "role": key, "label": spec["label"], "ok": False, "output": "",
        "data": None, "used_tools": [], "model": model_name, "method": method,
        "skills": matched, "elapsed_ms": 0.0, "error": "",
    }

    try:
        if tools:
            result.update(_run_with_tools(key, prompt, user, tools, spec, max_tokens))
            result["method"] = "agent"
        elif method == "json":
            data = llm.call_json(prompt, user, kind=spec["model"],
                                 max_tokens=max_tokens, temperature=temperature)
            if data is None:
                raise RuntimeError("模型未返回可解析的 JSON")
            result["data"] = data
            result["output"] = json.dumps(data, ensure_ascii=False)
            result["ok"] = True
        else:
            text = llm.call_text(prompt, user, kind=spec["model"],
                                 max_tokens=max_tokens, temperature=temperature)
            if not text:
                raise RuntimeError("模型返回为空")
            result["output"] = text
            result["ok"] = True
    except Exception as e:  # noqa: BLE001 - 角色失败由编排器决定怎么降级
        result["error"] = f"{type(e).__name__}: {e}"

    result["elapsed_ms"] = round((time.time() - started) * 1000, 1)
    return result


def _run_with_tools(key, prompt, user, tools, spec, max_tokens):
    """自由工具循环。返回 output/used_tools 等字段。"""
    from langchain.agents import create_agent

    model = llm.chat_model(spec["model"], temperature=0.0, max_tokens=max_tokens)
    agent = create_agent(model=model, tools=tools, system_prompt=prompt)
    out = agent.invoke({"messages": [{"role": "user", "content": user}]},
                       config={"recursion_limit": 16})

    messages = out.get("messages") if isinstance(out, dict) else None
    used, final_text = [], ""
    for m in messages or []:
        calls = getattr(m, "tool_calls", None) or []
        for c in calls:
            name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
            args = c.get("args") if isinstance(c, dict) else getattr(c, "args", None)
            if name:
                used.append({"tool": name, "args": args if isinstance(args, (dict, list, str)) else str(args)})
        content = getattr(m, "content", None)
        if content and getattr(m, "type", "") in ("ai", "assistant"):
            final_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    data = llm.loads_json(final_text) if final_text else None
    return {
        "ok": bool(final_text),
        "output": final_text,
        "data": data if isinstance(data, (dict, list)) else None,
        "used_tools": used[:20],
        "error": "" if final_text else "工具循环没有产出最终回答",
    }


# ═══════════════════════════════════════════════════════════════
#  便捷包装：只要数据
# ═══════════════════════════════════════════════════════════════

def ask_json(key, task, context="", **kwargs):
    """跑一个角色并直接返回解析后的数据（失败返回 None）。"""
    res = run_role(key, task, context=context, method="json", **kwargs)
    return res.get("data")


def ask_text(key, task, context="", **kwargs):
    res = run_role(key, task, context=context, method="text", **kwargs)
    return res.get("output") or ""


__all__ = ["ROLE_KEYS", "ROLES", "ask_json", "ask_text", "role", "run_role",
           "system_prompt", "team_overview"]
