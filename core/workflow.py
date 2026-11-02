"""AI4S 科研工作流编排器 —— 「假设—实验—验证—迭代」闭环的落地。

赛题（方向 1：AI4S 科学智能体）要求覆盖「搜、读、算、做、写」全链条，
并形成"假设—实验—验证—迭代"的端到端科学发现闭环。本文件就是那条链：

| 赛题要求 | 本项目的步骤 key | 由谁做 | 产物 |
|----------|------------------|--------|------|
| 搜：科学文献与多模态数据的精准检索 | `search` | 检索员 | 多源去重后的文献集合 |
| 读：长文本与科学数据理解 | `extract` | 精读员 | 对象/方法/数据/指标/结论 |
| 读：科学数据解析 | `analyze` | 代码（确定性） | 年份/被引/类型/来源分布 |
| 推导：科学假设生成与逻辑推导 | `hypothesis` | 假设提出者 | 可证伪假设 + 依据 + 判据 |
| 做：实验/仿真方案设计 | `experiment` | 实验设计师 | 因子/水平/样本量/设计矩阵 |
| 算：科学计算工具自主调用 | `compute` | 计算员（MCP） | 真实数值 + `repro` 复现凭据 |
| 验证：结果可复现验证与迭代优化 | `verify` | 审稿人 + 代码复算 | 三类检查 + verdict + 返工项 |
| 写：报告与成果 | `synthesize` / `format` | 综述作家 | 带引用的报告 + GB/T 7714 |

## 三个关键设计取舍

**① 确定性编排，而不是让模型自由调工具。**
步骤由 `plan_workflow()` 规划，但每一步"做什么"是代码写死的。这样才有：
可预估的耗时、可预估的费用、**可中断可续跑**（自由工具循环中断了没法恢复），
以及可复现（同一份输入，步骤顺序不会随机变）。模型只在"读文本/出判断"
的地方发挥，并且它的输出都要经过格式校验。

**② 每一步的产物都带证据链。**
抽取字段记来源篇号；假设记 `basis` 文献编号；计算记 `repro`（工具名+参数）；
验证记复算比对结果。这样报告里的每个数字都能一路追到出处 ——
这是"可复现验证"的前提，也是评审最容易验证的一点。

**③ 失败不炸整条流水线，但**不粉饰**。**
单步失败标记 failed 并写清原因，后续步骤用降级产物继续（例如模型没给出假设，
就用文献统计推出候选假设并标注 `degraded: true`）。报告里会如实写
"哪一步降级了、为什么"，而不是假装一切顺利。
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import llm
import mcp_client
import providers

# ═══════════════════════════════════════════════════════════════
#  步骤定义
# ═══════════════════════════════════════════════════════════════
#
# key     步骤标识（落库、前端状态、断点续跑都用它，**不能随便改**）
# title   给人看的中文名
# core    对应赛题"搜/读/算/做/写"里的哪个环节（前端显示成小徽章）
# detail  一句话说明这一步干什么
STEPS = [
    {"key": "search", "title": "文献检索与筛选", "core": "搜",
     "detail": "多源并发检索，跨源去重，按相关度与影响力筛选"},
    {"key": "extract", "title": "精读与信息抽取", "core": "读",
     "detail": "逐篇抽取研究对象、方法、数据、指标、结论"},
    {"key": "analyze", "title": "文献计量与可视化", "core": "读",
     "detail": "年份/被引/类型/来源分布，识别证据缺口"},
    {"key": "hypothesis", "title": "科学假设与推导", "core": "想",
     "detail": "从共识与空白推导可证伪假设，给变量与判据"},
    {"key": "experiment", "title": "实验方案设计与仿真", "core": "做",
     "detail": "因子水平、样本量、设计矩阵、指标与成功判据"},
    {"key": "compute", "title": "科学计算工具调用", "core": "算",
     "detail": "经 MCP 调用统计/谱图/拟合/分子工具，取真实数值"},
    {"key": "verify", "title": "复现校验与迭代", "core": "验",
     "detail": "重算比对 + 证据核查 + 独立审稿，给出 pass/revise/reject"},
    {"key": "synthesize", "title": "综述与报告撰写", "core": "写",
     "detail": "按主题归类写综述，只依据给定文献并标注引用"},
    {"key": "format", "title": "参考文献与成果导出", "core": "写",
     "detail": "GB/T 7714 参考文献 + 复现清单 + Markdown 报告"},
]

STEP_KEYS = [s["key"] for s in STEPS]
_STEP_INDEX = {s["key"]: i for i, s in enumerate(STEPS)}

# 迭代上限：默认 1 次额外迭代（即最多 2 轮「假设—实验—算—验」）。
# 定小一点的理由：模型每轮都要重跑假设与实验设计，一次约 10-20s；
# 而且审稿人的 revise 意见如果两轮都不收敛，第三轮基本也不会收敛，
# 不如如实报"未收敛"并给出返工项。
MAX_ITERATIONS = max(0, int(os.getenv("AI4S_MAX_ITERATIONS", "1") or 1))

# 默认只在 `revise`（方向成立、要返工）时迭代。
# `reject` 意味着审稿人认为核心结论不成立 —— 用同一份证据、同一条流程重跑一遍，
# 结论不会变，只会白烧 1-2 分钟模型时间（实测一轮 reject 返工 ≈ +100s）。
# 想连 reject 也返工时设 AI4S_ITERATE_ON_REJECT=1。
ITERATE_ON_REJECT = (os.getenv("AI4S_ITERATE_ON_REJECT", "0") or "0").strip() in (
    "1", "true", "yes", "on")

# 每个"读全部文献"的步骤最多喂多少篇（按相关度/影响力取前 N）。
# 实测：35 篇全喂给模型，单次输出要 60-90s；20 篇能省一半时间而结论几乎不变
# （长尾文献的边际信息量很低，而且综述正文里的引用编号本来就集中在前面）。
BRIEF_PAPERS = max(5, int(os.getenv("AI4S_BRIEF_PAPERS", "20") or 20))

# 综述一次写完时的输出额度。原来的 4000 是**不够的**（实测踩坑）：
# `max_tokens` 是"思考内容 + 正文"共用的，第 14 次运行里 flash 先思考了 2698
# token，正文写到第 3 节就被砍断（finish_reason=length），JSON 少了收尾括号，
# 两轮重试都是同一个预算 → 整步必挂。5 节中文正文约 3500-4500 token，
# 加上思考要 6000+，所以默认给 7000（上限 8192），可用 AI4S_SYNTH_TOKENS 覆盖。
SYNTH_TOKENS = min(8192, max(2400, int(os.getenv("AI4S_SYNTH_TOKENS", "7000") or 7000)))

# 逐节撰写时单节的额度。一节 300-700 字（约 700-1200 token），
# 但**思考内容也吃这份预算**：实测 1600 时，思考一多就"只有思考、正文为空"，
# 那一节直接丢掉。3000 给思考留了余量；仍不够时 `call_json` 的重试还会再抬 1.6 倍。
SYNTH_SECTION_TOKENS = min(6000, max(800, int(
    os.getenv("AI4S_SYNTH_SECTION_TOKENS", "3000") or 3000)))

# 计算步骤里模型提出的计算任务上限（防跑飞）
MAX_MODEL_TASKS = 6


# ═══════════════════════════════════════════════════════════════
#  提示词
# ═══════════════════════════════════════════════════════════════

PLANNER_PROMPT = """你是科研工作流规划器（AI4S 科学智能体）。用户给出一个研究目标，
你要产出一份可执行的「检索 + 研究」计划。

只输出 JSON：
- "topic"：把研究目标凝练成一个检索主题（中文主题用中文，英文主题用英文），
  去掉"帮我研究""写一篇"这类祈使语，保留专业术语。
- "query"：实际送去检索的关键词，可以比 topic 更精炼（用空格分隔的 2-4 个词）。
- "queries"：（可选）2-3 条互补检索式，形如 ["中文式", "英文式", "近义术语式"]，
  用于提高召回；不给就只用 query。
- "source"："all" | "cn" | "en"。用户提到中文文献/国内研究给 "cn"，
  英文/外文/SCI 给 "en"，否则 "all"。
- "limit"：检索条数，10-30 的整数，默认 20。
- "focus"：一句话说明这个综述应重点回答什么（15-40 字）。
- "plan"：一句话说明你的检索与研究思路（给用户看，30 字以内）。
- "sections"：综述的 3-5 个章节标题（字符串数组），要贴合 topic。
- "research_type"：以下之一 —— "review"（综述/进展）、"mechanism"（机理/因果）、
  "data"（数据分析/建模）、"experiment"（实验/仿真方案）。
  这决定后面假设与实验的写法。
- "metrics"：（可选）2-4 个该领域常用的评价指标名（如 "mIoU"、"F1"、"RMSE"）。

只输出 JSON，不要解释文字或 markdown 代码块。"""

EXTRACT_PROMPT = """你在做文献信息抽取（AI4S 科研流程的"读"环节）。下面给你若干篇论文的
标题和摘要（可能来自多语言源）。

对**每一篇**抽取：
- "i"：论文序号（从 1 开始，与输入顺序一致）
- "object"：研究对象 / 应用场景（10 字以内，没有就空字符串）
- "method"：核心方法或模型名（15 字以内）
- "data"：使用的数据集或数据来源（20 字以内，没有就空字符串）
- "metric"：评价指标（20 字以内，没有就空字符串）
- "finding"：主要结论（30 字以内）
- "numbers"：（可选）摘要里出现的**关键数值结论**，形如
  [{"metric": "准确率", "value": 92.3, "unit": "%"}]；没有就给空数组。

严格约束：**只依据给出的标题和摘要**，没有的信息留空，不要猜测、不要编造。
输出 JSON：{"items": [ {...}, ... ]}。只输出 JSON。"""

HYPOTHESIS_PROMPT = """你是科研团队里的「假设提出者」（AI4S 的"想"环节）。
根据给定的文献要点，先归纳共识与空白，再提出**可证伪**的科学假设。

⚠️ 假设必须**与当前可用的证据条件匹配**（这条最容易被忽略，也最致命）：
- 输入里的「可用证据」会写明这一轮有没有用户提供的实验数据、能调哪些计算工具；
- **没有实验数据时，不要提出需要新实验测量才能验证的假设**
  （例如"某方法 mIoU 更高"）。那样验证环节一定判"证据不支持"，整轮白做；
- 改为提出**能用文献报告数据或文献计量统计检验**的假设，例如：
  "近三年该方向的文献增长率高于该领域整体增长率"、
  "报告了数据集的文献，其被引中位数高于未报告的"、
  "该方向的方法集中在图卷积类，占比超过一半"；
- 如果某个性能类结论确实重要，就把它写成 `prediction` 里的**待验证条件**
  （"需要 X 数据集的对照实验"），并把 `confidence` 标 low，
  同时保证假设本身能由现有证据部分检验。
- **硬约束**：先看「文献字段可得性」里的计数。若"抽取到指标数值"的篇数 **少于 6**，
  **不要**提出"GNN 组 mIoU/OA/F1 高于某组"这类需要逐篇比较指标数值的假设 ——
  证据不足，验证环节一定判"不成立"。改提被引/占比/年份趋势类假设，
  这些用文献元数据当场就能检验。
  若"报告了数据集"的篇数足够，也可提"报告数据集的文献被引更高"这类假设。

约束：
1. 每条假设必须绑定依据：`basis` 里放文献编号（就是输入列表里的 i），
   **只能引用输入里真实存在的编号**。
2. 每条假设必须给否定判据 `falsify`：写出"观测到什么，就说明这条假设是错的"。
3. 变量要具体：`iv` 自变量、`dv` 因变量、`control` 控制变量，
   不要写"环境因素"这类笼统词。
4. `prediction` 要写预期结果，且必须**可测量**（含指标名或方向）。
5. 假设 1-3 条，互相独立；写不出可证伪的，就少写一条，不要凑。

输出 JSON：
{
  "consensus": ["当前文献的共识/主流做法，2-4 条"],
  "gaps": ["研究空白，2-3 条，要具体（缺什么条件下的什么研究）"],
  "hypotheses": [
    {"statement": "假设陈述（含条件与方向）",
     "basis": [1, 3],
     "mechanism": "机理（不确定就写'推测：...'）",
     "iv": "自变量", "dv": "因变量", "control": "控制变量",
     "falsify": "若观测到...则否定本假设",
     "prediction": "预期结果（含指标/方向）",
     "confidence": "high|medium|low"}
  ],
  "reasoning": "一到两句逻辑推导（从共识/空白到假设）"
}
只输出 JSON。"""

DESIGN_PROMPT = """你是科研团队里的「实验设计师」（AI4S 的"做"环节）。
把给定假设变成**能在本机工具条件下真跑一遍**的实验/仿真方案。

要求：
1. 因子（factors）2-4 个，每个因子给出 2-3 个水平（levels）；
   水平要写实际取值，不要只写"高/低"。
2. 给出主指标（metrics）与成功判据（success_criteria）、否定判据（failure_criteria），
   判据里要有具体阈值或统计量。
3. 给出可执行步骤（steps），每步一句话，且能对应到"采集/计算/比较"这类动作。
4. 如果这个题目**不适合做真实实验**（纯综述型），就设计"基于文献数据的复现实验"：
   用文献报告的数据集/指标做一致性核对与统计检验，并说明这是复现而非新实验。
5. `needed_calculations` 列出需要调用的计算：每项
   {"tool": "工具名", "args": {...}, "purpose": "为什么算这个"}。
   工具名只能从下面目录里选；不确定就少给，后面会自动补默认计算。

【可用科学计算工具】
__TOOL_CATALOG__

输出 JSON：
{"summary": "一句话方案概要",
 "factors": [{"name": "...", "levels": [...], "unit": "..."}],
 "design_type": "full-factorial|latin-hypercube|replication",
 "n_runs": 8,
 "power": {"effect_size": 0.5, "required_n": 64, "note": "..."},
 "metrics": ["..."],
 "success_criteria": ["..."],
 "failure_criteria": ["..."],
 "steps": ["..."],
 "needed_calculations": [{"tool": "...", "args": {}, "purpose": "..."}],
 "limitations": "一句话说明这个方案的局限"}
只输出 JSON。"""

VERIFY_PROMPT = """你是科研团队里的「审稿人」（AI4S 的"验"环节）。你看不到作者的辩解，
只看到产物与证据。请独立复核这一轮研究，判断能否写进最终报告。

复核三类：
1. 数值层：报告里的统计量能否由记录的参数重算出来？数字和工具返回是否一致？
2. 逻辑层：每条结论能否由证据推出？有没有"从相关推因果""从单篇推共识"
   "把外推当观测""把仿真当实测"？
3. 工程层：参数、数据、种子、工具版本是否记录齐全到"别人能重跑"？

结论只有三种，**必须给一个**：
- "pass"：可写进报告；
- "revise"：方向成立但需返工，`fixes` 给出**可执行**的返工项
  （"重新估计样本量"/"补做 Spearman 检验"/"删除无引用的第 3 节"）；
- "reject"：核心结论**在现有证据下不成立**，说明否定了哪条假设。

⚠️ 判据边界（务必分清，否则会误判）：
本项目做的是**复现型研究**——基于已有文献 + 可复现计算，**不做新的湿实验**。
所以：
* **不要**因为"没有新实验/没有实测数据"就判 reject。那属于研究的**局限**，
  应当写进 `open_questions` 一句"结论需 X 条件下的新实验验证"；
* reject 只用于：文献证据不支持该结论、引用与论断不符、逻辑断裂、
  或数值无法复算（自动检查已经报出不一致）；
* 如果假设本身属于"需后续实验验证"且报告/假设里**已明确标注**这一点
  （confidence=low、prediction 写了所需实验条件），这**不构成 reject**：
  判 pass 或 revise，并在 `open_questions` 里写清需要什么实验；
* **"证据不足所以做不了"和"该做的没做"要分开**：
  - 若"计算结果"里已经有对应的检验（工具名/参数/p 值都在），即使结论是"不显著"，
    也算这一项**已完成**，不能因为结果不支持假设而判 revise；
  - 若某条假设依赖的数值（例如文献报告的 mIoU）在文献里**根本没有报告**，
    属于文献计量研究的固有局限：写进 `open_questions` 即可，**不算缺陷**；
  - 只有当"计算结果"里明显缺少本可以做的检验（数据在产物里、却没跑）时，
    才把"补做 XX 检验"写进 `fixes`。
* **等价检验算已满足**：卡方 ≈ Fisher（小样本用 Fisher 更稳）、单样本二项 ≈
  两比例 z 的特例、Welch t ≈ Mann-Whitney（后者更稳健）。只要产物里**已经报了
  其中一种**（含统计量与 p 值），就不要因为"方法名不同"判 revise；
  Fisher 检验本来就不用期望频数，不能以"没给期望频数"为理由要求返工。
* 结论方向正确但证据量偏少、表述过强时，判 **revise**，并给出补齐证据的具体动作。

输出 JSON：
{"verdict": "pass|revise|reject",
 "checks": [{"name": "数值一致性|证据充分性|可复现性", "ok": true, "detail": "..."}],
 "fixes": ["具体返工项"],
 "open_questions": ["这一轮没解决、下一轮该接着问的问题"],
 "confidence": "high|medium|low",
 "summary": "两句话结论"}
只输出 JSON。"""

SYNTH_PROMPT = """你是学术综述撰写助手（AI4S 的"写"环节）。根据给定的文献列表（已抽取过要点，
并且已有假设与验证结论），写一篇中文 mini 综述。

硬性约束：
1. **只能依据给定文献**。不得引入列表之外的论文、作者、年份、数字。
2. 引用文献时用 [序号] 标注，序号必须对应给定列表的编号。不要编造序号。
3. 如果文献数量少或质量参差，要**如实说明**局限，不要硬凑成"研究充分"。
4. 不要逐篇复述摘要 —— 要按主题/方法归类，指出共识、分歧与空白。
5. 如果给了计算结果与验证结论，可以在正文里引用它们（写"经计算/经复算"），
   但**不要**把仿真值说成实验观测值。
6. 中文输出，学术但平实。

输出 JSON：
- "sections"：[{"heading": "章节标题", "body": "该节正文（可含 [序号] 引用）"}]
  章节标题请采用给定的章节列表；某节确实无内容可写时可以省略该节。
- "consensus"：2-4 条当前文献的共识或主流做法（每条一句话）
- "gaps"：2-3 条研究空白或不足（每条一句话）
- "caveat"：一句话说明本篇综述的局限（文献量、来源覆盖等）

⚠️ 每节正文控制在 300-700 字。**宁可写得精炼，也必须把 JSON 写完整**
（输出被截断等于全篇作废）。不要在任何字段里写换行以外的排版符号。

只输出 JSON。"""

# 整篇一次写不成时的退路：一次只写一节。
# 为什么需要它：一次要输出 5 节 + 共识 + 空白 + 局限，输出额度一旦被
# "思考内容"吃掉，JSON 就会在中间被砍断（见 SYNTH_TOKENS 的注释）。
# 拆成一节一次，单次输出小、被截断的概率低，而且**成功的节可以直接留下**，
# 失败的那节再单独重试，不必让整步归零。
SYNTH_SECTION_PROMPT = """你在为综述撰写**其中的一节**（AI4S 的"写"环节）。
用户会给你：研究目标、综述的完整章节列表、当前要写的章节标题、以及文献要点。

硬性约束：
1. **只写这一节**，不要写其它章节，也不要重复章节标题。
2. **只能依据给定文献**：不得引入列表之外的论文、作者、年份、数字；
   引用用 [序号]，序号必须来自给定列表。
3. 不要逐篇复述摘要 —— 按主题/方法归类，写成连贯的段落。
4. 如果给了共识/空白/验证结论，可以作为语境，但不要把仿真值说成实测值。
5. 正文 300-700 字，中文，学术但平实。写不下就压缩，**不要中途截断**。

输出 JSON：{"heading": "本章节标题", "body": "本节正文（可含 [序号] 引用）"}
只输出 JSON，不要解释文字或 markdown 代码块。"""

# 逐节写完之后补的"共识 / 空白 / 局限"（整篇那次没拿到时才用）。
# 输入是已经写好的章节摘要，所以输出很小，不会再次被截断。
SYNTH_SUMMARY_PROMPT = """你在为综述收尾（AI4S 的"写"环节）。已经写好的章节内容见输入。
请据此归纳，**不要引入新的文献或数字**。

输出 JSON：
- "consensus"：2-4 条当前文献的共识或主流做法（每条一句话）
- "gaps"：2-3 条研究空白或不足（每条一句话）
- "caveat"：一句话说明本篇综述的局限（文献量、来源覆盖等）

只输出 JSON，不要解释文字或 markdown 代码块。"""


# ═══════════════════════════════════════════════════════════════
#  模型调用（全部经 llm.py；测试可整体替换 _call_json）
# ═══════════════════════════════════════════════════════════════

def _call_json(prompt, user_content, max_tokens=1200, temperature=0, kind="fast",
               retries=2):
    """调一次模型并解析 JSON；失败返回 None（由调用方决定降级）。

    统一走 `llm.call_json`，好处是限流、重试、宽松解析只有一份实现。
    单测里直接替换本函数即可完全离线（见 tests/test_workflow.py）。
    """
    return llm.call_json(prompt, user_content, kind=kind, max_tokens=max_tokens,
                         temperature=temperature, retries=retries)


def _loads_lenient(raw):
    """兼容旧调用点：宽松 JSON 解析（实现已收进 llm.loads_json）。"""
    return llm.loads_json(raw)


def _role_json(role_key, task, payload, goal="", max_tokens=1600, temperature=0.0,
               prompt_override=None, with_skills=True, retries=2):
    """以某个角色的提示词跑一次 JSON 调用。

    两个坑（都是真机跑出来的）：

    1. **JSON 契约必须真的进提示词**。早期版本对"假设"这一步只用了角色提示词，
       把 `HYPOTHESIS_PROMPT`（写了完整 JSON schema）漏掉了 —— 模型于是老老实实
       写了一篇 4000 字的 Markdown 假设文档，解析必然失败、重试一次再失败，最后
       降级成"统计事实假设"。所以 `prompt_override`（=任务契约）永远要进提示词。
    2. **技能不能被契约覆盖掉**。design/verify/synth 原来只传 `prompt_override`，
       等于把角色的方法论技能整块丢了（Agent Skills 在工作流里形同虚设）。
       现在契约之后还会追加 1 个匹配到的技能（1200 字预算）。
    """
    import agents
    import skills as skills_mod

    spec = agents.role(role_key)
    base = prompt_override or agents.system_prompt(
        role_key, goal=goal, skill_limit=1, skill_budget=1600)
    if prompt_override and with_skills:
        block = skills_mod.skill_context(goal or task, role=role_key, limit=1,
                                         budget_chars=1200)
        if block:
            base = base + "\n\n" + block
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    data = _call_json(base, content, max_tokens=max_tokens,
                      temperature=temperature, kind=spec["model"],
                      retries=retries)
    return data if isinstance(data, dict) else None


def _tool_catalog_snippet(limit=12, chars=1800):
    """给提示词塞一小段 MCP 工具目录，**带上参数名与必填项**。

    为什么一定要带参数：实测不带时，模型给 `stats_ttest` 传了
    `{"x": ["high", "low"], ...}` 这种非法参数（把因子水平当成了数值组），
    5 个计算任务全部失败。带上 `x: 数值数组` 之后，非法参数明显减少。
    """
    try:
        tools = mcp_client.list_tools()
    except Exception:  # noqa: BLE001 - MCP 不可用不该影响规划
        return "（科学计算工具当前不可用）"

    def _param_brief(t):
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        parts = []
        for name, spec in list(props.items())[:6]:
            kind = spec.get("type") or "?"
            if kind == "array":
                item = (spec.get("items") or {}).get("type")
                kind = f"数组[{item}]" if item else "数组"
            mark = "*" if name in required else ""
            parts.append(f"{name}{mark}:{kind}")
        return "、".join(parts)

    lines, used = [], 0
    for t in tools[:limit]:
        name = t.get("name", "")
        desc = (t.get("description") or "").replace("\n", " ")[:70]
        line = f"- {name}：{desc}｜参数 {_param_brief(t)}（* 为必填）"
        if used + len(line) > chars:
            break
        lines.append(line)
        used += len(line) + 1
    if len(tools) > len(lines):
        lines.append(f"- ……共 {len(tools)} 个工具，其余见 list_science_tools")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  小工具
# ═══════════════════════════════════════════════════════════════

def _compact(papers, title_chars=110, abstract_chars=260):
    out = []
    for i, p in enumerate(papers, 1):
        out.append({
            "i": i,
            "title": (p.get("title") or "")[:title_chars],
            "abstract": (p.get("abstract") or "")[:abstract_chars],
            "year": p.get("year"),
            "venue": (p.get("journal") or p.get("venue") or "")[:40],
        })
    return out


def _num(value, default=None):
    """从任意东西里抠一个 float（"92.3%" -> 92.3）。抠不出就返回 default。"""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def _paper_years_cites(papers):
    xs, ys = [], []
    for p in papers:
        y = _num(p.get("year"))
        c = _num(p.get("citationCount"), 0.0)
        if y and c is not None:
            xs.append(y)
            ys.append(c)
    return xs, ys


def _bar(value, max_value, width=28):
    """纯文本条形图。不引第三方绘图库：报告要能直接贴进 Word/Markdown。"""
    if not max_value:
        return ""
    n = max(1, round(value / max_value * width))
    return "█" * n


# ═══════════════════════════════════════════════════════════════
#  ① 搜：检索与筛选
# ═══════════════════════════════════════════════════════════════

def step_search(cfg, emit, memory_hint=""):
    """多检索式 + 多源并发检索，跨式合并去重。

    为什么支持多检索式：单一检索式的召回率很低 —— 中文查"图神经网络 遥感图像"
    会漏掉只写"GNN"或只写"高光谱"的论文。规划器给 2-3 条互补检索式
    （中文式 / 英文式 / 近义术语式）分别检索后按 DOI/标题去重，
    实测能把新近文献的召回提高一倍以上，代价只是多一轮并发请求（各源都有缓存）。
    """
    queries = [q for q in (cfg.get("queries") or []) if isinstance(q, str) and q.strip()]
    base = (cfg.get("query") or cfg.get("topic") or "").strip()
    if base and base not in queries:
        queries.insert(0, base)
    queries = queries[:3]
    if not queries:
        raise RuntimeError("没有可用的检索式")

    source = cfg.get("source") or "all"
    limit = int(cfg.get("limit") or 20)
    emit(f"检索 {len(queries)} 条检索式（范围 {source}，每式取 {limit} 条）")

    merged, seen, warnings, sources = [], set(), [], set()
    errors = []
    dropped = 0
    for q in queries:
        payload = json.loads(providers.search_papers(q, limit, source, 0))
        if payload.get("error"):
            errors.append(payload.get("message") or payload.get("error"))
            continue
        for p in payload.get("papers") or []:
            key = (p.get("doi") or "").lower().strip() or \
                  (p.get("title") or "").lower().strip()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(p)
        warnings.extend(payload.get("warnings") or [])
        sources.update(payload.get("sources") or [])
        dropped += int(payload.get("filtered_out") or 0)

    if not merged:
        raise RuntimeError("；".join(errors[:3]) or f"未检索到与「{base}」相关的文献")

    # 合并后再按相关度/影响力重排一次：跨式合并会打乱各源内部顺序
    try:
        merged = providers._annotate_and_sort(merged)  # noqa: SLF001 - 同包内复用
    except Exception:  # noqa: BLE001 - 排序失败不影响结果可用
        pass

    emit(f"合并去重后 {len(merged)} 篇，来源 {len(sources)} 个")
    return {
        "papers": merged,
        "count": len(merged),
        "queries": queries,
        "sources": sorted(sources),
        "filtered_out": dropped,
        "warnings": list(dict.fromkeys(warnings))[:4],
        "errors": errors[:3],
    }


# ═══════════════════════════════════════════════════════════════
#  ② 读：信息抽取
# ═══════════════════════════════════════════════════════════════

def step_extract(cfg, papers, emit, stop=None):
    """分批抽取，**多批并发**。

    为什么要并发：实测 18 篇要 3 批、每批约 1.8~2.5s，串行跑就是 6-7s 纯等；
    而每批之间没有任何依赖（输入是文献切片，输出按 id 回填），天然可并行。
    并发度限制在 3：再高对本机与接口都没好处。中断语义保持"批次边界"：
    已发出的批次跑完照收，未发出的不再发。
    """
    if not papers:
        raise RuntimeError("上一步没有取到文献，无法抽取")
    batches = [papers[i:i + 8] for i in range(0, len(papers), 8)]
    items = {}
    emit(f"分 {len(batches)} 批抽取 {len(papers)} 篇的研究对象/方法/数据/指标/结论")

    def one_batch(bi, batch):
        """跑一批，返回 (编号, 解析结果, 错误)。异常不抛出，交给调用方记账。"""
        try:
            parsed = _call_json(EXTRACT_PROMPT,
                                json.dumps({"papers": _compact(batch)}, ensure_ascii=False),
                                max_tokens=2000, kind="fast")
            return bi, (parsed or {}).get("items") or [], None
        except Exception as e:  # noqa: BLE001 - 单批失败不该拖垮整步
            return bi, [], f"{type(e).__name__}: {e}"

    interrupted = False
    failures = []
    done_count = 0

    if len(batches) == 1:
        # 只有一批时不必开线程池（省掉调度开销，也让中断检查更直接）
        if stop and stop():
            emit("第 1/1 批开始前收到中断")
            return {"extracted": 0, "total": len(papers), "interrupted": True}
        bi, got, err = one_batch(1, batches[0])
        results = [(bi, got, err)]
    else:
        # 先按中断状态决定"发几批"：已经在跑的不打断，没发的就不发了
        to_run = []
        for bi, batch in enumerate(batches, 1):
            if stop and stop():
                interrupted = True
                break
            to_run.append((bi, batch))
        if not to_run:
            emit(f"第 1/{len(batches)} 批开始前收到中断")
            return {"extracted": 0, "total": len(papers), "interrupted": True}
        if interrupted:
            emit(f"收到中断，只跑前 {len(to_run)}/{len(batches)} 批")
        with ThreadPoolExecutor(max_workers=min(3, len(to_run))) as pool:
            futures = [pool.submit(one_batch, bi, b) for bi, b in to_run]
            results = []
            for fut in as_completed(futures):
                bi, got, err = fut.result()
                results.append((bi, got, err))
                done_count += 1
                emit(f"第 {done_count}/{len(to_run)} 批完成")

    for bi, got, err in results:
        if err:
            failures.append(f"第 {bi} 批: {err}")
            continue
        batch = batches[bi - 1]
        for it in got:
            if not isinstance(it, dict):
                continue
            try:
                # 批次内序号要换算成全局序号
                gi = int(it.get("i")) - 1
                if 0 <= gi < len(batch):
                    items[id(batch[gi])] = it
            except (TypeError, ValueError):
                continue
    if len(batches) == 1 and not failures:
        emit("抽取完成")

    filled = 0
    with_numbers = 0
    for p in papers:
        it = items.get(id(p)) or {}
        p["extract"] = {
            "object": str(it.get("object") or "").strip(),
            "method": str(it.get("method") or "").strip(),
            "data": str(it.get("data") or "").strip(),
            "metric": str(it.get("metric") or "").strip(),
            "finding": str(it.get("finding") or "").strip(),
        }
        nums = []
        for n in (it.get("numbers") or []):
            if isinstance(n, dict):
                v = _num(n.get("value"))
                if v is not None:
                    nums.append({"metric": str(n.get("metric") or "")[:20],
                                 "value": v, "unit": str(n.get("unit") or "")[:8]})
        if nums:
            p["extract"]["numbers"] = nums[:6]
            with_numbers += 1
        if any(v for k, v in p["extract"].items() if k != "numbers"):
            filled += 1

    if failures:
        emit(f"{len(failures)}/{len(batches)} 批抽取失败，其余批次照常使用")
    if filled == 0:
        emit("模型未能抽出结构化字段，后续结论将只依据标题与摘要")
    else:
        emit(f"{filled}/{len(papers)} 篇抽取到结构化信息"
             + (f"，{with_numbers} 篇带数值结论" if with_numbers else ""))
    return {"extracted": filled, "total": len(papers), "interrupted": interrupted,
            "with_numbers": with_numbers, "failures": failures[:3]}


# ═══════════════════════════════════════════════════════════════
#  ③ 读：文献计量与可视化（确定性，不依赖模型）
# ═══════════════════════════════════════════════════════════════

def step_analyze(cfg, papers, emit):
    if not papers:
        raise RuntimeError("没有文献可统计")

    years, cites, types, srcs, langs = {}, {}, {}, {}, {}
    for p in papers:
        y = str(p.get("year") or "未知")
        years[y] = years.get(y, 0) + 1
        c = p.get("citationCount") or 0
        bucket = "0" if c == 0 else ("1-9" if c < 10 else ("10-99" if c < 100 else "100+"))
        cites[bucket] = cites.get(bucket, 0) + 1
        types[p.get("docType") or "journal"] = types.get(p.get("docType") or "journal", 0) + 1
        srcs[p.get("sourceLabel") or p.get("source") or "未知"] = \
            srcs.get(p.get("sourceLabel") or p.get("source") or "未知", 0) + 1
        langs[p.get("language") or "未知"] = langs.get(p.get("language") or "未知", 0) + 1

    def mk(d):
        # 年份按时间排序，其余按数量倒序
        items = sorted(d.items(), key=lambda kv: kv[0]) if d is years else \
            sorted(d.items(), key=lambda kv: -kv[1])
        top = max(v for _, v in items) if items else 0
        return [{"label": k, "count": v, "bar": _bar(v, top)} for k, v in items]

    stats = {
        "total": len(papers),
        "years": mk(years),
        "citations": mk(cites),
        "docTypes": mk(types),
        "sources": mk(srcs),
        "languages": mk(langs),
        "withFulltext": sum(1 for p in papers if p.get("openAccessPdf")),
        "withDoi": sum(1 for p in papers if p.get("doi")),
        "medianCitations": sorted((p.get("citationCount") or 0) for p in papers)[len(papers) // 2],
    }

    # 证据缺口：让"读"这一步就能给出可被假设步骤引用的结构化缺口，
    # 而不是等综述写完才说"缺研究"。这三条都是**可核对的事实**，不是模型判断。
    gaps = []
    numeric_years = [int(y) for y in years if y.isdigit()]
    recent_cut = datetime.now().year - 2
    recent = sum(1 for y in numeric_years if y >= recent_cut)
    if numeric_years and recent == 0:
        gaps.append(f"没有 {recent_cut} 年之后的文献，最新进展缺失")
    if stats["withDoi"] < max(1, len(papers) // 2):
        gaps.append(f"仅 {stats['withDoi']}/{len(papers)} 篇有 DOI，著录与可复现性受限")
    if sum(1 for p in papers if p.get("openAccessPdf")) < max(1, len(papers) // 3):
        gaps.append("全文可得比例低，多数结论只能依据摘要")
    no_data = sum(1 for p in papers if not (p.get("extract") or {}).get("data"))
    if papers and no_data >= max(2, len(papers) * 2 // 3):
        gaps.append(f"{no_data}/{len(papers)} 篇未报告数据集，实验条件难以复现")
    stats["evidence_gaps"] = gaps[:4]
    stats["recent_ratio"] = round(recent / max(1, len(numeric_years)), 3) if numeric_years else 0.0

    emit(f"统计完成：{stats['total']} 篇，{len(years)} 个年份，"
         f"{stats['withFulltext']} 篇有全文直链，识别 {len(gaps)} 条证据缺口")
    return stats


# ═══════════════════════════════════════════════════════════════
#  ④ 想：科学假设生成与逻辑推导
# ═══════════════════════════════════════════════════════════════

def _fallback_hypotheses(papers, stats, gaps):
    """模型不可用时的确定性降级：从"方法分布 + 证据缺口"推出候选假设。

    为什么要有这条路径：假设是整个 AI4S 闭环的入口，它失败就等于闭环断路。
    这里的降级不是编造 —— 它只把**统计事实**改写成可检验的问句，
    并明确 `degraded: true`，让读者知道这不是模型推导出来的。
    """
    # 太泛的词不算"具体方法"：实测抽取结果里很多文献的 method 是
    # "人工智能"/"机器学习"/"深度学习"/"综述" —— 拿它写假设毫无信息量。
    too_generic = {"人工智能", "机器学习", "深度学习", "神经网络", "综述", "无", "—", "-"}
    methods = {}
    for p in papers:
        m = ((p.get("extract") or {}).get("method") or "").strip()
        if m and m not in too_generic and len(m) >= 2:
            methods[m] = methods.get(m, 0) + 1
    out = []
    if methods:
        top_method, top_n = max(methods.items(), key=lambda kv: kv[1])
        out.append({
            "statement": f"在同类研究条件下，以「{top_method}」为代表的方法相比其他方法"
                         f"在主要评价指标上表现更优（推测）",
            "basis": [1],
            "mechanism": "推测：该方法是当前文献中最常用的路线，可能存在归纳偏置优势",
            "iv": f"方法类型（{top_method} vs 其他）", "dv": "主要评价指标",
            "control": "数据集、训练规模、评价口径",
            "falsify": f"若对照实验显示其他方法在相同数据集上的指标不低于 {top_method}，则否定本假设",
            "prediction": "该方法组的主指标均值高于对照组（差异需显著）",
            "confidence": "low",
        })
    if gaps:
        out.append({
            "statement": f"针对「{gaps[0]}」这一缺口，补充相应数据/方法后结论的稳健性会提高（推测）",
            "basis": [1],
            "mechanism": "推测：证据缺口会放大复现偏差",
            "iv": "是否补齐缺口信息", "dv": "结论一致性（复算通过率）",
            "control": "文献集合、抽取口径",
            "falsify": "若补齐后复算通过率无变化，则否定本假设",
            "prediction": "补齐组的复算通过率高于未补齐组",
            "confidence": "low",
        })
    return out


def step_hypothesis(cfg, papers, emit, stats=None, feedback=None, iteration=0, goal=""):
    """从文献共识与空白推导可证伪假设（含降级路径）。"""
    if not papers:
        raise RuntimeError("没有文献，无法提出假设")
    emit(f"读 {len(papers)} 篇的抽取结果，归纳共识与空白（第 {iteration + 1} 轮）")

    # 只喂前 BRIEF_PAPERS 篇：模型出的假设要引用文献编号，编号必须在**完整列表**里
    # 有效 —— 所以这里只用编号 1..N（与报告里的编号一致，前 N 篇）。
    brief = []
    for i, p in enumerate(papers[:BRIEF_PAPERS], 1):
        ex = p.get("extract") or {}
        brief.append({
            "i": i, "title": (p.get("title") or "")[:90], "year": p.get("year"),
            "method": ex.get("method", ""), "data": ex.get("data", ""),
            "metric": ex.get("metric", ""), "finding": ex.get("finding", ""),
            "numbers": ex.get("numbers") or [],
        })

    gaps = list((stats or {}).get("evidence_gaps") or [])
    has_dataset = bool((cfg.get("dataset") or "").strip())
    try:
        tool_names = [t["name"] for t in mcp_client.list_tools()][:20]
    except Exception:  # noqa: BLE001
        tool_names = []
    payload = {
        "研究目标": cfg.get("topic") or goal,
        # 让模型知道"这轮能拿什么证据说话"：没有实验数据时提出的性能假设
        # 在验证环节必然被判"证据不支持"，整轮就白跑了。
        "可用证据": {
            "用户实验数据": "有（" + (cfg.get("dataset_kind") or "table") + "）" if has_dataset else "无",
            "文献元数据": "有（标题/年份/被引/期刊/DOI/全文链接）",
            # 下面这几个计数是**真数字**：模型原来只看到"有部分数值"就敢提
            # "GNN 组 mIoU 更高"，可这一批文献实际抽到数值的只有 0-3 篇，
            # 于是验证环节必然判"证据不支持"。给出计数，模型才会选能测的假设。
            "文献字段可得性（这决定你能不能提某类假设）": {
                "总数": len(papers),
                "报告了数据集": f"{sum(1 for x in papers if ((x.get('extract') or {}).get('data') or '').strip())}/{len(papers)}",
                "抽取到指标数值（mIoU/OA/F1 等）":
                    f"{sum(1 for x in papers if ((x.get('extract') or {}).get('numbers')))}/{len(papers)}",
                "有全文直链": f"{sum(1 for x in papers if x.get('openAccessPdf'))}/{len(papers)}",
                "有被引次数": f"{sum(1 for x in papers if (x.get('citationCount') or 0) > 0)}/{len(papers)}",
            },
            "可调计算工具": tool_names,
            "说明": "没有用户实验数据时，假设必须能用文献计量或文献报告数值检验；"
                    "需要新实验才能验证的结论，只能写进 prediction 的待验证条件。",
        },
        "重点关注": cfg.get("focus") or "",
        "研究类型": cfg.get("research_type") or "review",
        "常用指标": cfg.get("metrics") or [],
        "文献计量事实": {
            "总数": (stats or {}).get("total", len(papers)),
            "近年占比": (stats or {}).get("recent_ratio"),
            "有 DOI": (stats or {}).get("withDoi"),
            "全文可得": (stats or {}).get("withFulltext"),
            "已识别证据缺口": gaps,
        },
        "文献要点": brief,
    }
    if feedback:
        payload["上一轮验证反馈"] = {
            "verdict": feedback.get("verdict"),
            "fixes": feedback.get("fixes") or [],
            "open_questions": feedback.get("open_questions") or [],
            "checks": [c.get("detail") for c in (feedback.get("checks") or [])
                       if isinstance(c, dict) and not c.get("ok")],
        }
        emit("带着上一轮审稿意见重新提假设：" + "；".join(
            (feedback.get("fixes") or [])[:2]))

    parsed = _role_json("hypothesizer",
                        "请归纳共识与空白，并提出 1-3 条可证伪的科学假设。",
                        payload, goal=cfg.get("topic") or goal, max_tokens=2800,
                        temperature=0.2,
                        # 必须把 JSON 契约真的传下去：早期版本漏了这个参数，
                        # HYPOTHESIS_PROMPT 里那套 schema 等于死代码，模型于是写
                        # 了一篇 4000 字的 Markdown（实测），解析必然失败 → 降级。
                        prompt_override=HYPOTHESIS_PROMPT)

    def slist(seq, cap, width=120):
        out = []
        for x in (seq or []):
            if isinstance(x, str) and x.strip():
                out.append(x.strip()[:width])
            if len(out) >= cap:
                break
        return out

    hypotheses = []
    for h in (parsed or {}).get("hypotheses") or []:
        if not isinstance(h, dict) or not str(h.get("statement") or "").strip():
            continue
        basis = []
        for b in (h.get("basis") or [])[:5]:
            bi = _num(b)
            if bi is not None and 1 <= int(bi) <= len(papers):
                basis.append(int(bi))
        hypotheses.append({
            "id": f"H{len(hypotheses) + 1}",
            "statement": str(h["statement"]).strip()[:300],
            "basis": basis,
            "mechanism": str(h.get("mechanism") or "").strip()[:200],
            "iv": str(h.get("iv") or "").strip()[:60],
            "dv": str(h.get("dv") or "").strip()[:60],
            "control": str(h.get("control") or "").strip()[:80],
            "falsify": str(h.get("falsify") or "").strip()[:200],
            "prediction": str(h.get("prediction") or "").strip()[:200],
            "confidence": str(h.get("confidence") or "low").strip().lower()[:10],
        })
        if len(hypotheses) >= 3:
            break

    degraded = True
    consensus = slist((parsed or {}).get("consensus"), 4)
    gaps_out = slist((parsed or {}).get("gaps"), 3)
    reasoning = str((parsed or {}).get("reasoning") or "").strip()[:400]

    if not hypotheses:
        hypotheses = _fallback_hypotheses(papers, stats or {}, gaps_out or gaps)
        # 降级路径同样要有 id：报告/前端/验证都按 id 指代假设，缺了会显示成 None
        for i, h in enumerate(hypotheses, 1):
            h.setdefault("id", f"H{i}")
        degraded = bool(hypotheses)     # 有降级假设仍算"降级"
        if not gaps_out:
            gaps_out = (gaps or [])[:3]
        if not consensus:
            consensus = ["（模型未返回共识归纳，以下结论仅基于文献计量事实）"]
        emit("模型未给出可用假设，已用文献统计事实生成降级假设（degraded）")
    else:
        degraded = False
        if not gaps_out:
            gaps_out = (gaps or [])[:3]

    emit(f"产出 {len(hypotheses)} 条假设" +
         ("（降级）" if degraded else "") +
         ("，其中 " + str(sum(1 for h in hypotheses if h["basis"])) + " 条绑定文献依据"))

    return {
        "hypotheses": hypotheses,
        "consensus": consensus,
        "gaps": gaps_out,
        "reasoning": reasoning,
        "degraded": degraded,
        "iteration": iteration,
        "feedback_applied": bool(feedback),
    }


# ═══════════════════════════════════════════════════════════════
#  ⑤ 做：实验/仿真方案设计
# ═══════════════════════════════════════════════════════════════

def _mcp_call(tool_name, args, emit=None, purpose=""):
    """经 MCP 调一个科学计算工具，返回 `(ok, data, meta)`。

    * `data`：工具真正算出来的结果（dict）
    * `meta`：`{"tool","repro","elapsed_ms","error"}`，其中 `repro` 是
      "工具名 + 归一化参数"，验证步骤靠它把这次计算**重跑一遍**。

    统一在这里落地，是为了所有计算都带同一份复现凭据。
    """
    res = mcp_client.call_tool(tool_name, args or {})
    ok = bool(res.get("ok"))
    env = res.get("result") if isinstance(res.get("result"), dict) else {}
    data = env.get("result")
    meta = {
        "tool": env.get("tool") or tool_name,
        "repro": env.get("repro") or {"tool": tool_name, "args": args or {}},
        "elapsed_ms": env.get("elapsed_ms"),
        "error": env.get("error") or res.get("error"),
    }
    if not isinstance(data, dict):
        data = {"raw": data}
    if emit:
        mark = "✓" if ok else "✗"
        brief = "、".join(list(data.keys())[:4]) if isinstance(data, dict) else ""
        emit(f"{mark} {tool_name}" + (f"（{purpose}）" if purpose else "")
             + (f" → {brief}" if brief else "")
             + ("" if ok else f" 失败：{meta.get('error')}"))
    return ok, data, meta


def step_experiment(cfg, papers, hypothesis, emit, iteration=0, goal=""):
    """把假设变成可执行方案，并用 MCP 真的生成设计矩阵与样本量。"""
    if not hypothesis or not hypothesis.get("hypotheses"):
        raise RuntimeError("上一步没有产出假设，无法设计实验")

    emit(f"为 {len(hypothesis['hypotheses'])} 条假设设计方案")
    methods = []
    for p in papers[:min(12, BRIEF_PAPERS)]:
        ex = p.get("extract") or {}
        if ex.get("method") or ex.get("metric"):
            methods.append({"method": ex.get("method", ""), "metric": ex.get("metric", ""),
                            "data": ex.get("data", "")})

    payload = {
        "研究目标": cfg.get("topic") or goal,
        "假设": hypothesis.get("hypotheses"),
        "候选方法与被试指标": methods,
        "已有数据集": (cfg.get("dataset_kind") or "无"),
        "研究类型": cfg.get("research_type") or "review",
        "迭代轮次": iteration + 1,
    }
    # 1800 在真机上会被截断（方案里的因子/步骤/判据都很长），
    # 截断 -> JSON 不完整 -> call_json 重试一次 -> 白等 30-60s。
    parsed = _role_json("designer", "请给出可执行的实验或复现方案。", payload,
                        goal=cfg.get("topic") or goal, max_tokens=2400,
                        temperature=0.2,
                        prompt_override=DESIGN_PROMPT.replace(
                            "__TOOL_CATALOG__", _tool_catalog_snippet()))

    factors = []
    for f in (parsed or {}).get("factors") or []:
        if not isinstance(f, dict) or not str(f.get("name") or "").strip():
            continue
        levels = [lv for lv in (f.get("levels") or []) if isinstance(lv, (int, float, str))]
        if not levels:
            continue
        factors.append({"name": str(f["name"]).strip()[:40],
                        "levels": levels[:3],
                        "unit": str(f.get("unit") or "").strip()[:12]})
        if len(factors) >= 4:
            break
    if not factors:
        # 降级方案：用文献里出现的方法/数据集作为单因子，保证方案可执行
        factors = [{"name": "方法路线", "levels": ["文献主流方法", "对照方法"], "unit": ""}]
        emit("模型未给出实验因子，降级为单因子对照设计")

    # ── 真的生成设计矩阵（不靠模型）：全因子 / 拉丁超立方 ──
    design = {"type": "single", "runs": [], "note": "只有 1 个因子，直接对照"}
    design_type = str((parsed or {}).get("design_type") or "").strip().lower()
    if len(factors) >= 2 and len(factors) <= 3 and all(len(f["levels"]) == 2 for f in factors):
        ok, data, _meta = _mcp_call(
            "design_factorial",
            {"factors": [f["name"] for f in factors], "levels": 2},
            emit, "全因子设计矩阵")
        if ok:
            design = dict(data)
            design["type"] = "full-factorial"
    elif len(factors) >= 2:
        ok, data, _meta = _mcp_call(
            "design_lhs",
            {"n_factors": len(factors), "n_runs": max(8, 4 * len(factors)), "seed": 42},
            emit, "拉丁超立方设计")
        if ok:
            design = dict(data)
            design["type"] = "latin-hypercube"

    # ── 样本量：用真实功效分析，而不是模型随口说的数字 ──
    effect_size = 0.5
    if isinstance((parsed or {}).get("power"), dict):
        effect_size = _num((parsed or {})["power"].get("effect_size"), 0.5) or 0.5
    effect_size = min(max(abs(effect_size), 0.1), 2.0)
    ok_p, power, _meta = _mcp_call("design_power",
                                   {"effect_size": effect_size, "alpha": 0.05, "power": 0.8},
                                   emit, "样本量估算")
    if not ok_p:
        power = {"effect_size": effect_size, "n_per_group": None,
                 "note": "功效分析工具不可用，样本量需人工确定"}

    steps = [s for s in ((parsed or {}).get("steps") or [])
             if isinstance(s, str) and s.strip()][:8]
    if not steps:
        steps = ["按设计矩阵准备各组样本/数据",
                 "执行测量或调用计算工具得到指标",
                 "用统计检验比较组间差异并报告效应量",
                 "复核关键数值能否重算"]

    n_runs = int(design.get("n_runs") or (parsed or {}).get("n_runs") or len(design.get("runs") or [])
                 or (power.get("n_per_group") or 0) * 2 or 0)

    plan = {
        "summary": str((parsed or {}).get("summary") or
                       "按设计矩阵做对照实验，用统计检验判定假设是否成立")[:200],
        "design_type": design.get("type") or design_type or "single",
        "factors": factors,
        "design": design,
        "power": power,
        "n_runs": n_runs,
        "metrics": [str(m)[:40] for m in ((parsed or {}).get("metrics") or [])][:6]
                   or (cfg.get("metrics") or [])[:6],
        "success_criteria": [str(s)[:120] for s in
                             ((parsed or {}).get("success_criteria") or [])][:4],
        "failure_criteria": [str(s)[:120] for s in
                             ((parsed or {}).get("failure_criteria") or [])][:4],
        "steps": steps,
        "limitations": str((parsed or {}).get("limitations") or "")[:200],
        "degraded": not bool(parsed),
        "iteration": iteration,
    }
    needed = []
    for c in ((parsed or {}).get("needed_calculations") or [])[:MAX_MODEL_TASKS]:
        if isinstance(c, dict) and str(c.get("tool") or "").strip():
            needed.append({"tool": str(c["tool"]).strip(),
                           "args": c.get("args") if isinstance(c.get("args"), dict) else {},
                           "purpose": str(c.get("purpose") or "")[:60]})
    plan["needed_calculations"] = needed

    emit(f"方案就绪：{len(factors)} 个因子，设计类型 {plan['design_type']}，"
         f"每组样本量 {power.get('n_per_group')}，指标 {len(plan['metrics'])} 个")
    return plan


# ═══════════════════════════════════════════════════════════════
#  ⑥ 算：科学计算工具自主调用
# ═══════════════════════════════════════════════════════════════

def _default_literature_tasks(papers):
    """确定性计算任务：全部基于**真实文献元数据**，不依赖模型。

    这些是"算"环节的保底产出：即便模型一个计算任务都没提，
    读者也能看到真实的统计量与检验结果（而不是"研究表明"这种空话）。
    """
    xs, ys = _paper_years_cites(papers)
    tasks = []
    cites = [float(p.get("citationCount") or 0) for p in papers]
    if len(cites) >= 3:
        tasks.append({
            "tool": "stats_describe", "args": {"values": cites},
            "purpose": "被引次数分布（均值/中位数/离散度）",
        })
    if len(xs) >= 4 and len(set(xs)) >= 3:
        tasks.append({
            "tool": "stats_correlation",
            "args": {"x": xs, "y": ys, "method": "spearman"},
            "purpose": "发表年份与被引的秩相关（新旧文献影响力差异）",
        })
        tasks.append({
            "tool": "stats_regression", "args": {"x": xs, "y": ys},
            "purpose": "被引随年份的线性趋势",
        })
        mid = sorted(xs)[len(xs) // 2]
        new = [y for x, y in zip(xs, ys) if x >= mid]
        old = [y for x, y in zip(xs, ys) if x < mid]
        if len(new) >= 2 and len(old) >= 2:
            tasks.append({
                "tool": "stats_mannwhitney", "args": {"x": new, "y": old},
                "purpose": f"近半（≥{int(mid)} 年）与早期文献被引差异的非参数检验",
            })
    return tasks


def _dataset_tasks(cfg):
    """用户给了数据集（CSV / 谱图数据 / SMILES 列表）时，按种类生成计算任务。"""
    text = (cfg.get("dataset") or "").strip()
    if not text:
        return []
    kind = (cfg.get("dataset_kind") or "table").strip().lower()
    if kind in ("smiles", "molecule"):
        items = [s.strip() for s in re.split(r"[\s,;]+", text) if s.strip()]
        return [{"tool": "molecule_batch", "args": {"smiles": items[:20]},
                 "purpose": "批量算分子式与分子量（Lipinski 规则检查）"}]
    if kind in ("spectrum", "spectra", "curve"):
        return [{"tool": "table_parse", "args": {"text": text},
                 "purpose": "解析实验数据表（两列：x 强度 / y 响应）"}]
    return [{"tool": "table_parse", "args": {"text": text},
             "purpose": "解析用户提供的数据表"}]


def _dataset_followup(parsed_table, kind):
    """数据集解析成功后，再按数据类型追加一次真正的分析。"""
    cols = parsed_table.get("columns") or []
    if len(cols) < 2:
        return []
    numeric = [c for c in cols if (parsed_table.get("numeric") or {}).get(c)]
    if len(numeric) < 2:
        return []
    y_col = numeric[1]
    values = []
    for row in parsed_table.get("rows") or []:
        idx = cols.index(y_col)
        if idx < len(row) and isinstance(row[idx], (int, float)):
            values.append(float(row[idx]))
    if not values:
        return []
    if kind in ("spectrum", "spectra", "curve"):
        return [{"tool": "spectra_summary", "args": {"y": values},
                 "purpose": f"谱图/曲线解析：峰位、信噪比、主峰占比（列 {y_col}）"}]
    xs = []
    x_col = numeric[0]
    for row in parsed_table.get("rows") or []:
        idx = cols.index(x_col)
        if idx < len(row) and isinstance(row[idx], (int, float)):
            xs.append(float(row[idx]))
    if len(xs) != len(values) or len(xs) < 5:
        return [{"tool": "stats_describe", "args": {"values": values},
                 "purpose": f"列 {y_col} 的描述统计"}]
    return [{"tool": "curves_fit",
             "args": {"x": xs, "y": values, "model": "linear"},
             "purpose": f"{x_col} → {y_col} 的线性拟合（R²/RMSE）"}]


# ═══════════════════════════════════════════════════════════════
#  假设 -> 可执行检验（"假设—实验—算"之间的数据绑定层）
# ═══════════════════════════════════════════════════════════════
#
# 真机踩到的坑（很重要）：
# 设计步骤会写出"对 H1 做 Mann-Whitney 检验（报告数据集组 vs 未报告组）"，
# 但它**拿不到数据本身**，于是参数是空数组 `{"x": [], "y": []}` ——
# 5 个计算任务全部失败，审稿人随即判 revise："关键假设的统计检验结果缺失"。
#
# 这说明"设计"和"计算"之间缺了一层**数据绑定**：设计说"检验什么对比"，
# 计算负责"把对比变成真实数组"。下面是这一层的实现 —— 它只做能用文献字段
# 直接构造出来的检验，并且：
#   * 每组至少 3 个样本才做（统计上合理，也避免拿 1 个样本算出"显著"）；
#   * 每条任务都写明它为哪条假设服务（purpose 里带假设编号）；
#   * 结果标注 source="auto-derived"，报告里与"模型提议"区分开。

CITE_TERMS = ("被引", "引用", "citation")
HEAT_TERMS = ("GNN", "GCN", "GAT", "GRAPH", "图神经", "图卷积", "图结构")
RATIO_TERMS = ("占比", "比例", "报告比例")
MAJORITY_TERMS = ("超过半数", "高于 0.5", "高于0.5", "多数", "过半")
DATA_TERMS = ("数据集", "数据")
YEAR_TERMS = ("年份", "增长", "趋势", "近年")
OA_TERMS = ("全文", "开放获取", "OA", "全文可得")
# 文献里最常被拿来比较的指标名（用于从抽取结果里取数值）
METRIC_TERMS = ("MIOU", "IOU", "OA", "F1", "ACC", "ACCURACY", "KAPPA", "PRECISION",
                "RECALL", "RMSE", "MAE", "PSNR", "精度", "准确率")


def _paper_cites(p):
    try:
        return float(p.get("citationCount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _paper_method(p):
    return ((p.get("extract") or {}).get("method") or "").upper()


def _paper_has_data(p):
    return bool(((p.get("extract") or {}).get("data") or "").strip())


def _paper_is_gnn(p):
    m = _paper_method(p)
    return any(k in m for k in HEAT_TERMS)


def _paper_oa(p):
    return bool(p.get("openAccessPdf"))


def _paper_metric_values(p):
    """从抽取出的 numbers 里取指标数值（只保留 0-100 的百分比类量）。

    为什么要卡 0-100：抽取模型偶尔会把被引数、样本量当成"数值结论"塞进
    numbers（实测出现过 value=511），不做范围过滤会把 t 检验算成垃圾。
    """
    out = []
    for n in ((p.get("extract") or {}).get("numbers") or []):
        if not isinstance(n, dict):
            continue
        metric = str(n.get("metric") or "").upper()
        if metric and not any(k in metric for k in METRIC_TERMS):
            continue
        val = _num(n.get("value"))
        if val is None or not (0.0 <= val <= 100.0):
            continue
        out.append(float(val))
    return out


def _paper_reports_numbers(p):
    return bool(_paper_metric_values(p))


def _hit(text, terms):
    t = (text or "").upper()
    return any(k.upper() in t for k in terms)


def _auto_hypothesis_tasks(papers, hypotheses, max_tasks=4):
    """把假设映射成**参数齐全**的检验任务。返回 [{tool,args,purpose}]。

    规则按"具体优先"排列。每条假设只命中第一条适用的规则，避免同一件事
    被推导成三四个重复检验（实测过：不加约束时同一假设生成了 5 个任务）。
    """
    if not papers or not hypotheses:
        return []

    gnn = [p for p in papers if _paper_is_gnn(p)]
    non_gnn = [p for p in papers if not _paper_is_gnn(p)]
    with_data = [p for p in papers if _paper_has_data(p)]
    without_data = [p for p in papers if not _paper_has_data(p)]
    oa = [p for p in papers if _paper_oa(p)]
    non_oa = [p for p in papers if not _paper_oa(p)]

    def cites(group):
        return [_paper_cites(p) for p in group]

    def metrics(group):
        out = []
        for p in group:
            out.extend(_paper_metric_values(p))
        return out

    gnn_metrics, non_gnn_metrics = metrics(gnn), metrics(non_gnn)
    years = [_num(p.get("year")) for p in papers]
    years = [y for y in years if y]
    year_cites = [c for y, c in ((_num(p.get("year")), _paper_cites(p)) for p in papers) if y]

    tasks, used = [], set()

    def add(tool, args, purpose):
        key = (tool, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in used:
            return
        used.add(key)
        tasks.append({"tool": tool, "args": args, "purpose": purpose})

    def add_group_test(hid, text, xa, xb, what):
        """两组指标数值比较：样本够就做 t 检验，两组不等长或偏态时用 U 检验。"""
        if len(xa) >= 3 and len(xb) >= 3:
            add("stats_ttest", {"x": xa, "y": xb},
                f"{hid}：{what} 的均值差异（Welch t 检验）")
            add("stats_mannwhitney", {"x": xa, "y": xb},
                f"{hid}：{what} 的差异（Mann-Whitney U，t 检验的稳健性对照）")
            return True
        return False

    for h in hypotheses[:3]:
        hid = h.get("id") or "H?"
        text = " ".join(str(h.get(k) or "") for k in
                        ("statement", "falsify", "prediction", "iv", "dv"))

        # ① 指标数值对比（mIoU/OA/F1… 且是 GNN vs 非 GNN）
        if _hit(text, METRIC_TERMS) and _hit(text, HEAT_TERMS):
            if add_group_test(hid, text, gnn_metrics, non_gnn_metrics,
                              "GNN vs 非 GNN 文献报告的指标数值"):
                continue

        # ② 两组"报告了指标数值"的比例差异
        # 同时给两比例 z 检验与 Fisher 精确检验：小样本（n<40 或某格 <5）时
        # 精确检验才可信，审稿人一般也要求两个都报（实测就被要求过）。
        if _hit(text, RATIO_TERMS) and _hit(text, HEAT_TERMS) and not _hit(text, MAJORITY_TERMS):
            a = sum(1 for p in gnn if _paper_reports_numbers(p))
            b = len(gnn) - a
            c = sum(1 for p in non_gnn if _paper_reports_numbers(p))
            d = len(non_gnn) - c
            if (a + b) >= 3 and (c + d) >= 3:
                add("stats_proportion_two",
                    {"s1": a, "n1": a + b, "s2": c, "n2": c + d},
                    f"{hid}：GNN（{a}/{a + b}）与非 GNN（{c}/{c + d}）"
                    f"报告指标数值的比例差异（两比例 z 检验）")
                add("stats_fisher", {"a": a, "b": b, "c": c, "d": d},
                    f"{hid}：同上，改用 Fisher 精确检验（小样本稳健）")
                continue

        # ③ 单一群体的占比是否高于 0.5
        if _hit(text, MAJORITY_TERMS) and _hit(text, HEAT_TERMS):
            add("stats_proportion",
                {"successes": len(gnn), "n": len(papers), "p0": 0.5},
                f"{hid}：GNN 类文献占比是否显著高于 0.5（精确二项检验）")
            continue

        # ③b 年份切分：假设里出现具体年份（"2021 年前后"）时，按该年切两组
        ym = re.search(r"(20\d{2})\s*年?\s*(?:前|之前|以前|后|之后|以后|起|以来)", text)
        if ym and _hit(text, CITE_TERMS + METRIC_TERMS):
            cut = float(ym.group(1))
            before = [p for p in papers if (_num(p.get("year")) or 0) < cut]
            after = [p for p in papers if (_num(p.get("year")) or 0) >= cut]
            vals_before, vals_after = metrics(before), metrics(after)
            if _hit(text, METRIC_TERMS) and len(vals_before) >= 3 and len(vals_after) >= 3:
                add("stats_mannwhitney", {"x": vals_after, "y": vals_before},
                    f"{hid}：{int(cut)} 年前后文献报告指标数值的差异（Mann-Whitney U）")
                continue
            if len(before) >= 3 and len(after) >= 3:
                add("stats_mannwhitney", {"x": cites(after), "y": cites(before)},
                    f"{hid}：{int(cut)} 年前后文献的被引差异（Mann-Whitney U）")
                continue

        # ③c 方法关键词分组：假设里点名的技术词（如"超像素""自适应"）在
        # extract.method 里能匹配到，就按"含该词 vs 不含"分组比较
        if _hit(text, CITE_TERMS + METRIC_TERMS):
            for kw in ("超像素", "自适应", "注意力", "Transformer", "CNN", "U-Net",
                       "多尺度", "语义", "弱监督", "半监督"):
                if kw.upper() not in text.upper():
                    continue
                hit = [p for p in papers if kw.upper() in _paper_method(p)]
                miss = [p for p in papers if kw.upper() not in _paper_method(p)]
                if len(hit) >= 3 and len(miss) >= 3:
                    add("stats_mannwhitney", {"x": cites(hit), "y": cites(miss)},
                        f"{hid}：方法含「{kw}」vs 不含的被引差异（Mann-Whitney U）")
                    break
            else:
                pass
            if tasks and tasks[-1]["purpose"].startswith(hid):
                continue

        # ④ 被引对比：数据集 / 开放获取 / GNN 三种分组
        if _hit(text, CITE_TERMS):
            if _hit(text, DATA_TERMS) and len(with_data) >= 3 and len(without_data) >= 3:
                add("stats_mannwhitney",
                    {"x": cites(with_data), "y": cites(without_data)},
                    f"{hid}：报告数据集 vs 未报告数据集的被引差异（Mann-Whitney U）")
                continue
            if _hit(text, OA_TERMS) and len(oa) >= 3 and len(non_oa) >= 3:
                add("stats_mannwhitney", {"x": cites(oa), "y": cites(non_oa)},
                    f"{hid}：全文可得 vs 仅摘要的被引差异（Mann-Whitney U）")
                continue
            if _hit(text, HEAT_TERMS) and len(gnn) >= 3 and len(non_gnn) >= 3:
                add("stats_mannwhitney", {"x": cites(gnn), "y": cites(non_gnn)},
                    f"{hid}：GNN vs 非 GNN 文献的被引差异（Mann-Whitney U）")
                continue

        # ⑤ 年份/趋势
        if _hit(text, YEAR_TERMS) and len(years) >= 4:
            add("stats_correlation", {"x": years, "y": year_cites, "method": "spearman"},
                f"{hid}：发表年份与被引的秩相关（趋势类假设）")
            continue

    return tasks[:max_tasks]


def _task_args_usable(args):
    """粗校验模型给的参数：数组不能为空、不能夹着非数值。

    模型经常给 `{"x": [], "y": []}`（它拿不到数据）或
    `{"x": ["high","low"]}`（把因子水平当成了数值组）。这类任务别发出去，
    直接记一条可读的失败说明，比让工具报"参数里含非法数值"更清楚。
    """
    if not isinstance(args, dict) or not args:
        return False, "参数为空"
    for k, v in args.items():
        if isinstance(v, list):
            if not v:
                return False, f"参数 {k} 是空数组"
            nums = [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if not nums:
                return False, f"参数 {k} 里没有数值"
        elif isinstance(v, str):
            # 模型会拿"人工编码表"这种占位文字去调 table_parse（实测），
            # 空串/纯占位直接当不可用，交给自动推导层用真实文献字段构造数据。
            t = v.strip()
            if not t:
                return False, f"参数 {k} 是空字符串"
            if len(t) < 8 and not t.replace("-", "").replace(",", "").isdigit():
                return False, f"参数 {k} 太短（疑似占位）"
    return True, ""


def step_compute(cfg, papers, hypothesis, experiment, emit, iteration=0):
    """自主调用科学计算工具，产出**真数值 + 复现凭据**。

    任务来源三路，按优先级：
      1. 用户提供的数据集（最可信：真实实验数据）
      2. 实验方案里 `needed_calculations` 指定的计算（模型自主决定，白名单校验）
      3. 基于文献元数据的确定性统计（保底，永远能跑）
    """
    emit(f"规划计算任务（第 {iteration + 1} 轮）")
    computations, failures = [], []

    def _record(tool, purpose, data, meta, source):
        computations.append({
            "tool": meta.get("tool") or tool,
            "purpose": purpose,
            "source": source,
            "args": (meta.get("repro") or {}).get("args") or {},
            "result": data,
            "repro": meta.get("repro"),
        })

    # ── ① 数据集（用户给的实验数据优先） ──────────────────
    if (cfg.get("dataset") or "").strip():
        kind = (cfg.get("dataset_kind") or "table").lower()
        if kind in ("smiles", "molecule"):
            # SMILES 列表不是"表"，直接走分子工具，别先做表格解析
            for task in _dataset_tasks(cfg):
                ok_m, data_m, meta_m = _mcp_call(task["tool"], task["args"], emit,
                                                 task["purpose"])
                if ok_m:
                    _record(task["tool"], task["purpose"], data_m, meta_m, "user-dataset")
                else:
                    failures.append(f"{task['tool']}：{meta_m.get('error')}")
        else:
            ok, data, meta = _mcp_call("table_parse", {"text": cfg["dataset"]}, emit,
                                       "用户数据集")
            if ok:
                _record("table_parse", "解析用户提供的数据集", data, meta, "user-dataset")
                for extra in _dataset_followup(data, kind):
                    ok2, d2, m2 = _mcp_call(extra["tool"], extra["args"], emit,
                                            extra["purpose"])
                    if ok2:
                        _record(extra["tool"], extra["purpose"], d2, m2, "user-dataset")
                    else:
                        failures.append(f"{extra['tool']}：{m2.get('error')}")
            else:
                failures.append(f"数据集解析失败：{meta.get('error')}")

    # ── ② 实验方案要求的计算（白名单校验） ─────────────────
    try:
        # 只允许科学计算工具：模型偶尔会在"算"里提议 search_papers，
        # 那属于"搜"这一步的职责，在这里重跑既慢又会让证据链混乱。
        catalog = {t["name"] for t in mcp_client.list_tools()
                   if not t["name"].startswith(("search_", "get_"))}
    except Exception:  # noqa: BLE001
        catalog = set()
    needed = list((experiment or {}).get("needed_calculations") or [])
    for task in needed[:MAX_MODEL_TASKS]:
        name = task.get("tool")
        if name not in catalog:
            failures.append(f"跳过未注册工具 {name}")
            continue
        usable, why = _task_args_usable(task.get("args"))
        if not usable:
            # 模型只能声明"要检验什么"，数据得由下面的数据绑定层来填
            failures.append(f"{name}：{why}（已跳过，改由自动推导补齐）")
            continue
        ok, data, meta = _mcp_call(name, task.get("args") or {}, emit,
                                   task.get("purpose") or "")
        if ok:
            _record(name, task.get("purpose") or "", data, meta, "hypothesis-driven")
        else:
            failures.append(f"{name}：{meta.get('error')}")

    # ── ②b 数据绑定层：把假设变成参数齐全的检验 ──────────────
    # 这一步是"假设—实验—算"真正闭合的地方：设计步骤只能声明对比，
    # 这里用文献字段（是否报告数据集 / 方法是否 GNN / 是否开放获取 / 年份）
    # 把对比变成真实数组，再去跑统计检验。
    auto_tasks = _auto_hypothesis_tasks(papers, (hypothesis or {}).get("hypotheses"))
    if auto_tasks:
        emit(f"按假设自动推导出 {len(auto_tasks)} 项检验（用文献字段构造分组）")
    for task in auto_tasks:
        name = task["tool"]
        if name not in catalog:
            continue
        ok, data, meta = _mcp_call(name, task["args"], emit, task["purpose"])
        if ok:
            _record(name, task["purpose"], data, meta, "auto-derived")
        else:
            failures.append(f"{name}：{meta.get('error')}")

    # ── ③ 文献元数据的确定性统计（保底） ──────────────────
    defaults = _default_literature_tasks(papers)
    if not computations:
        for task in defaults:
            ok, data, meta = _mcp_call(task["tool"], task["args"], emit, task["purpose"])
            if ok:
                _record(task["tool"], task["purpose"], data, meta, "bibliometric")
            else:
                failures.append(f"{task['tool']}：{meta.get('error')}")
    else:
        # 有模型/数据集任务时，也补 1 条确定性统计作为交叉验证材料
        for task in defaults[:1]:
            ok, data, meta = _mcp_call(task["tool"], task["args"], emit, task["purpose"])
            if ok:
                _record(task["tool"], task["purpose"], data, meta, "bibliometric")

    # 每个任务只保留结果里的关键值，避免快照过大
    trimmed = []
    for c in computations:
        r = c.get("result") or {}
        trimmed.append({
            "tool": c["tool"], "purpose": c["purpose"], "source": c["source"],
            "args": c.get("args"), "repro": c.get("repro"),
            "result": r if isinstance(r, dict) else {"raw": str(r)[:400]},
        })

    if not trimmed:
        emit("没有计算任务成功执行 —— 后续验证将标记为不可复算")
    else:
        emit(f"完成 {len(trimmed)} 项计算" +
             (f"，{len(failures)} 项失败" if failures else "") +
             f"，覆盖 {len({c['source'] for c in trimmed})} 类数据来源")

    return {
        "computations": trimmed,
        "count": len(trimmed),
        "failures": failures[:6],
        "iteration": iteration,
        "degraded": not trimmed,
    }


# ═══════════════════════════════════════════════════════════════
#  ⑦ 验：复现校验与迭代
# ═══════════════════════════════════════════════════════════════

def _replay_one(comp):
    """用 repro 里记的参数把一次计算**重新跑一遍**，比对结果是否一致。

    这是"可复现验证"最硬的一环：不是让模型自己说"我核对过了"，
    而是真的再执行一次同样的工具调用。数值不一致就意味着
    报告里的数字不可信（通常是模型自己改过数）。
    """
    repro = comp.get("repro") or {}
    tool = repro.get("tool") or comp.get("tool")
    args = repro.get("args")
    if not tool:
        return {"tool": tool, "ok": False, "detail": "缺少 repro.tool，无法重算"}
    try:
        ok, again, meta = _mcp_call(tool, args or {})
    except Exception as e:  # noqa: BLE001
        return {"tool": tool, "ok": False, "detail": f"重算异常：{type(e).__name__}: {e}"}
    if not ok:
        return {"tool": tool, "ok": False, "detail": f"重算失败：{meta.get('error')}"}

    first = comp.get("result")
    same = json.dumps(again, ensure_ascii=False, sort_keys=True) == \
        json.dumps(first, ensure_ascii=False, sort_keys=True)
    # 数值型结果允许 1e-9 的相对误差（浮点累加顺序可能不同）
    if not same and isinstance(again, dict) and isinstance(first, dict):
        same = _almost_equal(again, first)
    return {"tool": tool, "ok": bool(same),
            "detail": "重算结果一致" if same else "重算结果不一致（数值被改动过？）"}


def _almost_equal(a, b, tol=1e-9):
    """宽松数值比较：结构一致 + 数值在容差内。"""
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        return False
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_almost_equal(a[k], b[k], tol) for k in a)
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(_almost_equal(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))
    return a == b


def step_verify(cfg, papers, hypothesis, experiment, computation, emit,
                iteration=0, goal=""):
    """三类检查 + 独立审稿，给出 pass / revise / reject。

    确定性检查先跑（可复算、证据编号、覆盖度），模型审稿只作为**补充意见**：
    两者任一判 revise 都要返工。这样即便模型"护短"，复算不一致也会被拦下。
    """
    checks = []
    comps = (computation or {}).get("computations") or []

    # ── A. 数值层：逐项重算 ──────────────────────────────
    if comps:
        emit(f"开始复算 {len(comps)} 项计算")
        replays = [_replay_one(c) for c in comps]
        failed = [r for r in replays if not r["ok"]]
        checks.append({
            "name": "数值一致性",
            "ok": not failed,
            "detail": (f"{len(replays) - len(failed)}/{len(replays)} 项重算一致"
                       if not failed else
                       "以下计算无法重算/结果不一致：" +
                       "；".join(f"{r['tool']}（{r['detail']}）" for r in failed[:3])),
            "items": replays[:8],
        })
    else:
        replays = []
        checks.append({"name": "数值一致性", "ok": False,
                       "detail": "本轮没有任何计算任务成功执行，无可复算的数值"})

    # ── B. 逻辑层：假设依据是否真实存在 ──────────────────
    hyps = (hypothesis or {}).get("hypotheses") or []
    bad_basis = []
    no_falsify = []
    for h in hyps:
        for b in h.get("basis") or []:
            if not (1 <= int(b) <= len(papers)):
                bad_basis.append(f"{h.get('id')} 引用了不存在的文献 [{b}]")
        if not (h.get("falsify") or "").strip():
            no_falsify.append(h.get("id") or "?")
    checks.append({
        "name": "证据可追溯",
        "ok": bool(hyps) and not bad_basis,
        "detail": ("；".join(bad_basis[:3]) if bad_basis else
                   (f"{len(hyps)} 条假设的引用编号均在文献范围内" if hyps
                    else "本轮没有产出假设")),
    })
    checks.append({
        "name": "假设可证伪",
        "ok": bool(hyps) and not no_falsify,
        "detail": ("缺少否定判据：" + "、".join(no_falsify[:3]) if no_falsify else
                   "每条假设都写了否定判据" if hyps else "本轮没有产出假设"),
    })

    # ── C. 工程层：复现凭据是否齐全 ─────────────────────
    missing_repro = [c["tool"] for c in comps if not (c.get("repro") or {}).get("args")
                     and not (c.get("args") or {})]
    checks.append({
        "name": "可移交性",
        "ok": bool(comps) and not missing_repro,
        "detail": (f"{len(comps)} 项计算均带参数与工具名，可重跑" if comps and not missing_repro
                   else (f"以下计算缺少参数记录：{', '.join(missing_repro[:3])}"
                         if missing_repro else "没有计算产物可移交")),
    })

    # ── D. 仿真/读图不得冒充实测 ────────────────────────
    fabricated_risk = []
    for c in comps:
        if c.get("source") == "bibliometric" and "实测" in json.dumps(
                c.get("result") or {}, ensure_ascii=False):
            fabricated_risk.append(c["tool"])
    checks.append({
        "name": "数据来源诚实性",
        "ok": not fabricated_risk,
        "detail": ("存在把统计量描述为实测的风险：" + "、".join(fabricated_risk[:3])
                   if fabricated_risk else
                   "仿真/统计类结果未标注为实测；数据来源已在产物里区分"),
    })

    deterministic_fail = [c for c in checks if not c["ok"]]

    # ── E. 模型独立审稿（补充意见） ─────────────────────
    review = _role_json("critic", "请独立复核这一轮研究并给出 verdict。", {
        "研究目标": cfg.get("topic") or goal,
        "假设": hyps,
        "实验方案": {
            "summary": (experiment or {}).get("summary"),
            "design_type": (experiment or {}).get("design_type"),
            "n_runs": (experiment or {}).get("n_runs"),
            "metrics": (experiment or {}).get("metrics"),
            "success_criteria": (experiment or {}).get("success_criteria"),
        },
        "计算结果（含复现凭据）": [
            {"tool": c["tool"], "source": c["source"], "purpose": c["purpose"],
             "result": c.get("result")} for c in comps[:8]
        ],
        "自动化检查结果": [{"name": c["name"], "ok": c["ok"], "detail": c["detail"]}
                          for c in checks],
        "迭代轮次": iteration + 1,
        "文献数量": len(papers),
        "喂给模型的文献条目数": min(len(papers), BRIEF_PAPERS),
    }, goal=cfg.get("topic") or goal, max_tokens=2800, temperature=0.1,
        prompt_override=VERIFY_PROMPT)

    llm_verdict = str((review or {}).get("verdict") or "").strip().lower()
    if llm_verdict not in ("pass", "revise", "reject"):
        llm_verdict = ""

    fixes = [str(x)[:160] for x in ((review or {}).get("fixes") or [])
             if isinstance(x, str) and x.strip()][:5]
    open_q = [str(x)[:160] for x in ((review or {}).get("open_questions") or [])
              if isinstance(x, str) and x.strip()][:5]
    llm_checks = []
    for c in ((review or {}).get("checks") or [])[:5]:
        if isinstance(c, dict) and c.get("name"):
            llm_checks.append({"name": str(c["name"])[:30], "ok": bool(c.get("ok")),
                               "detail": str(c.get("detail") or "")[:200]})

    # ── 合成 verdict ────────────────────────────────────
    if deterministic_fail:
        verdict = "reject" if any(c["name"] == "证据可追溯" and not c["ok"]
                                  for c in deterministic_fail) and not hyps else "revise"
        if not fixes:
            fixes = [c["detail"] for c in deterministic_fail][:3]
        reason = "自动检查未通过：" + "；".join(c["name"] for c in deterministic_fail)
    elif llm_verdict == "reject":
        verdict = "reject"
        reason = "审稿人判定核心结论不成立"
    elif llm_verdict == "revise":
        verdict = "revise"
        reason = "审稿人要求返工"
    else:
        verdict = "pass"
        reason = "自动检查通过" + ("，审稿人同意" if llm_verdict == "pass" else "（审稿意见缺失，按自动检查结论）")

    emit(f"验证结论：{verdict}（{reason}）")

    return {
        "verdict": verdict,
        "reason": reason,
        "checks": checks + llm_checks,
        "fixes": fixes,
        "open_questions": open_q,
        "llm_verdict": llm_verdict or "unavailable",
        "confidence": str((review or {}).get("confidence") or "medium")[:10],
        "summary": str((review or {}).get("summary") or "")[:300],
        "replay": {"total": len(replays), "same": sum(1 for r in replays if r["ok"])},
        "iteration": iteration,
        "degraded": not bool(review),
    }


# ═══════════════════════════════════════════════════════════════
#  ⑧ 写：综述生成
# ═══════════════════════════════════════════════════════════════

def _synth_brief(papers):
    """喂给模型的文献要点：只留论证需要的字段，摘要压到 130 字。"""
    brief = []
    for i, p in enumerate(papers[:BRIEF_PAPERS], 1):
        ex = p.get("extract") or {}
        brief.append({
            "i": i,
            "title": p.get("title"),
            "year": p.get("year"),
            "venue": p.get("journal") or p.get("venue"),
            "object": ex.get("object", ""),
            "method": ex.get("method", ""),
            "data": ex.get("data", ""),
            "metric": ex.get("metric", ""),
            "finding": ex.get("finding", ""),
            # 摘要只留 130 字：综述的论证靠"方法/指标/结论"字段，
            # 摘要在这一步只是补充语境（实测 15380 字的输入里有 1/3 是摘要）
            "abstract": (p.get("abstract") or "")[:130],
        })
    return brief


def _synth_payload(cfg, brief, sections, hypothesis, experiment, computation,
                   verify):
    """整篇撰写用的输入。逐节撰写会在此基础上换掉「章节」字段。"""
    payload = {
        "研究目标": cfg.get("topic"),
        "重点关注": cfg.get("focus"),
        "章节": sections or [],
        "文献": brief,
    }
    if hypothesis:
        payload["共识与空白（假设步骤产出）"] = {
            "consensus": (hypothesis or {}).get("consensus"),
            "gaps": (hypothesis or {}).get("gaps"),
            "hypotheses": [{"id": h.get("id"), "statement": h.get("statement"),
                            "falsify": h.get("falsify")}
                           for h in (hypothesis.get("hypotheses") or [])],
        }
    if experiment:
        payload["实验方案"] = {
            "summary": experiment.get("summary"),
            "design_type": experiment.get("design_type"),
            "n_runs": experiment.get("n_runs"),
            "metrics": experiment.get("metrics"),
        }
    if computation:
        payload["计算结果（工具返回，可直接引用）"] = [
            {"tool": c.get("tool"), "purpose": c.get("purpose"),
             "result": c.get("result")} for c in (computation.get("computations") or [])[:6]
        ]
    if verify:
        payload["验证结论"] = {
            "verdict": verify.get("verdict"),
            "reason": verify.get("reason"),
            "checks": [{"name": c.get("name"), "ok": c.get("ok"),
                        "detail": c.get("detail")} for c in (verify.get("checks") or [])],
            "fixes": verify.get("fixes"),
            "replay": verify.get("replay"),
        }
    return payload


def _as_list(raw, cap):
    out = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            if len(out) >= cap:
                break
    return out


def _synth_list(parsed, key, cap):
    return _as_list((parsed or {}).get(key), cap)


def _salvage_count():
    """`llm` 累计"截断抢救"过几次。看差值就知道刚才那次调用是不是被截断了。"""
    try:
        return int(llm.last_finish().get("salvaged") or 0)
    except Exception:  # noqa: BLE001 - 诊断信息拿不到不影响主流程
        return 0


def _last_truncated():
    try:
        return bool(llm.last_finish().get("truncated"))
    except Exception:  # noqa: BLE001
        return False


def _synth_merge(first, extra, headings):
    """把"补写"的章节按原章节顺序插回去，避免正文顺序错乱。"""
    order = {_norm_heading(h): i for i, h in enumerate(headings or [])}
    merged = list(first) + list(extra)
    merged.sort(key=lambda s: order.get(_norm_heading(s["heading"]), 99))
    return merged


_SENT_END = "。！？；…」』）)”\"'.!?"


def _clean_body(body):
    """正文收尾处理：被截断的半句话不要留在报告里。

    截断抢救（`llm.salvage_json`）能保住写了一半的字段，但那一节的正文
    往往停在句子中间（"…该方法的优势在于"）。报告里出现断句比少一节更糟，
    所以退到最后一个句末；**只有一条句子都没写完时才用原样**（宁可多点字，也不整节丢）。
    """
    b = str(body or "").strip()
    if not b:
        return ""
    if b[-1] in _SENT_END or len(b) < 40:
        return b
    cut = max(b.rfind(x) for x in "。！？；.!?")
    if cut >= 20:
        return b[:cut + 1].strip()
    return b


def _synth_sections(parsed, headings=None):
    """从模型返回里取出章节：[{heading, body}]。

    兼容三种形状（都真的见过）：
    * 整篇：{"sections": [{heading, body}, ...]}
    * 逐节：{"heading": "...", "body": "..."}
    * 截断抢救后只剩一部分章节（同样走第一种）
    """
    out = []
    if not isinstance(parsed, dict):
        return out
    raw = parsed.get("sections")
    if isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict) and str(s.get("heading") or "").strip():
                out.append({"heading": str(s["heading"]).strip(),
                            "body": _clean_body(s.get("body"))})
    elif str(parsed.get("body") or "").strip():
        head = str(parsed.get("heading") or parsed.get("标题") or "").strip()
        out.append({"heading": head or (headings[0] if headings else "综述"),
                    "body": _clean_body(parsed["body"])})
    # 空正文的章节没有意义，留着只会让报告里出现空标题
    return [s for s in out if s["body"]]


def _norm_heading(text):
    """章节标题归一化：去掉标点与空白，只留字，便于比对。"""
    return re.sub(r"[\s，。、：:；;（）()【】\[\]“”\"'·—\-/]+", "", str(text or ""))


def _synth_missing(headings, secs):
    """哪些请求的章节没拿到（模型可能少写、或被截断）。

    标题是模型自己复述的，字面未必一字不差（"损失函数与训练范式" vs
    "损失函数、优化策略与训练范式横向比较"），所以按"包含/前缀"做宽松比对，
    宁可判"写到了"，也不要冤枉模型、更不要在报告里凭空报缺节。
    """
    if not headings:
        return []
    done = [_norm_heading(s["heading"]) for s in secs]
    missing = []
    for h in headings:
        want = _norm_heading(h)
        if not want:
            continue
        hit = any(want in d or d in want or
                  (len(want) >= 4 and len(d) >= 4 and (want[:4] in d or d[:4] in want))
                  for d in done if d)
        if not hit:
            missing.append(h)
    return missing


def _synth_by_sections(cfg, headings, payload, emit, stop=None):
    """逐节写：一次只写一节，成功的留下、失败的记下来。

    关键点：**单节失败不连坐**。整篇一次写是"全有或全无"，
    第 14 次运行里就是因为最后少一个 `}` 让前面写好的 3 节全废了。
    拆开以后，能写几节就留几节，缺的如实写进 caveat。

    返回 (已写章节, 缺的章节, 是否被用户中断)。
    """
    secs, missing = [], []
    stopped = False
    heads = headings or ["研究现状与主要方法", "存在的问题与研究空白"]
    for i, h in enumerate(heads, 1):
        if stop and stop():
            missing.extend(heads[i - 1:])
            emit("收到中断，综述未写完")
            stopped = True
            break
        emit(f"逐节撰写 {i}/{len(heads)}：{h}")
        part = dict(payload)
        part["综述的全部章节"] = heads
        part["当前章节"] = h
        parsed = _role_json(
            "writer", f"只写「{h}」这一节。", part,
            goal=cfg.get("topic") or "", max_tokens=SYNTH_SECTION_TOKENS,
            temperature=0.2, prompt_override=SYNTH_SECTION_PROMPT,
            # 单节失败不连坐，但这一节本身值得多试一次（额度会逐次抬高）
            retries=3)
        got = _synth_sections(parsed, [h])
        if got:
            secs.append({"heading": h, "body": got[0]["body"]})
        else:
            missing.append(h)
    return secs, missing, stopped


def _synth_caveat(caveat, meta, n_papers):
    """把"生成方式"和"缺了哪几节"如实写进局限，而不是假装整篇都写好了。"""
    notes = []
    if caveat:
        notes.append(caveat)
    if meta.get("missing"):
        notes.append("以下章节本轮未能生成（模型未返回可用内容）："
                     + "、".join(meta["missing"][:5]) + "。")
    if meta.get("mode") in ("by-section", "oneshot+补写"):
        notes.append("整篇一次生成时模型输出被截断（思考与正文共用输出额度），"
                     "部分章节改为单独撰写；章节之间的过渡不如整篇连贯。")
    elif meta.get("truncated"):
        notes.append("整篇一次生成时模型输出曾被截断，只保留了写完的部分。")
    if not notes:
        notes.append(f"本篇综述仅基于检索到的 {n_papers} 篇文献（喂给模型的为前 "
                     f"{BRIEF_PAPERS} 篇要点），未做全文系统阅读。")
    return " ".join(n.strip() for n in notes if n and n.strip())


def step_synthesize(cfg, papers, emit, sections=None, stop=None,
                    hypothesis=None, experiment=None, computation=None,
                    verify=None):
    """写综述。**三步退让（第 14 次运行就是挂在这一步的）**：

    ① 整篇一次写完 —— 最省调用、上下文最连贯；
    ② 整篇的 JSON 被截断 → `llm.loads_json` 先"抢救"已经写完的章节；
    ③ 还是拼不齐 → **逐节写**：一次只写一节，成功的节直接留下，
       失败的那节记进 caveat，不再让整步归零。

    ## 为什么"重试整篇"没用

    `max_tokens` 是**思考内容 + 正文共用**的。实测该次运行（workflow #14）：
    flash 先思考了 2698 token，正文写到第 3 节就被砍断
    （`finish_reason=length`），JSON 少了收尾的 `]` `}`，`json.loads` 必然失败。
    同一个提示词、同一个 4000 的预算再跑一遍，结果几乎一模一样 ——
    两轮重试一共 60s，然后整步判失败、报告里没有综述正文。
    所以修法是：抬额度（SYNTH_TOKENS）+ 截断抢救 + 缩小单次输出（逐节）。
    """
    if not papers:
        raise RuntimeError("没有文献，无法生成综述")
    if stop and stop():
        emit("收到中断，未开始撰写综述")
        return {"sections": [], "consensus": [], "gaps": [], "caveat": "",
                "interrupted": True}
    emit(f"阅读 {len(papers)} 篇并撰写综述")

    brief = _synth_brief(papers)
    payload = _synth_payload(cfg, brief, sections, hypothesis, experiment,
                            computation, verify)
    headings = [str(h).strip() for h in (sections or []) if str(h).strip()]

    # ── ① 整篇一次写完（额度已按"思考 + 正文"重新估算）──────────
    salvaged0 = _salvage_count()
    parsed = _role_json("writer", "请按给定章节撰写综述。", payload,
                        goal=cfg.get("topic") or "",
                        max_tokens=SYNTH_TOKENS, temperature=0.2,
                        prompt_override=SYNTH_PROMPT)
    salvaged = _salvage_count() > salvaged0

    secs = _synth_sections(parsed, headings)
    meta = {"mode": "oneshot" if secs else "", "degraded": False,
            "missing": [], "truncated": salvaged, "recovered": salvaged}
    stopped = False

    if secs:
        # 整篇返回了：核对"请求的章节"是否都写到了（截断时通常少最后几节）
        meta["missing"] = _synth_missing(headings, secs)
        meta["degraded"] = bool(meta["missing"])
        if salvaged:
            emit(f"整篇输出被截断，已保留写完的 {len(secs)} 节")
        if meta["missing"]:
            # 只补缺的节，已经写好的不重写（省时间，也不会出现两版正文）
            emit("补齐缺失的章节：" + "、".join(meta["missing"][:3]))
            more, still, stopped = _synth_by_sections(cfg, meta["missing"], payload,
                                                      emit, stop=stop)
            if more:
                secs = _synth_merge(secs, more, headings)
                meta["mode"] = "oneshot+补写"
            meta["missing"] = still
            meta["degraded"] = bool(still)

    # ── ②/③ 整篇不可用：先看是不是被截断，再决定逐节写 ───────────
    if not secs and not stopped:
        meta["truncated"] = salvaged or bool(_last_truncated())
        emit("整篇输出" + ("被截断" if meta["truncated"] else "不是完整 JSON")
             + "，改为逐节撰写")
        secs, missing, stopped = _synth_by_sections(cfg, headings, payload, emit,
                                                    stop=stop)
        if secs:
            meta["mode"] = "by-section"
            meta["missing"] = missing
            meta["degraded"] = bool(missing)

    # 逐节写时，共识/空白/局限要单独补（整篇那次没拿到）
    consensus = _synth_list(parsed, "consensus", 4)
    gaps = _synth_list(parsed, "gaps", 3)
    caveat = str((parsed or {}).get("caveat") or "").strip()

    if secs and not consensus:
        # 假设步骤的共识/空白是带文献编号、有出处的，可以直接作为综述的归纳
        consensus = _as_list((hypothesis or {}).get("consensus"), 4)
        gaps = gaps or _as_list((hypothesis or {}).get("gaps"), 3)
    if secs and not (consensus or gaps):
        emit("补写共识与空白")
        extra = _role_json("writer", "请归纳共识与空白。", {
            "研究目标": cfg.get("topic"),
            "已写章节": [{"heading": s["heading"], "body": (s["body"] or "")[:400]}
                         for s in secs],
            "文献": brief,
        }, goal=cfg.get("topic") or "", max_tokens=1200, temperature=0.1,
            prompt_override=SYNTH_SUMMARY_PROMPT)
        consensus = consensus or _synth_list(extra, "consensus", 4)
        gaps = gaps or _synth_list(extra, "gaps", 3)
        caveat = caveat or str((extra or {}).get("caveat") or "").strip()

    caveat = _synth_caveat(caveat, meta, len(papers))

    if stopped:
        # 用户中途点了中断：按"断点在这一步"处理（与 extract 的语义一致），
        # 已经写好的章节带回去，续跑时会重写这一步。
        emit("综述中断，已写的章节已保存")
        return {"sections": secs, "consensus": consensus, "gaps": gaps,
                "caveat": caveat, "degraded": meta["degraded"],
                "mode": meta["mode"], "missing_sections": meta["missing"],
                "interrupted": True}

    if not secs:
        # 一节都没写出来才算失败（前端与测试都依赖这条语义：不粉饰、如实失败）
        detail = "；模型输出被截断" if meta["truncated"] else ""
        raise RuntimeError(f"综述生成失败（模型未返回可用内容）{detail}")

    if meta["missing"]:
        emit(f"综述完成：{len(secs)} 节（缺 {len(meta['missing'])} 节，已如实标注）")
    else:
        emit(f"综述完成：{len(secs)} 节")

    return {
        "sections": secs,
        "consensus": consensus,
        "gaps": gaps,
        "caveat": caveat,
        "degraded": meta["degraded"],
        "mode": meta["mode"],
        "missing_sections": meta["missing"],
    }


# ═══════════════════════════════════════════════════════════════
#  ⑨ 写：参考文献格式化 + 报告
# ═══════════════════════════════════════════════════════════════

def gbt_author(name):
    """GB/T 7714：中文作者原样、英文作者"姓全大写 + 名缩写"。

    这里**不做**"从拼音判断哪个是中文姓"的猜测。试过一版按常见姓氏表判断，
    结果把 "Zhang San" 判成 "SAN Z"（"三"的拼音被当成了姓），
    因为姓氏表里的字同时也能拼出英文词。数据源给的作者顺序本来就是对的
    （中文姓名在前、英文姓名在后），照最后一个词当姓即可。

    但有个数据源的坑要处理：OpenAlex 对中文论文给的是**汉字+拼音拼在一起**的
    字符串（`曾军英ZengJunying`），不是两个字段。GB/T 7714 的中文著录只要汉字，
    拼音是多余的，所以中英混排时只保留汉字部分。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    han = "".join(re.findall(r"[\u4e00-\u9fff]+", s))
    if han:
        return re.sub(r"\s+", "", han)
    parts = s.split()
    if len(parts) == 1:
        return parts[0].upper()
    family = parts[-1].upper()
    initials = " ".join(
        " ".join(x[0].upper() for x in w.split("-") if x) for w in parts[:-1])
    return f"{family} {initials}"


def gbt_authors(authors):
    lst = [gbta for gbta in (gbt_author(a) for a in (authors or [])) if gbta]
    if not lst:
        return "佚名"
    if len(lst) <= 3:
        return ", ".join(lst)
    zh = any(re.search(r"[\u4e00-\u9fff]", a) for a in lst)
    return ", ".join(lst[:3]) + ("等" if zh else ", et al")


DOC_MARK = {"journal": "J", "conference": "C", "thesis": "D", "book": "M", "preprint": "EB/OL"}


def format_gbt7714(p, idx=None):
    """GB/T 7714-2015 著录格式（与前端保持同一套规则）。"""
    seg = [gbt_authors(p.get("authors")) + "."]
    seg.append((p.get("title") or "") + "[" + DOC_MARK.get(p.get("docType") or "journal", "J") + "].")
    src = (p.get("journal") or p.get("venue") or "").strip() or "[刊名不详]"
    year = p.get("year")
    if year and year != "未知":
        src += ", " + str(year)
    if p.get("volume"):
        src += ", " + str(p["volume"]) + (f"({p['issue']})" if p.get("issue") else "")
    if p.get("pages"):
        src += ": " + str(p["pages"])
    seg.append(src + ".")
    if p.get("doi"):
        seg.append("DOI: " + p["doi"] + ".")
    text = re.sub(r"\s+", " ", " ".join(seg)).strip()
    return text if idx is None else f"[{idx}] {text}"


def short_authors(authors, limit=2):
    """报告表格里的作者列：常见中文人名的展示形式。"""
    names = []
    for a in (authors or [])[:limit]:
        han = "".join(re.findall(r"[\u4e00-\u9fff]+", str(a)))
        names.append(han or str(a).strip())
    names = [n for n in names if n]
    if not names:
        return "佚名"
    return "、".join(names) + ("等" if len(authors or []) > limit else "")


def _repro_manifest(computations, llm_info=None):
    """生成"复现清单"：别人照着这些命令能重算出同样的数。"""
    lines = []
    for c in computations or []:
        repro = c.get("repro") or {}
        args = repro.get("args") or c.get("args") or {}
        lines.append("- `%s` ← 参数 `%s`%s" % (
            c.get("tool"),
            json.dumps(args, ensure_ascii=False)[:220],
            f"（{c.get('purpose')}）" if c.get("purpose") else ""))
    if llm_info:
        lines.append(f"- 模型：{llm_info.get('backend_label')} / "
                     f"{', '.join(r['model'] for r in llm_info.get('roles') or [])}")
    lines.append("- 运行时：Python " + sys.version.split()[0]
                 + "；工具经 MCP 协议调用（`mcp_server.py`），协议 2025-03-26")
    return lines


def step_format(cfg, papers, plan, emit, synth=None, stats=None,
                hypothesis=None, experiment=None, computation=None, verify=None,
                iterations=None):
    """参考文献 + 复现清单 + 整合报告。

    两点必须注意（都是踩过的坑）：
    1. 要把 synth / stats 传进 build_report —— 否则报告里没有综述正文和统计图，
       只剩一张文献表和参考文献。
    2. **没有文献也要出报告**。前面的步骤失败时（比如检索被限流），这里再抛错的话
       用户就拿不到任何产物；不如生成一份说明失败原因的报告。
    """
    refs = [format_gbt7714(p, i) for i, p in enumerate(papers, 1)] if papers else []
    if refs:
        emit(f"生成 {len(refs)} 条 GB/T 7714 参考文献")
    else:
        emit("没有可用文献，生成一份说明性的报告")
    md = build_report(cfg, plan, papers, refs, synth=synth, stats=stats,
                      hypothesis=hypothesis, experiment=experiment,
                      computation=computation, verify=verify, iterations=iterations)
    emit(f"报告已生成（{len(md)} 字）")
    return {"references": refs, "report": md, "chars": len(md)}


def build_report(cfg, plan, papers, refs, synth=None, stats=None,
                 hypothesis=None, experiment=None, computation=None,
                 verify=None, iterations=None):
    """把各步产物拼成一份可直接导出的 Markdown 报告。"""
    L = []
    topic = cfg.get("topic") or cfg.get("query") or "研究主题"
    L.append(f"# {topic}：AI4S 研究报告")
    L.append("")
    L.append(f"> 由 AI4S 科学智能体自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if cfg.get("focus"):
        L.append(f"> 重点关注：{cfg['focus']}")
    if cfg.get("research_type"):
        L.append(f"> 研究类型：{cfg['research_type']}")
    L.append("> 流程：搜 → 读 → 想（假设） → 做（实验） → 算（计算） → 验（复现） → 写")
    L.append("")
    L.append(f"## 摘要")
    L.append("")
    summ = []
    if hypothesis and hypothesis.get("hypotheses"):
        h0 = hypothesis["hypotheses"][0]
        summ.append(f"本文围绕「{topic}」检索并精读 {len(papers)} 篇文献，"
                    f"提出 {len(hypothesis['hypotheses'])} 条可证伪假设"
                    f"（首条：{h0.get('statement')}）。")
    else:
        summ.append(f"本文围绕「{topic}」检索并精读 {len(papers)} 篇文献。")
    if computation and computation.get("computations"):
        summ.append(f"调用 {len(computation['computations'])} 项科学计算工具产出真实数值，"
                    f"其中 {sum(1 for c in computation['computations'] if c.get('source') == 'user-dataset')} "
                    f"项基于用户数据集。")
    if verify:
        summ.append(f"复现校验结论为 **{verify.get('verdict')}**"
                    f"（重算一致 {verify.get('replay', {}).get('same', 0)}/"
                    f"{verify.get('replay', {}).get('total', 0)} 项）。")
    L.extend(summ)
    L.append("")

    # ── 一、检索概况 ──────────────────────────────────
    L.append("## 一、检索概况")
    L.append("")
    qs = (plan.get("queries") or []) if isinstance(plan, dict) else []
    L.append(f"- 检索式：`{cfg.get('query')}`" +
             (f"（另有互补式：{' / '.join(qs[1:])}）" if len(qs) > 1 else ""))
    L.append(f"- 来源范围：{cfg.get('source')}")
    L.append(f"- 命中文献：{len(papers)} 篇")
    if stats:
        L.append(f"- 其中开放获取且有全文直链：{stats.get('withFulltext', 0)} 篇")
        L.append(f"- 有 DOI：{stats.get('withDoi', 0)} 篇")
        if stats.get("recent_ratio"):
            L.append(f"- 近两年文献占比：{stats['recent_ratio'] * 100:.1f}%")
    L.append("")
    L.append("| # | 标题 | 作者 | 年份 | 来源 | 被引 |")
    L.append("|---|------|------|------|------|------|")
    for i, p in enumerate(papers, 1):
        au = short_authors(p.get("authors"))
        L.append("| %d | %s | %s | %s | %s | %s |" % (
            i, (p.get("title") or "").replace("|", "/"), au, p.get("year"),
            p.get("sourceLabel") or p.get("source") or "", p.get("citationCount") or 0))
    L.append("")

    # ── 二、科学假设 ──────────────────────────────────
    if hypothesis:
        L.append("## 二、科学假设与逻辑推导")
        L.append("")
        if hypothesis.get("reasoning"):
            L.append(f"> 推导：{hypothesis['reasoning']}")
            L.append("")
        if hypothesis.get("degraded"):
            L.append("> ⚠️ 本步为**降级模式**：模型未返回可用假设，以下由文献统计事实改写而来，"
                     "仅供参考，不作为已验证结论。")
            L.append("")
        if hypothesis.get("consensus"):
            L.append("**当前共识**")
            L.append("")
            for x in hypothesis["consensus"]:
                L.append(f"- {x}")
            L.append("")
        if hypothesis.get("gaps"):
            L.append("**证据缺口**")
            L.append("")
            for x in hypothesis["gaps"]:
                L.append(f"- {x}")
            L.append("")
        for h in hypothesis.get("hypotheses") or []:
            basis = "、".join(f"[{b}]" for b in (h.get("basis") or [])) or "（无引用）"
            L.append(f"### {h.get('id')}：{h.get('statement')}")
            L.append("")
            L.append(f"- 依据文献：{basis}｜置信度：{h.get('confidence')}")
            if h.get("mechanism"):
                L.append(f"- 机理：{h['mechanism']}")
            L.append(f"- 变量：自变量 {h.get('iv') or '—'}；因变量 {h.get('dv') or '—'}；"
                     f"控制 {h.get('control') or '—'}")
            L.append(f"- 否定判据：{h.get('falsify') or '—'}")
            L.append(f"- 预期结果：{h.get('prediction') or '—'}")
            L.append("")

    # ── 三、实验/仿真方案 ────────────────────────────
    if experiment:
        L.append("## 三、实验方案设计与仿真")
        L.append("")
        L.append(f"- 方案概要：{experiment.get('summary')}")
        L.append(f"- 设计类型：{experiment.get('design_type')}；计划运行次数：{experiment.get('n_runs')}")
        pw = experiment.get("power") or {}
        if pw:
            L.append(f"- 功效分析：效应量 {pw.get('effect_size')}，"
                     f"每组所需样本量 {pw.get('n_per_group')}（α=0.05, power=0.8）")
        if experiment.get("metrics"):
            L.append(f"- 评价指标：{'、'.join(experiment['metrics'])}")
        if experiment.get("success_criteria"):
            L.append(f"- 成功判据：{'；'.join(experiment['success_criteria'])}")
        if experiment.get("failure_criteria"):
            L.append(f"- 否定判据：{'；'.join(experiment['failure_criteria'])}")
        if experiment.get("limitations"):
            L.append(f"- 方案局限：{experiment['limitations']}")
        L.append("")
        factors = experiment.get("factors") or []
        design = experiment.get("design") or {}
        runs = design.get("runs") or design.get("matrix") or []
        if factors:
            L.append("**实验因子**")
            L.append("")
            L.append("| 因子 | 水平 | 单位 |")
            L.append("|------|------|------|")
            for f in factors:
                L.append("| %s | %s | %s |" % (f.get("name"),
                                               "、".join(str(x) for x in f.get("levels") or []),
                                               f.get("unit") or "—"))
            L.append("")
        if runs:
            L.append(f"**设计矩阵（{design.get('type')}，共 {len(runs)} 组）**")
            L.append("")
            if isinstance(runs[0], dict):
                keys = list(runs[0].keys())
                L.append("| " + " | ".join(keys) + " |")
                L.append("|" + "|".join(["---"] * len(keys)) + "|")
                for r in runs[:16]:
                    L.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
            else:
                L.append("```")
                for r in runs[:16]:
                    L.append(str(r))
                L.append("```")
            L.append("")
        if experiment.get("steps"):
            L.append("**执行步骤**")
            L.append("")
            for i, s in enumerate(experiment["steps"], 1):
                L.append(f"{i}. {s}")
            L.append("")
            if experiment.get("degraded"):
                L.append("> ⚠️ 方案为**降级模式**（模型未返回可用方案），"
                         "因子与步骤取自默认对照设计。")
                L.append("")

    # ── 四、计算结果（真实数值） ─────────────────────
    if computation:
        L.append("## 四、科学计算结果")
        L.append("")
        comps = computation.get("computations") or []
        src_label = {"user-dataset": "用户数据集", "hypothesis-driven": "假设驱动",
                     "auto-derived": "假设自动推导（文献字段构造分组）",
                     "bibliometric": "文献计量"}
        if not comps:
            L.append("本轮没有计算任务成功执行。失败原因：" +
                     ("；".join(computation.get("failures") or []) or "未知"))
            L.append("")
        for c in comps:
            L.append(f"### {c.get('tool')} — {c.get('purpose') or '计算'}")
            L.append("")
            L.append(f"- 数据来源：{src_label.get(c.get('source'), c.get('source'))}"
                     f"｜参数：`{json.dumps(c.get('args') or {}, ensure_ascii=False)[:200]}`")
            L.append("")
            L.append("```json")
            L.append(json.dumps(c.get("result") or {}, ensure_ascii=False, indent=2)[:2000])
            L.append("```")
            L.append("")
        if computation.get("failures"):
            L.append("> 未成功的计算：" + "；".join(computation["failures"][:4]))
            L.append("")

    # ── 五、验证与迭代 ───────────────────────────────
    if verify:
        L.append("## 五、复现验证与迭代")
        L.append("")
        verdict = verify.get("verdict")
        verdict_note = {
            "pass": "检查通过，可以引用本报告的结论",
            "revise": "方向成立但需返工；返工项见下，未在迭代上限内收敛的已如实标注",
            # reject 是**科学结论**，不是流程失败：假设被数据否定同样是有价值的结果
            "reject": "本轮假设在现有证据下**未被支持（负结果）**。"
                      "这不是流程失败 —— 负结果同样记录在案，并给出下一步该问什么",
        }.get(verdict, "")
        L.append(f"- 结论：**{verdict}**（{verify.get('reason')}）")
        if verdict_note:
            L.append(f"- 解读：{verdict_note}")
        replay = verify.get("replay") or {}
        if replay:
            L.append(f"- 重算比对：{replay.get('same')}/{replay.get('total')} 项一致")
        L.append(f"- 审稿人意见：{verify.get('llm_verdict')}（置信度 {verify.get('confidence')}）")
        L.append("")
        L.append("| 检查项 | 结论 | 说明 |")
        L.append("|--------|------|------|")
        for c in verify.get("checks") or []:
            L.append("| %s | %s | %s |" % (
                str(c.get("name") or "").replace("|", "/"),
                "✓" if c.get("ok") else "✗",
                str(c.get("detail") or "").replace("|", "/")[:160]))
        L.append("")
        if verify.get("fixes"):
            L.append("**返工项**")
            L.append("")
            for x in verify["fixes"]:
                L.append(f"- {x}")
            L.append("")
        if verify.get("open_questions"):
            L.append("**待解问题（下一轮起点）**")
            L.append("")
            for x in verify["open_questions"]:
                L.append(f"- {x}")
            L.append("")
        if iterations:
            L.append("**迭代记录**")
            L.append("")
            L.append("| 轮次 | 假设数 | 计算数 | 验证结论 | 处理 |")
            L.append("|------|--------|--------|----------|------|")
            for it in iterations:
                L.append("| %s | %s | %s | %s | %s |" % (
                    it.get("iteration"), it.get("hypotheses", 0), it.get("computations", 0),
                    it.get("verdict") or "—", it.get("action") or "—"))
            L.append("")

    # ── 六、综述正文 ─────────────────────────────────
    if synth:
        L.append("## 六、综述正文")
        L.append("")
        if synth.get("degraded") or (synth.get("missing_sections") or []):
            L.append("> ⚠️ 本节为**降级生成**：整篇一次撰写时模型输出被截断，"
                     "部分章节改为单独撰写"
                     + ("；未生成的章节：" + "、".join(
                         (synth.get("missing_sections") or [])[:5])
                        if synth.get("missing_sections") else "")
                     + "。缺失章节不会由代码补写，请结合第七节的统计结果阅读。")
            L.append("")
        for s in synth.get("sections") or []:
            L.append(f"### {s['heading']}")
            L.append("")
            L.append(s["body"])
            L.append("")
        if synth.get("consensus"):
            L.append("### 主要共识")
            L.append("")
            for x in synth["consensus"]:
                L.append(f"- {x}")
            L.append("")
        if synth.get("gaps"):
            L.append("### 研究空白")
            L.append("")
            for x in synth["gaps"]:
                L.append(f"- {x}")
            L.append("")
        if synth.get("caveat"):
            L.append(f"> ⚠️ 局限：{synth['caveat']}")
            L.append("")
    else:
        # 综述这一步失败时**不留白**：把"为什么没有正文"写清楚，
        # 免得读者以为是漏排版，也免得有人把第七节的统计当成结论。
        L.append("## 六、综述正文")
        L.append("")
        L.append("> ❌ 本轮未能生成综述正文：「综述与报告撰写」这一步失败"
                 "（模型未返回可用内容）。第七节的统计与第八节的抽取明细仍然有效，"
                 "可直接引用；需要综述正文时，请在工作流里点「从断点继续」重跑该步。")
        L.append("")

    # ── 七、统计与可视化 ─────────────────────────────
    if stats:
        L.append("## 七、统计与可视化")
        L.append("")
        for title, key in (("年份分布", "years"), ("被引分布", "citations"),
                           ("文献类型", "docTypes"), ("数据来源", "sources")):
            items = stats.get(key) or []
            if not items:
                continue
            L.append(f"**{title}**")
            L.append("")
            L.append("```")
            for it in items:
                L.append("%-10s %s %d" % (it["label"], it["bar"], it["count"]))
            L.append("```")
            L.append("")
        if stats.get("evidence_gaps"):
            L.append("**证据缺口（自动识别）**")
            L.append("")
            for g in stats["evidence_gaps"]:
                L.append(f"- {g}")
            L.append("")

    # ── 八、信息抽取明细 ─────────────────────────────
    extract_rows = [(i, p) for i, p in enumerate(papers, 1)
                    if any(v for k, v in (p.get("extract") or {}).items() if k != "numbers")]
    if extract_rows:
        L.append("## 八、信息抽取明细")
        L.append("")
        L.append("| # | 研究对象 | 方法 | 数据 | 指标 | 主要结论 |")
        L.append("|---|----------|------|------|------|----------|")
        for i, p in extract_rows:
            e = p["extract"]
            cell = lambda v: str(v or "").replace("|", "/")
            L.append("| %d | %s | %s | %s | %s | %s |" % (
                i, cell(e.get("object")), cell(e.get("method")), cell(e.get("data")),
                cell(e.get("metric")), cell(e.get("finding"))))
        L.append("")

    # ── 九、参考文献 ─────────────────────────────────
    L.append("## 九、参考文献（GB/T 7714）")
    L.append("")
    for r in refs:
        L.append(r)
    L.append("")

    # ── 十、复现清单 ─────────────────────────────────
    L.append("## 十、复现清单")
    L.append("")
    L.append("以下记录足以让别人重算出本报告里的数值：")
    L.append("")
    try:
        info = llm.model_info()
    except Exception:  # noqa: BLE001
        info = None
    for line in _repro_manifest((computation or {}).get("computations") or [], info):
        L.append(line)
    L.append("- 复现方式：`python mcp_client.py <工具名> '<参数字典>'`")
    L.append("")

    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════
#  自规划
# ═══════════════════════════════════════════════════════════════

# 规划结果缓存：同一个研究目标只让模型规划一次。
# 为什么值得缓存：规划要 1.5-2s（实测），而**每次提交都会调用它** ——
# 包括"中断后从断点续跑"和"重复点开始执行"，这些场景下目标字符串一模一样，
# 再调一次模型纯属白等。缓存只按目标文本命中，不跨目标复用。
_PLAN_CACHE = {}
_PLAN_CACHE_MAX = 64


def plan_workflow(goal, memory_hint=""):
    """自规划：把研究目标变成一份可执行的研究计划。

    模型不可用时退化成"目标原样当检索词"，保证流程仍能跑。
    `memory_hint` 是长周期记忆拼出来的上下文（可为空），只影响提示词，不进缓存键。
    """
    goal = (goal or "").strip()
    fallback = {
        "topic": goal, "query": goal, "queries": [], "source": "all", "limit": 20,
        "focus": "", "plan": "按研究目标直接检索并归纳",
        "sections": ["研究背景与问题", "主流方法", "数据集与评价指标", "存在问题与展望"],
        "research_type": "review", "metrics": [],
    }
    if not goal:
        return fallback

    cached = _PLAN_CACHE.get(goal)
    if cached is not None and not memory_hint:
        return dict(cached)

    content = goal if not memory_hint else (goal + "\n\n" + memory_hint)
    parsed = _call_json(PLANNER_PROMPT, content, max_tokens=800, kind="chat")
    if not isinstance(parsed, dict):
        return fallback

    def s(k, d=""):
        v = str(parsed.get(k) or "").strip()
        return v or d

    try:
        limit = int(parsed.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    secs = [str(x).strip() for x in (parsed.get("sections") or []) if str(x).strip()][:5]
    queries = [str(x).strip() for x in (parsed.get("queries") or []) if str(x).strip()][:3]
    metrics = [str(x).strip() for x in (parsed.get("metrics") or []) if str(x).strip()][:4]

    src = str(parsed.get("source") or "all").strip().lower()
    rtype = str(parsed.get("research_type") or "review").strip().lower()
    plan = {
        "topic": s("topic", goal),
        "query": s("query", goal),
        "queries": queries,
        "source": src if src in ("all", "cn", "en") else "all",
        "limit": min(max(5, limit), 30),
        "focus": s("focus"),
        "plan": s("plan", "按研究目标检索并归纳"),
        "sections": secs or fallback["sections"],
        "research_type": rtype if rtype in ("review", "mechanism", "data", "experiment") else "review",
        "metrics": metrics,
    }
    if not memory_hint:
        # 简单容量控制：满了就丢最早的一条（不值得为它引 OrderedDict）
        if len(_PLAN_CACHE) >= _PLAN_CACHE_MAX:
            _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
        _PLAN_CACHE[goal] = plan
    return dict(plan)


def clear_plan_cache():
    """清空规划缓存（测试用，避免用例之间互相影响）。"""
    _PLAN_CACHE.clear()


# ═══════════════════════════════════════════════════════════════
#  编排器
# ═══════════════════════════════════════════════════════════════

class WorkflowRunner:
    """按步骤执行工作流。每一步开始/结束都通过 emit 回调报出去，
    调用方（app.py）负责落库与转成 SSE；这样编排逻辑可以脱离 Flask 单测。
    """

    def __init__(self, plan, on_step=None, should_stop=None,
                 max_iterations=None, dataset=None, dataset_kind=None,
                 user_id=None):
        self.plan = plan or {}
        self.on_step = on_step or (lambda *a, **k: None)
        self.should_stop = should_stop or (lambda: False)
        self.max_iterations = MAX_ITERATIONS if max_iterations is None else max(0, int(max_iterations))
        self.dataset = dataset
        self.dataset_kind = dataset_kind or "table"
        self.user_id = user_id
        self.ctx = {}          # 步骤之间传递的产物
        self.state = {}        # key -> {status, detail, error, output}
        self.iterations = []   # 迭代记录（每轮假设/计算/结论）
        self.iteration = 0

    # ── 内部：取上下文 ────────────────────────────────────
    def _papers(self):
        return self.ctx.get("papers") or []

    def _cfg(self):
        cfg = dict(self.plan)
        if self.dataset:
            cfg["dataset"] = self.dataset
            cfg["dataset_kind"] = self.dataset_kind
        return cfg

    def _run_step(self, key, emit):
        """执行一步，返回产物 dict（可能带 interrupted 标记）。"""
        cfg = self._cfg()
        papers = self._papers()
        if key == "search":
            out = step_search(cfg, emit)
            self.ctx["papers"] = out["papers"]
            self.ctx["search"] = out
        elif key == "extract":
            out = step_extract(cfg, papers, emit, stop=self.should_stop)
            self.ctx["extract"] = out
        elif key == "analyze":
            out = step_analyze(cfg, papers, emit)
            self.ctx["stats"] = out
        elif key == "hypothesis":
            out = step_hypothesis(cfg, papers, emit, stats=self.ctx.get("stats"),
                                 feedback=self.ctx.get("feedback"),
                                 iteration=self.iteration,
                                 goal=self.plan.get("topic") or "")
            self.ctx["hypothesis"] = out
        elif key == "experiment":
            out = step_experiment(cfg, papers, self.ctx.get("hypothesis"), emit,
                                 iteration=self.iteration,
                                 goal=self.plan.get("topic") or "")
            self.ctx["experiment"] = out
        elif key == "compute":
            out = step_compute(cfg, papers, self.ctx.get("hypothesis"),
                               self.ctx.get("experiment"), emit,
                               iteration=self.iteration)
            self.ctx["computation"] = out
        elif key == "verify":
            out = step_verify(cfg, papers, self.ctx.get("hypothesis"),
                              self.ctx.get("experiment"), self.ctx.get("computation"),
                              emit, iteration=self.iteration,
                              goal=self.plan.get("topic") or "")
            self.ctx["verify"] = out
        elif key == "synthesize":
            out = step_synthesize(cfg, papers, emit,
                                 sections=self.plan.get("sections"),
                                 stop=self.should_stop,
                                 hypothesis=self.ctx.get("hypothesis"),
                                 experiment=self.ctx.get("experiment"),
                                 computation=self.ctx.get("computation"),
                                 verify=self.ctx.get("verify"))
            self.ctx["synth"] = out
        elif key == "format":
            out = step_format(cfg, papers, self.plan, emit,
                              synth=self.ctx.get("synth"),
                              stats=self.ctx.get("stats"),
                              hypothesis=self.ctx.get("hypothesis"),
                              experiment=self.ctx.get("experiment"),
                              computation=self.ctx.get("computation"),
                              verify=self.ctx.get("verify"),
                              iterations=self.iterations)
            self.ctx["format"] = out
        else:
            out = None
        return out

    def run(self, start_from="search", skip=None, previous=None, previous_state=None):
        """从 start_from 开始跑。

        previous       之前跑出来的上下文（papers/stats/synth…），续跑时直接复用
        previous_state 之前各步骤的状态，续跑时铺回去，
                       否则快照里"上一轮已经跑完的步骤"会退回 pending，
                       前端看到的时间线会突然少掉半截。
        """
        skip = set(skip or [])
        self.ctx.update(previous or {})
        for k, v in (previous_state or {}).items():
            if isinstance(v, dict) and v.get("status") not in (None, "pending"):
                self.state[k] = dict(v)
        start_idx = _STEP_INDEX.get(start_from, 0)

        idx = start_idx
        while idx < len(STEPS):
            step = STEPS[idx]
            key = step["key"]
            if key in skip:
                self.state[key] = {"status": "skipped", "detail": "已跳过"}
                self.on_step(key, "skipped", "已跳过")
                idx += 1
                continue
            if self.should_stop():
                # 中断语义（全流程只认一个断点）：
                #   * 已经跑完的步骤保持 done —— 成果不丢；
                #   * 断点就是"本来要开始、但被拦下"的这一步。
                self.state[key] = {"status": "interrupted", "detail": "用户中断，从此处继续"}
                self.on_step(key, "interrupted", "用户中断，从此处继续")
                return self.state

            self.on_step(key, "running", "开始")
            broke_here = False
            out = None

            def emit(detail, _key=key):
                # emit 只负责"汇报进度"，不负责判断中断 ——
                # 早期版本在这里顺手查 should_stop 并报 interrupted，
                # 结果同一次中断被报了两次（步骤自己一次、循环顶部一次）。
                self.state.setdefault(_key, {})["detail"] = detail
                self.on_step(_key, "running", detail)

            try:
                out = self._run_step(key, emit)
            except Exception as e:  # noqa: BLE001 - 单步失败不拖垮整条流水线
                self.state[key] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                self.on_step(key, "failed", str(e))
            else:
                # 步骤自己在批次边界发现中断时，会在产物里带 interrupted=True。
                # 这时断点就是这一步本身，直接返回，不要再标"下一步"。
                if isinstance(out, dict) and out.get("interrupted"):
                    broke_here = True
                    self.state[key] = {"status": "interrupted", "detail": "用户中断，从此处继续"}
                    self.on_step(key, "interrupted", "用户中断，从此处继续")
                else:
                    detail = self.state.get(key, {}).get("detail", "")
                    state_item = {"status": "done", "detail": detail, "output": out}
                    if key in ("hypothesis", "experiment", "compute", "verify"):
                        state_item["iteration"] = self.iteration
                    self.state[key] = state_item
                    # 一定要报 done：外部只认 on_step 回调，
                    # 不报的话这一步在库里永远停在 running，续跑也会从错误位置开始。
                    self.on_step(key, "done", detail)

            if broke_here:
                return self.state

            # ── 「验证—迭代」回环 ─────────────────────────────
            if key == "verify" and self.state.get(key, {}).get("status") == "done":
                verdict = (out or {}).get("verdict")
                action = "通过，进入撰写" if verdict == "pass" else ""
                self.iterations.append({
                    "iteration": self.iteration + 1,
                    "hypotheses": len((self.ctx.get("hypothesis") or {}).get("hypotheses") or []),
                    "computations": (self.ctx.get("computation") or {}).get("count", 0),
                    "verdict": verdict,
                    "replay": (out or {}).get("replay"),
                    "action": action,
                })
                may_iterate = verdict == "revise" or (verdict == "reject" and ITERATE_ON_REJECT)
                if may_iterate and self.iteration < self.max_iterations:
                    self.iteration += 1
                    self.ctx["feedback"] = {
                        "verdict": verdict,
                        "fixes": (out or {}).get("fixes") or [],
                        "open_questions": (out or {}).get("open_questions") or [],
                        "checks": (out or {}).get("checks") or [],
                    }
                    self.iterations[-1]["action"] = f"返工（第 {self.iteration + 1} 轮）"
                    self.on_step("hypothesis", "running",
                                 f"第 {self.iteration + 1} 轮迭代：按审稿意见返工")
                    idx = _STEP_INDEX["hypothesis"]
                    continue
                if verdict == "reject" and not ITERATE_ON_REJECT:
                    self.iterations[-1]["action"] = "结论被否决，未返工（reject 重跑同流程无意义，已如实写入报告）"
                elif verdict in ("revise", "reject"):
                    self.iterations[-1]["action"] = "未收敛（已达迭代上限），如实报告"
                elif not action:
                    self.iterations[-1]["action"] = "通过"
                if self.ctx.get("feedback") and verdict == "pass":
                    self.ctx.pop("feedback", None)
            idx += 1

        return self.state

    def snapshot(self):
        """给前端/数据库用的可序列化状态。papers 只保留必要的展示字段，避免过大。"""
        out = {
            "plan": self.plan,
            "steps": [],
            "iteration": self.iteration,
            "iterations": self.iterations,
        }
        papers = self._papers()
        for step in STEPS:
            key = step["key"]
            st = self.state.get(key) or {"status": "pending"}
            item = {
                "key": key, "title": step["title"], "core": step["core"],
                "detail_text": step["detail"],
                "status": st.get("status", "pending"),
            }
            if st.get("detail"):
                item["progress"] = st["detail"][:200]
            if st.get("error"):
                item["error"] = st["error"][:300]
            if st.get("iteration"):
                item["iteration"] = st["iteration"]
            out["steps"].append(item)

        out["paperCount"] = len(papers)
        out["papers"] = [{
            "id": p.get("id"), "title": p.get("title"), "authors": (p.get("authors") or [])[:6],
            "year": p.get("year"), "venue": p.get("journal") or p.get("venue"),
            "sourceLabel": p.get("sourceLabel"), "citationCount": p.get("citationCount"),
            "doi": p.get("doi"), "url": p.get("url"), "openAccessPdf": p.get("openAccessPdf"),
            "relevance": p.get("relevance"), "extract": p.get("extract") or {},
        } for p in papers]
        if self.ctx.get("stats"):
            out["stats"] = self.ctx["stats"]
        if self.ctx.get("hypothesis"):
            out["hypothesis"] = self.ctx["hypothesis"]
        if self.ctx.get("experiment"):
            out["experiment"] = self.ctx["experiment"]
        if self.ctx.get("computation"):
            comp = dict(self.ctx["computation"])
            # 计算产物可能很大；快照里只留前 8 条，报告里是完整的
            comp["computations"] = (comp.get("computations") or [])[:8]
            out["computation"] = comp
        if self.ctx.get("verify"):
            out["verify"] = self.ctx["verify"]
        if self.ctx.get("synth"):
            out["synth"] = self.ctx["synth"]
        if self.ctx.get("format"):
            out["references"] = self.ctx["format"]["references"]
            out["report"] = self.ctx["format"]["report"]
        return out


def step_titles():
    return [{"key": s["key"], "title": s["title"], "core": s["core"],
             "detail": s["detail"]} for s in STEPS]
