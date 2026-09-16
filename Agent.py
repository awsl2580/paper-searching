"""学术文献检索助手 —— 命令行版

通过命令行交互方式搜索学术论文、获取详细信息。
使用 Semantic Scholar API（免费无需 API Key）+ DeepSeek 大模型。
"""

import json
import os
import sys
import subprocess
from dotenv import load_dotenv

load_dotenv()

# ── 自动检查并安装依赖 ────────────────────────────────────
REQUIRED_PACKAGES = {
    "langchain": "langchain",
    "langchain_deepseek": "langchain-deepseek",
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
    except subprocess.CalledProcessError as e:
        print(f"❌ 安装失败，请手动执行: pip install {' '.join(missing)}")
        sys.exit(1)

import requests
from langchain.agents import create_agent
from langchain_deepseek import ChatDeepSeek
from langchain_core.tools import tool

# ── API 配置 ──────────────────────────────────────────────

# ── 系统提示词 ────────────────────────────────────────────
# 注意：CLI 版没有 Web 的"论文卡片"，所以这里要求正文把作者/年份/摘要/链接都写全；
# Web 版（app.py）反过来要求正文只列标题，避免和卡片重复。两边刻意不同，别改齐。
DEFAULT_SYSTEM_PROMPT = """你是一个专业的学术文献检索助手。你的任务是帮助用户查找和了解学术论文。

检索结果来自多个数据源（Semantic Scholar、OpenAlex），
每条结果里都有 sourceLabel 标明来源、language 标明语种（en/zh）。

你可以使用以下工具：
- search_papers：搜索学术论文，返回论文列表（含标题、作者、年份、摘要概要、来源）
- get_paper_detail：获取论文详细信息（含完整摘要、引用数、发表期刊/会议等）

使用规则：
1. 当用户询问某个领域或主题的论文时，先用 search_papers 搜索
2. search_papers 的 source 参数控制范围：all=中英文（默认）、cn=仅中文、en=仅英文。
   用户说"中文文献/国内研究"就用 cn，说"英文文献/外文"就用 en
3. 把结果以简洁的编号列表呈现，每条包含：标题、作者、年份、来源标注、简短摘要、原文链接，
   来源标注写成【来源：OpenAlex】这样的形式
   - 原文链接直接写完整 URL（取自结果的 url 字段），有 openAccessPdf 就再附上 PDF 链接
   - 链接只能照抄 url / openAccessPdf 字段，不要自己拼造或改写 URL；为空就写"该源未提供链接"
4. warnings 字段非空时（某个源本次检索失败），只用半句话在末尾带一句，不要单独写一段数据源状态说明
5. 当用户说"看第X篇"或"详细介绍某篇"时，把该篇的 id 字段原样传给 get_paper_detail；
   如果用户直接给了论文标题，也可以直接查
6. 不要编造检索结果里没有的论文、作者、引用数或链接
7. 回复时请用中文，保持专业且友好的学术交流风格"""


# ── 工具定义 ──────────────────────────────────────────────
# 检索逻辑与 Web 版共用 providers 多源聚合层
# （节流 + 指数退避 + 缓存 + 跨源去重），避免两边行为漂移。
import providers


@tool
def search_papers(query: str, limit: int = 5, source: str = "all", min_relevance: int = 0) -> str:
    """搜索学术论文。

    参数:
        query: 搜索关键词（中文主题用中文，英文主题用英文）
        limit: 返回数量（默认5，最多10）
        source: 'all'=中英文全部(默认)、'cn'=仅中文文献、'en'=仅英文文献
        min_relevance: 相关度下限 0-100，默认 0（只剔除查询词完全没命中的）
    """
    return providers.search_papers(query, limit, source, min_relevance)


@tool
def get_paper_detail(paper_id: str) -> str:
    """获取论文详细信息。参数 paper_id=检索结果里的 id 字段，也可以是论文标题。"""
    return providers.get_detail(paper_id)


# ── 构建 Agent ────────────────────────────────────────────
if not os.getenv("DEEPSEEK_API_KEY"):
    raise RuntimeError("DEEPSEEK_API_KEY 未设置。请设置环境变量或在 .env 文件中配置。")

# 模型名可用 DEEPSEEK_MODEL 覆盖（默认 deepseek-chat，与 Web 版一致）。
# 说明见 app.py 里 DEEPSEEK_MODEL 的注释：该别名当前由 DeepSeek-V4.1-Flash
# 承接，且走非思考模式，因此不会有收不到的 reasoning_content。
DEEPSEEK_MODEL = (os.getenv("DEEPSEEK_MODEL") or "").strip() or "deepseek-chat"

llm = ChatDeepSeek(model=DEEPSEEK_MODEL, temperature=0)
agent = create_agent(
    model=llm,
    tools=[search_papers, get_paper_detail],
    system_prompt=DEFAULT_SYSTEM_PROMPT,
)


# ── 流式输出 ──────────────────────────────────────────────
def stream_and_collect(question: str, history: list[dict]) -> str:
    """流式输出并返回完整回复文本."""
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
                if event.get("event") == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
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

    except Exception as e:
        print(f"[stream fallback] {type(e).__name__}: {e}", file=sys.stderr)

    # 回退
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
    history: list[dict] = []

    print("=" * 50)
    print("   📚 学术文献检索助手（命令行版）")
    print("   输入论文主题或关键词开始检索")
    print("   输入 exit / quit 退出")
    print("=" * 50)

    while True:
        try:
            question = input("\n🔍 请输入关键词或问题：\n").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            print("再见！")
            break

        answer_text = stream_and_collect(question, history)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer_text})

if __name__ == "__main__":
    main()