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
DEFAULT_SYSTEM_PROMPT = """你是一个专业的学术文献检索助手。你的任务是帮助用户查找和了解学术论文。

你可以使用以下工具：
- search_papers：根据关键词搜索学术论文，返回论文列表（含标题、作者、年份、摘要概要）
- get_paper_detail：获取某篇论文的详细信息（含完整摘要、引用数、发表期刊/会议等）

使用规则：
1. 当用户询问某个领域或主题的论文时，先用 search_papers 搜索
2. 将搜索到的论文以简洁的编号列表呈现给用户，每条包含：标题、作者、年份、简短摘要
3. 当用户说"看第X篇"或"详细介绍某篇"时，用 get_paper_detail 获取详细信息
4. 如果用户直接提供了论文标题，也可以用 get_paper_detail 直接查询
5. 回复时请用中文，保持专业且友好的学术交流风格"""


# ── 工具定义 ──────────────────────────────────────────────
# 检索逻辑与 Web 版共用 s2_client（节流 + 指数退避 + 缓存 + 可选 API Key）。
# 不配 SEMANTIC_SCHOLAR_API_KEY 时走公共通道，429 时请按提示配置 Key。
import s2_client


@tool
def search_papers(query: str, limit: int = 5) -> str:
    """搜索学术论文。参数: query=搜索关键词（英文效果最佳），limit=返回数量（默认5，最多10）。"""
    return s2_client.search_papers(query, limit)


@tool
def get_paper_detail(paper_id: str) -> str:
    """获取论文详细信息。参数: paper_id=论文ID（40位）或论文标题。"""
    if not paper_id or not paper_id.strip():
        return json.dumps({"error": "请提供论文ID或标题"}, ensure_ascii=False)

    pid = paper_id.strip()

    if len(pid) == 40 and all(c in "0123456789abcdef" for c in pid.lower()):
        return s2_client.get_paper_by_id(pid)
    return s2_client.get_paper_by_title(pid)


# ── 构建 Agent ────────────────────────────────────────────
if not os.getenv("DEEPSEEK_API_KEY"):
    raise RuntimeError("DEEPSEEK_API_KEY 未设置。请设置环境变量或在 .env 文件中配置。")

llm = ChatDeepSeek(model="deepseek-chat", temperature=0)
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