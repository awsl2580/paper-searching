"""LangChain 工具定义 —— 智能体"手"的统一出口。

## 为什么从 app.py 里拆出来

原来 `search_papers` / `get_paper_detail` 直接定义在 `app.py` 里，
于是**只有 Web 版的对话能调工具**：工作流里的"计算员"、命令行版、
多智能体角色想要同一批工具，只能各自再抄一遍 —— 抄完就开始漂移
（参数校验不一致、错误形状不一致，最麻烦的是返回给模型的措辞不一致）。

拆到这里之后：

    agent_tools.py  ──►  app.py（对话智能体）
                    ──►  agents.py（多智能体科研团队）
                    ──►  Agent.py（命令行）

## 工具清单

| 工具 | 谁用 | 说明 |
|------|------|------|
| `search_papers` | 检索员 / 对话 / 命令行 | 多源并发检索（providers 聚合层） |
| `get_paper_detail` | 检索员 / 对话 | 按 id 或标题取详情 |
| `list_science_tools` | 计算员 | 列出 MCP 科学计算工具目录 |
| `run_science_tool` | 计算员 | 通过 **MCP 协议**调用一个科学计算工具 |
| `remember_note` | 所有角色 | 写一条长周期记忆（登录用户） |

## 关于 `run_science_tool` 的设计

MCP 那边有 20+ 个工具，逐个包成 LangChain tool 会让系统提示词里的工具清单
长到几千字，模型反而更容易选错。这里用**一个桥接工具 + 动态目录**：
`list_science_tools` 告诉模型有什么，`run_science_tool(name, arguments_json)`
负责真调用。目录文本由 MCP `tools/list` 现查，所以加了新工具不用改这个文件。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

import providers


# ═══════════════════════════════════════════════════════════════
#  文献检索
# ═══════════════════════════════════════════════════════════════

@tool
def search_papers(query: str, limit: int = 5, source: str = "all",
                  min_relevance: int = 0) -> str:
    """搜索学术论文（中英文均可）。

    参数:
        query: 搜索关键词（中文主题用中文，英文主题用英文，多个词用空格分隔）
        limit: 返回数量（默认 5，最多 50）
        source: 检索范围，'all'=中英文全部(默认)、'cn'=仅中文文献、'en'=仅英文文献
        min_relevance: 相关度下限 0-100。默认 0（只剔除查询词完全没命中的）。
                       用户要求"高相关""严格筛选"时给 40-60。

    返回的每篇论文里：
        relevance      相关度分（0-100，标题命中权重更高）
        openAccessPdf  全文 PDF 直链
        url            来源页/DOI 页
        volume/issue/pages  卷期页码（生成参考文献要用）
    """
    return providers.search_papers(query, limit, source, min_relevance)


@tool
def get_paper_detail(paper_id: str) -> str:
    """获取论文详细信息。参数 paper_id 可以是检索结果里的 id 字段，也可以是论文标题。"""
    return providers.get_detail(paper_id)


# ═══════════════════════════════════════════════════════════════
#  MCP 科学计算桥接
# ═══════════════════════════════════════════════════════════════

@tool
def list_science_tools(category: str = "") -> str:
    """列出可用的科学计算工具（统计检验 / 谱图解析 / 曲线拟合 / 分子描述符 / 实验设计）。

    参数:
        category: 可选过滤词，例如 "统计"、"谱"、"分子"、"设计"；留空返回全部。
    返回每个工具的名字、中文标题与用途。拿到名字后用 run_science_tool 调用。
    """
    import mcp_client
    try:
        tools = mcp_client.list_tools(refresh=True)
    except Exception as e:  # noqa: BLE001
        return f"科学计算工具不可用：{type(e).__name__}: {e}"
    cat = (category or "").strip()
    picked = []
    for t in tools:
        blob = (t.get("name", "") + t.get("title", "") + t.get("description", "")
                + " ".join(t.get("annotations", {}).get("tags") or []))
        if cat and cat not in blob:
            continue
        picked.append(t)
    if not picked:
        return f"没有匹配「{cat}」的工具。可用工具共 {len(tools)} 个。"
    out = [f"共 {len(picked)} 个工具："]
    for t in picked:
        schema = t.get("inputSchema") or {}
        params = ", ".join((schema.get("properties") or {}).keys())
        req = ", ".join(schema.get("required") or []) or "无必填"
        out.append(f"- {t['name']}：{t.get('title','')}。参数({params})；必填({req})。"
                   f"{(t.get('description') or '')[:160]}")
    out.append("调用方式：run_science_tool(tool_name=..., arguments_json='{...}')")
    return "\n".join(out)


@tool
def run_science_tool(tool_name: str, arguments_json: str = "{}") -> str:
    """通过 MCP 协议执行一个科学计算工具，返回真实计算结果（不是估计值）。

    可用的工具名见 list_science_tools。常用：
        stats_describe / stats_ttest / stats_anova / stats_correlation / stats_regression
        spectra_summary / spectra_peaks / spectra_smooth / spectra_snr
        curves_fit / curves_extrapolate
        molecule_parse / molecule_batch
        design_factorial / design_power / design_sweep / design_grid_search
        table_parse

    参数:
        tool_name: 工具名，例如 "stats_ttest"
        arguments_json: JSON 字符串，键名与该工具的 inputSchema 一致，
                        例如 '{"x": [1,2,3], "y": [2,3,4]}'

    返回里 ok=false 表示工具执行失败（参数不合法或数据退化），error 字段是原因；
    repro 字段记录了工具名与参数，报告里的"复现清单"用它。
    数值必须以此为准，**不要自己心算**。
    """
    import mcp_client
    if isinstance(arguments_json, dict):
        args = arguments_json
    else:
        text = (arguments_json or "").strip() or "{}"
        try:
            args = json.loads(text)
        except Exception:
            # 模型常传 Python 字面量（单引号），退一步救回来
            try:
                args = json.loads(text.replace("'", '"'))
            except Exception:
                return json.dumps({"ok": False,
                                   "error": "arguments_json 不是合法 JSON，请传 {} 形式"},
                                  ensure_ascii=False)
    if not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "arguments_json 必须是 JSON 对象"},
                          ensure_ascii=False)

    result = mcp_client.call_tool(tool_name, args)
    # 只把模型需要的部分给它：raw 里是 content + structuredContent 的重复副本，
    # 原样返回会让上下文里同一份结果出现两遍（实测能吃掉上千 token）。
    payload = {
        "tool": result.get("tool"),
        "ok": result.get("ok"),
        "error": result.get("error"),
        "result": (result.get("result") or {}).get("result")
                  if isinstance(result.get("result"), dict) else result.get("result"),
        "repro": (result.get("result") or {}).get("repro")
                 if isinstance(result.get("result"), dict) else None,
    }
    return json.dumps(payload, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  长周期记忆
# ═══════════════════════════════════════════════════════════════

def make_memory_tool(user_id):
    """造一个"记住这条结论"的工具。

    每次请求现造（和 `_make_research_tool` 同理）：要把 user_id 绑进闭包，
    用模块级变量会让两个用户同时聊天时互相写入对方的记忆。
    """
    @tool
    def remember_note(text: str, kind: str = "note") -> str:
        """把一条值得长期记住的结论写进研究记忆（跨会话可召回）。

        适用：用户明确说"记住…"、或对话里得出了可复用的结论/偏好/排除项。
        不适用：普通问答内容（记忆库不是聊天记录的副本）。

        参数:
            text: 要记住的内容，一句话说清（30-120 字最佳）
            kind: goal=研究目标 / finding=已确认结论 / excluded=已排除方向 /
                  open=开放问题 / preference=用户偏好 / note=备注
        """
        if not user_id:
            return "当前未登录，长周期记忆只在登录后可用。"
        import memory
        res = memory.remember(user_id, text, kind=kind)
        if not res.get("id"):
            return f"未写入：{res.get('reason') or '未知原因'}"
        return ("已记住。" if res.get("created") else "这条已存在，已提高它的权重。")

    return remember_note


# ═══════════════════════════════════════════════════════════════
#  工具箱
# ═══════════════════════════════════════════════════════════════

# 检索员 / 对话智能体的默认工具集
LITERATURE_TOOLS = [search_papers, get_paper_detail]
# 计算员 / 实验设计师的默认工具集
COMPUTE_TOOLS = [list_science_tools, run_science_tool]


def role_tools(role, user_id=None):
    """按角色给工具。role 见 agents.ROLES 里的 tools 字段。"""
    import agents
    spec = agents.role(role)
    names = spec.get("tools") or []
    table = {t.name: t for t in LITERATURE_TOOLS + COMPUTE_TOOLS}
    out = [table[n] for n in names if n in table]
    if spec.get("memory") and user_id:
        out.append(make_memory_tool(user_id))
    return out


__all__ = ["COMPUTE_TOOLS", "LITERATURE_TOOLS", "get_paper_detail", "list_science_tools",
           "make_memory_tool", "role_tools", "run_science_tool", "search_papers"]
