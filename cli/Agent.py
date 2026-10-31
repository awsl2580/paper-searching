"""DeepSeek AI4S 科学智能体 —— 命令行版

和 Web 版共用同一套底层：`llm.py`（DeepSeek 模型）+ `agent_tools.py`（工具）+
`providers/`（多源检索）+ MCP 科学计算（`mcp_client`）。

## 为什么还留着命令行版

两个实际原因：

1. **评委最容易验证**：不用起服务、不用登录，`python cli/Agent.py` 就能看到
   模型确实在调工具、算出来的数确实来自工具；
2. **排查问题快**：Web 版链路长（SSE / 队列 / 线程），出问题时用 CLI 跑一遍
   就能分清"是模型/工具的问题"还是"传输层的问题"。

## 与 Web 版的差异（刻意为之）

Web 版有论文卡片，所以正文只列标题；CLI 版没有卡片，
所以这里要求正文把作者/年份/摘要/链接都写全。两边提示词不同是有意的，别改齐。
"""

import os
import sys
import subprocess

# ── 目录整理后的路径兼容（必须先于项目内的 import）──────────────
# 本文件在 cli/ 下；项目模块分布在 core/、mcp/ 等目录，由根目录的 paths.py
# 统一加进 sys.path（内部仍保持 `import llm` 这种扁平写法）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import paths  # noqa: E402,F401

# 统一从 env_boot 加载 .env（绝对路径 + 幂等，见 core/env_boot.py 的说明）
from env_boot import load_project_env  # noqa: E402

load_project_env()

# ── 自动检查并安装依赖 ────────────────────────────────────
REQUIRED_PACKAGES = {
    "langchain": "langchain",
    "langchain_openai": "langchain-openai",
    "langchain_core": "langchain-core",
    "requests": "requests>=2.28.0",
    "dotenv": "python-dotenv>=1.0.0",
}

missing = []
for module_name, pip_name in REQUIRED_PACKAGES.items():
    try:
        __import__(module_name)
    except ImportError:
        missing.append(pip_name)

if missing:
    print(f"📦 检测到缺失依赖: {', '.join(missing)}")
    print("⏳ 正在自动安装...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q"] + missing,
            stdout=subprocess.DEVNULL,
        )
        print("✅ 依赖安装完成！\n")
    except subprocess.CalledProcessError:
        print(f"❌ 安装失败，请手动执行: pip install {' '.join(missing)}")
        sys.exit(1)

from langchain.agents import create_agent

import agent_tools
import llm

# ── 系统提示词 ────────────────────────────────────────────
DEFAULT_SYSTEM_PROMPT = """你是「DeepSeek AI4S 科学智能体」的命令行版，底层是 DeepSeek 大模型
（deepseek-V4.1-flash / deepseek-v4-pro）。你能搜文献、读数据、调科学计算工具、给研究结论。

【工具】
- search_papers(query, limit, source, min_relevance)：多源并发检索学术论文
- get_paper_detail(paper_id)：取完整摘要/被引/卷期页/全文链接
- list_science_tools(category)：列出可用的科学计算工具
- run_science_tool(tool_name, arguments_json)：通过 MCP 真的执行计算

【规则】
1. 问某个方向的论文时先 search_papers；中文主题用中文检索，必要时补一次英文。
2. 命令行**没有论文卡片**，所以正文要把每篇的标题、作者、年份、来源、摘要要点、
   原文链接都写全（链接只能照抄 url / openAccessPdf 字段，不许拼造）。
3. 凡是统计量、p 值、拟合参数、峰位、分子量，**必须**用 run_science_tool 算，
   不许自己心算或估计。工具返回 ok=false 时如实说明原因。
4. 用户给了一段数据（CSV/两列数字/SMILES）就按数据类型选工具：
   谱图先 spectra_smooth 再 spectra_peaks；两组比较先 stats_describe 再 stats_ttest
   或 stats_mannwhitney；分子结构交给 molecule_parse。
5. 不要编造检索结果里没有的论文、作者、数字或链接。
6. 中文回答，专业但平实。数值结论要写清"检验名 + 统计量 + p + 效应量 + n"。
"""


def build_agent():
    """按当前配置造一个带工具链的智能体。"""
    llm.require_llm()
    info = llm.model_info()
    tools = agent_tools.LITERATURE_TOOLS + agent_tools.COMPUTE_TOOLS
    return create_agent(
        # 命令行也走真流式（DeepSeek 的 flash 与 pro 都支持流式）
        model=llm.chat_model("chat", temperature=0, streaming=True),
        tools=tools,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    ), info


# ── 流式输出 ──────────────────────────────────────────────
def stream_and_collect(agent, question: str, history: list) -> str:
    """流式输出并返回完整回复文本。"""
    full_text_parts: list[str] = []
    printed_any = False

    try:
        import asyncio

        async def _run():
            nonlocal printed_any
            async for event in agent.astream_events(
                {"messages": history + [{"role": "user", "content": question}]},
                version="v2",
            ):
                kind = event.get("event")
                if kind == "on_tool_start":
                    print(f"\n  🛠 调用工具：{event.get('name')}", file=sys.stderr)
                    printed_any = True
                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    # 工具参数分片不是答案（Web 版踩过的坑，这里同样过滤）
                    if getattr(chunk, "tool_call_chunks", None):
                        continue
                    token = getattr(chunk, "content", None)
                    if token:
                        printed_any = True
                        full_text_parts.append(token)
                        sys.stdout.write(token)
                        sys.stdout.flush()

        asyncio.run(_run())

        if printed_any:
            print()
            return "".join(full_text_parts)

    except Exception as e:  # noqa: BLE001 - 流式失败就退回一次性调用
        print(f"[stream fallback] {type(e).__name__}: {e}", file=sys.stderr)

    result = agent.invoke({"messages": history + [{"role": "user", "content": question}]})
    messages = result.get("messages") if isinstance(result, dict) else None
    if messages:
        last_msg = messages[-1]
        content = getattr(last_msg, "content", None)
        if content is None and isinstance(last_msg, dict):
            content = last_msg.get("content")
        text = content if content is not None else str(last_msg)
    else:
        text = str(getattr(result, "content", result))
    print(text)
    return text


# ── 主循环 ────────────────────────────────────────────────
def main():
    agent, info = build_agent()
    history: list = []

    print("=" * 62)
    print("   🔬 DeepSeek AI4S 科学智能体（命令行版）")
    print(f"   🧠 模型后端：{info['backend_label']}")
    for role in info["roles"][:2]:
        print(f"      - {role['label']}: {role['model']}")
    print("   检索论文 / 让模型算一下 / 贴一段数据让它分析")
    print("   输入 exit / quit 退出")
    print("=" * 62)

    while True:
        try:
            question = input("\n🔍 请输入问题：\n").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            print("再见！")
            break

        answer_text = stream_and_collect(agent, question, history)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer_text})


if __name__ == "__main__":
    main()
