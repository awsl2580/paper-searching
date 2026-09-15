"""学术文献检索助手 —— Web 服务入口

启动时自动检查并安装缺失依赖。
"""

import os
import sys
import subprocess
import json
import re
import queue
import asyncio
import threading
import time
from datetime import datetime
from urllib.parse import quote

# ── 自动检查并安装依赖 ────────────────────────────────────
REQUIRED_PACKAGES = {
    "flask": "flask>=3.0.0",
    "langchain": "langchain",
    "langchain_deepseek": "langchain-deepseek",
    "langchain_core": "langchain-core",
    "requests": "requests>=2.28.0",
    "dotenv": "python-dotenv>=1.0.0",
    "bcrypt": "bcrypt>=4.0.0",
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

# ── 第三方导入 ────────────────────────────────────────────
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, Response, jsonify, request, session, stream_with_context
from functools import wraps

from langchain.agents import create_agent
from langchain_deepseek import ChatDeepSeek
from langchain_core.tools import tool

# 自动加载 .env（如果存在）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
S2_USER_AGENT = os.getenv("S2_USER_AGENT", "LiteratureSearchAssistant/1.0 (personal research tool)")

# 导入数据库模块
import database as db

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key-change-this-in-production")

# ── 登录验证装饰器 ────────────────────────────────────────
def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "请先登录"}), 401
        return f(*args, **kwargs)
    return decorated_function

# ── 文献数据源：多源聚合层 ────────────────────────────────
# 数据源不是单一的 Semantic Scholar，而是 providers/ 下的多源聚合：
#   semantic_scholar  英文，免费公共配额（高峰期会限流）
#   openalex          英+中，完全免费无需 key，中文靠 filter=language:zh
# 节流 / 指数退避 / 缓存 / 跨源去重都在 providers 里，CLI 版 Agent.py 共用同一份。
# 换源或加源只需要新增 providers/xxx.py 并在注册表里加一行，本文件不用动。
import providers


DEFAULT_SYSTEM_PROMPT = """你是一个专业的学术文献检索助手。你的任务是帮助用户查找和了解学术论文。

你的检索结果来自多个数据源（Semantic Scholar、OpenAlex），
每条结果里都有 sourceLabel 字段标明来源，language 字段标明语种（en/zh）。

你可以使用以下工具：
- search_papers：搜索学术论文，返回论文列表（含标题、作者、年份、摘要概要、来源、链接）
- get_paper_detail：按论文 id（或标题）获取详细信息（含完整摘要、引用数、发表期刊等）

使用规则：
1. 当用户询问某个领域或主题的论文时，先用 search_papers 搜索
2. search_papers 的 source 参数控制检索范围：
   - "all" 中文 + 英文（默认）
   - "cn" 仅中文文献（用户说"中文文献""国内研究"时用）
   - "en" 仅英文文献（用户说"英文文献""外文"时用）
3. 【最重要】检索结果会由界面自动渲染成"论文卡片"显示在回答下方，
   卡片里已经包含作者、年份、期刊、被引次数、摘要、来源标签、
   原文链接、PDF 链接，以及「收藏」「详情」按钮。
   所以你的正文**绝对不要重复这些字段**，只写两部分：

   (a) 开头一句话概括：主题、共几篇，必要时点出覆盖的方向或年份跨度；
   (b) 一个「编号. 标题」的清单，每条只占一行。

   正文里不要写作者、期刊、摘要、被引次数、链接、来源标注。
   这份清单必须保留，因为用户下一轮会说"看第 2 篇的详细信息"，
   而对话上下文里只有你的正文，没有卡片数据。

   反例（不要这样写——这些信息卡片里全都有，写一遍就是重复）：
     "1. 乡村振兴背景下货币政策… 作者：唐文婷｜2022｜摘要：利用向量自回归模型…
      链接：https://doi.org/10.xxxx"
   正例：
     "1. 乡村振兴背景下货币政策的农业产业发展效应探究"
4. 当用户说"看第X篇的详细信息"时，把该篇的 id 字段原样传给 get_paper_detail。
   这种情况下没有卡片，你可以把完整摘要、引用数、期刊、链接都写清楚。
5. 只有在用户**明确索要链接**时，才在正文里写出 URL。这时要分清楚两个字段：
   - url = 网页地址（来源页 / DOI 页），点开是普通网页，能直接在浏览器里浏览
   - openAccessPdf = 完整论文的 PDF 直链；有些期刊点开会**直接下载文件**
   用户说"只是想看看 / 浏览一下"时给 url；明确说"要全文 / 要 PDF"时给 openAccessPdf。
   两个都照抄，**绝对不要自己拼造、改写或猜测 URL**（尤其是 DOI）；
   字段为空就如实说明"该源未提供"，不要编一个出来
6. 如果结果是 {"error": "rate_limited", ...}，说明数据源被限流了（这不是故障）：
   - 直接把 message 和 suggestion 里的内容转述给用户，不要自己编造"服务不可用"之类的结论
   - 可以再调用一次 search_papers 重试（相同检索会命中本地缓存，重试成本很低）
   - 不要连续反复重试同一个查询超过两次
7. 如果结果里的 warnings 字段非空（某个源本次检索失败、结果不完整），
   只用半句话在末尾带过，例如"（Semantic Scholar 本次被限流，以上为 OpenAlex 来源）"。
   不要单独写一段数据源状态说明，更不要把"未配置/未启用"这类部署信息讲给用户听。
8. 回复时请用中文，保持专业且友好的学术交流风格
9. 不要编造检索结果里没有的论文、作者、引用数或链接
10. 界面会自动把检索结果汇总成"精读文献表"（含相关度打分、可导出 CSV），
    并支持一键生成 GB/T 7714 / APA / BibTeX 三种参考文献格式。
    所以：用户要参考文献格式时，不必手工排版，告诉他去「📊 文献表」面板切换格式即可；
    用户嫌结果太杂/要"高相关的"时，用 search_papers 的 min_relevance 参数（给 40-60）收紧，
    不要自己在正文里人工剔除。"""


@tool
def search_papers(query: str, limit: int = 5, source: str = "all", min_relevance: int = 0) -> str:
    """搜索学术论文。

    参数:
        query: 搜索关键词（中文主题用中文，英文主题用英文）
        limit: 返回数量（默认5，最多10）
        source: 检索范围，'all'=中英文全部(默认)、'cn'=仅中文文献、'en'=仅英文文献
        min_relevance: 相关度下限 0-100。默认 0（只剔除查询词完全没命中的）。
                       用户要求"高相关""严格筛选""精读文献表"时给 40-60。

    返回的每篇论文里：
        relevance      相关度分（0-100，标题命中权重更高）
        openAccessPdf  全文 PDF 直链（能读到完整论文）
        url            来源页/DOI 页（付费论文只有摘要页）
        volume/issue/pages  卷期页码（生成参考文献格式要用）
    """
    return providers.search_papers(query, limit, source, min_relevance)


@tool
def get_paper_detail(paper_id: str) -> str:
    """获取论文详细信息。参数 paper_id 可以是检索结果里的 id 字段，也可以是论文标题。"""
    return providers.get_detail(paper_id)


def build_agent():
    llm = ChatDeepSeek(model="deepseek-chat", temperature=0)
    return create_agent(
        model=llm,
        tools=[search_papers, get_paper_detail],
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )


AGENT = build_agent()


# ═══════════════════════════════════════════════════════════════
#  路由：静态页面
# ═══════════════════════════════════════════════════════════════

@app.get("/")
def index():
    return app.send_static_file("index.html")


# ═══════════════════════════════════════════════════════════════
#  路由：用户认证
# ═══════════════════════════════════════════════════════════════

@app.post("/api/register")
def register():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    email = data.get('email', '').strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 3:
        return jsonify({"error": "用户名至少3个字符"}), 400
    if len(password) < 8 or len(password) > 16:
        return jsonify({"error": "密码需为8-16位"}), 400
    if not re.search(r'[a-z]', password) or not re.search(r'[A-Z]', password) or not re.search(r'\d', password):
        return jsonify({"error": "密码需包含大写字母、小写字母和数字"}), 400

    success, message = db.register_user(username, password, email)
    if success:
        return jsonify({"success": True, "message": message})
    else:
        return jsonify({"error": message}), 400


@app.post("/api/login")
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    user = db.login_user(username, password)
    if user:
        session['user_id'] = user['id']
        session['username'] = user['username']
        return jsonify({
            "success": True,
            "user": {"id": user['id'], "username": user['username']}
        })
    else:
        return jsonify({"error": "用户名或密码错误"}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"success": True, "message": "已登出"})


@app.get("/api/me")
def get_current_user():
    if 'user_id' in session:
        user = db.get_user_by_id(session['user_id'])
        if user:
            return jsonify({"success": True, "user": user})
    return jsonify({"success": False, "user": None})


# ═══════════════════════════════════════════════════════════════
#  路由：搜索历史
# ═══════════════════════════════════════════════════════════════

@app.get("/api/history")
@login_required
def get_history():
    history = db.get_search_history(session['user_id'], 20)
    return jsonify({"success": True, "history": history})


@app.delete("/api/history")
@login_required
def clear_history():
    db.clear_search_history(session['user_id'])
    return jsonify({"success": True, "message": "历史已清空"})


# ═══════════════════════════════════════════════════════════════
#  路由：会话 / 对话记录（登录后可用）
# ═══════════════════════════════════════════════════════════════

@app.get("/api/conversations")
@login_required
def list_conversations():
    """当前用户的会话列表"""
    conversations = db.get_conversations(session['user_id'])
    return jsonify({"success": True, "conversations": conversations})


@app.post("/api/conversations")
@login_required
def new_conversation():
    """新建会话（标题可用首条消息自动生成）"""
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get('title') or '新对话').strip() or '新对话'
    conversation_id = db.create_conversation(session['user_id'], title)
    if not conversation_id:
        return jsonify({"error": "创建会话失败"}), 500
    return jsonify({"success": True, "conversation": {"id": conversation_id, "title": title[:50]}})


@app.get("/api/conversations/<int:conversation_id>")
@login_required
def read_conversation(conversation_id):
    """读取某个会话的全部消息"""
    conversation, messages = db.get_conversation_messages(session['user_id'], conversation_id)
    if not conversation:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"success": True, "conversation": conversation, "messages": messages})


@app.delete("/api/conversations/<int:conversation_id>")
@login_required
def remove_conversation(conversation_id):
    """删除某个会话"""
    if db.delete_conversation(session['user_id'], conversation_id):
        return jsonify({"success": True, "message": "已删除"})
    return jsonify({"error": "会话不存在"}), 404


@app.delete("/api/conversations")
@login_required
def clear_all_conversations():
    """清空当前用户的全部会话"""
    db.clear_conversations(session['user_id'])
    return jsonify({"success": True, "message": "已清空所有对话记录"})


# ═══════════════════════════════════════════════════════════════
#  路由：收藏（简化版）
# ═══════════════════════════════════════════════════════════════

@app.post("/api/favorites")
@login_required
def add_favorite():
    """添加收藏"""
    data = request.get_json(force=True, silent=True) or {}
    paper_id = data.get('paper_id', '').strip()
    paper_title = data.get('paper_title', '').strip()
    # 作者/年份：前端从结构化检索结果里带过来，让收藏列表有出处信息
    authors = str(data.get('authors') or '').strip()
    year = str(data.get('year') or '').strip()

    if not paper_title:
        return jsonify({"error": "缺少论文标题"}), 400

    if not paper_id or paper_id.startswith('title_'):
        clean_title = re.sub(r'[《》\s]', '_', paper_title)
        paper_id = 'title_' + clean_title[:50]

    success = db.add_favorite(session['user_id'], paper_id, paper_title, authors, year)
    if success:
        return jsonify({
            "success": True,
            "message": "已收藏",
            "paper": {
                "title": paper_title,
                "authors": authors,
                "year": year,
            }
        })
    else:
        return jsonify({"error": "已收藏过"}), 400


@app.delete("/api/favorites/<path:paper_id>")
@login_required
def remove_favorite(paper_id):
    """取消收藏"""
    db.remove_favorite(session['user_id'], paper_id)
    return jsonify({"success": True, "message": "已取消收藏"})


@app.delete("/api/favorites")
@login_required
def clear_favorites():
    """清空所有收藏"""
    db.clear_favorites(session['user_id'])
    return jsonify({"success": True, "message": "已清空所有收藏"})


@app.get("/api/favorites")
@login_required
def get_favorites():
    """获取收藏列表"""
    favorites = db.get_favorites(session['user_id'])
    return jsonify({"success": True, "favorites": favorites})


@app.get("/api/favorites/<path:paper_id>")
@login_required
def check_favorite(paper_id):
    """检查是否已收藏"""
    is_fav = db.is_favorited(session['user_id'], paper_id)
    return jsonify({"success": True, "is_favorited": is_fav})


# ═══════════════════════════════════════════════════════════════
#  路由：论文摘要润色
# ═══════════════════════════════════════════════════════════════

@app.post("/api/polish")
@login_required
def polish_abstract():
    """AI 润色论文摘要"""
    data = request.get_json(force=True, silent=True) or {}
    abstract = data.get('abstract', '').strip()
    style = data.get('style', 'academic')

    if not abstract:
        return jsonify({"error": "请提供需要润色的摘要内容"}), 400
    if len(abstract) < 20:
        return jsonify({"error": "摘要内容太短，至少需要20个字符"}), 400
    if len(abstract) > 5000:
        return jsonify({"error": "摘要内容过长，请控制在5000字符以内"}), 400

    style_prompts = {
        'academic': '请将以下论文摘要润色为更学术、更正式的风格。使用学术写作的常用表达，增强逻辑性和严谨性。保持原意不变，不要添加新的内容。',
        'concise': '请将以下论文摘要精简，保留核心信息，删除冗余表达，使内容更加紧凑有力。',
        'clear': '请将以下论文摘要改写得更清晰易懂，使非专业读者也能理解主要内容。使用更简洁、流畅的语言。'
    }

    prompt = style_prompts.get(style, style_prompts['academic'])

    system_prompt = f"""你是一位专业的学术论文编辑，擅长润色和改进学术写作。

{prompt}

输出格式要求：
1. 直接输出润色后的摘要内容
2. 不需要任何解释或说明
3. 保持原意的完整性和准确性
4. 使用中文输出

请润色以下摘要："""

    try:
        llm = ChatDeepSeek(model="deepseek-chat", temperature=0.3)

        response = llm.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"原始摘要：\n{abstract}"}
        ])

        polished = response.content

        return jsonify({
            "success": True,
            "original": abstract,
            "polished": polished,
            "style": style,
            "word_count_original": len(abstract),
            "word_count_polished": len(polished)
        })

    except Exception as e:
        return jsonify({"error": f"润色失败: {str(e)}"}), 500


# ═══════════════════════════════════════════════════════════════
#  路由：聊天
# ═══════════════════════════════════════════════════════════════

def _parse_conversation_id(value):
    """把前端传来的 conversation_id 规整成 int 或 None"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _papers_from_tool_output(content):
    """从工具返回的 JSON 字符串里取出结构化论文列表。

    （工具返回 {"papers": [...]}，这里直接解析，前端不再靠正则从
    Markdown 里猜标题 —— 那样一篇结果只能猜出一篇，也拿不到作者/年份。）
    """
    if not isinstance(content, str) or '"papers"' not in content:
        return []
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("papers"), list):
        return [p for p in data["papers"] if isinstance(p, dict)]
    return []


def _pdf_filename(paper):
    """给下载的 PDF 起一个人能看懂的名字：年份_第一作者_标题.pdf

    那些期刊给的原始文件名是 OJS 生成的编号（`1609-1936-1-PB.pdf`），
    存下来完全认不出是哪篇，所以这里重命名。

    必须清洗 Windows 不允许的字符（\\ / : * ? " < > |），否则保存会直接失败；
    标题也截断，避免路径过长。
    """
    def safe(text, limit):
        text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or ""))
        text = re.sub(r"\s+", " ", text).strip(" ._")
        return text[:limit]

    title = safe(paper.get("title") or "paper", 80) or "paper"
    year = safe(paper.get("year"), 8)
    if year == "未知":
        year = ""
    authors = paper.get("authors") or []
    first_author = safe(authors[0], 16).replace(" ", "") if authors else ""

    parts = [p for p in (year, first_author) if p]
    parts.append(title)
    return "_".join(parts) + ".pdf"


def _resolve_conversation(user_id, requested_id, question):
    """在流式响应之前就确定本轮要写入哪个会话，并返回该会话 id。

    - 请求里的会话不存在或不属于该用户 -> 新建一个
    - 没有请求会话 -> 新建一个（标题取首条消息）
    提前决定很关键：否则 SSE 会先把别的会话 id 告诉前端，导致下一轮又开新会话。
    """
    if not user_id:
        return None

    if requested_id:
        _, existing = db.get_conversation_messages(user_id, requested_id)
        if existing:
            return requested_id
    try:
        return db.create_conversation(user_id, question[:30])
    except Exception:
        return None


def _persist_turn(user_id, conversation_id, question, answer, results_count=0):
    """把一轮问答写进搜索历史和会话（任一失败都不影响聊天本身）"""
    try:
        db.save_search_history(user_id, question, results_count)
    except Exception:
        pass

    if not user_id or not conversation_id:
        return
    try:
        db.append_message(user_id, conversation_id, "user", question)
        db.append_message(user_id, conversation_id, "assistant", answer)
    except Exception:
        pass


@app.post("/api/chat")
def chat():
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    source_pref = (data.get("source_pref") or "all").strip().lower()

    if not message:
        return jsonify({"error": "message is required"}), 400

    messages = []
    for m in history:
        role = m.get("role")
        content = m.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str):
            messages.append({"role": role, "content": content})

    # 前端的"文献来源"开关：以系统消息的形式带进本轮上下文。
    # 不拼进用户消息里，是因为那样会被模型当成用户说的话，也会污染保存下来的对话记录。
    if source_pref in ("cn", "en"):
        scope = "仅中文文献（source='cn'）" if source_pref == "cn" else "仅英文文献（source='en'）"
        messages.insert(0, {
            "role": "system",
            "content": f"用户当前的文献来源偏好是：{scope}。调用 search_papers 时必须使用这个 source 取值。",
        })

    messages.append({"role": "user", "content": message})

    user_id = session.get('user_id') if 'user_id' in session else None

    # 登录用户：本轮对话写入哪个会话（流式开始前就定下来）
    conversation_id = _resolve_conversation(user_id, _parse_conversation_id(data.get("conversation_id")), message)

    def sse_pack(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def generate():
        # ── 为什么用"工作线程 + 队列"而不是直接 invoke ──────────────
        # 原来用 AGENT.invoke()：整段答案生成完才开始切片下发，
        # 用户要盯着一个空泡泡等十几秒（学术接口还经常要退避重试），
        # 看起来就像卡死了。改成 astream_events 之后可以：
        #   1. 一边生成一边吐字（真流式）
        #   2. 在调用工具时发一个 status 事件，让前端显示"正在检索…"
        # 但 astream_events 是 async 的，而 Flask 的生成器是同步的，
        # 所以让它在独立线程里跑，事件经 Queue 交给生成器转成 SSE。
        if conversation_id:
            yield sse_pack({"type": "conversation_id", "conversation_id": conversation_id}).encode("utf-8")

        yield sse_pack({"type": "status", "text": "正在思考…"}).encode("utf-8")

        events = queue.Queue()
        state = {"tool_used": False, "answer": "", "papers": []}

        async def run_agent():
            try:
                async for ev in AGENT.astream_events({"messages": messages}, version="v2"):
                    kind = ev.get("event")

                    if kind == "on_tool_start":
                        # 工具调用前模型常会先说一句"我先搜索一下"（还可能是英文），
                        # 那不是给用户看的答案，丢掉它并切到"检索中"状态
                        state["tool_used"] = True
                        state["answer"] = ""
                        events.put(("status", "正在检索文献源…"))

                    elif kind == "on_tool_end":
                        output = (ev.get("data") or {}).get("output")
                        content = getattr(output, "content", None)
                        if content is None and isinstance(output, str):
                            content = output
                        papers = _papers_from_tool_output(content)
                        if papers:
                            state["papers"].extend(papers)
                            events.put(("papers", list(state["papers"])))
                        events.put(("status", "正在阅读检索结果并生成回答…"))

                    elif kind == "on_chat_model_stream":
                        chunk = (ev.get("data") or {}).get("chunk")
                        text = getattr(chunk, "content", None)
                        if text:
                            state["answer"] += text
                            # 有工具调用时，工具之后的内容才是答案，直接边生成边发
                            if state["tool_used"]:
                                events.put(("token", text))

                events.put(("done", None))
            except Exception as e:  # noqa: BLE001 - 异常要送回生成器，不能只留在子线程里
                events.put(("error", f"{type(e).__name__}: {str(e)[:500]}"))

        threading.Thread(target=lambda: asyncio.run(run_agent()), daemon=True).start()

        # 主线程只做一件事：把队列里的事件转成 SSE。带总超时，避免永久挂住
        deadline = time.time() + 240
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                yield sse_pack({"type": "error", "message": "检索超时（超过 240 秒），请重试"}).encode("utf-8")
                break
            try:
                kind, value = events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

            if kind == "done":
                break
            if kind == "error":
                yield sse_pack({"type": "error", "message": value}).encode("utf-8")
                break
            if kind == "status":
                yield sse_pack({"type": "status", "text": value}).encode("utf-8")
            elif kind == "papers":
                yield sse_pack({"type": "papers", "papers": value}).encode("utf-8")
            elif kind == "token":
                yield sse_pack({"type": "token", "token": value}).encode("utf-8")

        answer = state["answer"] or "抱歉，未能获取到回复"

        # 没有调用工具的情况（闲聊类问题）：token 没被实时发出去，这里补发
        if not state["tool_used"]:
            for i in range(0, len(answer), 3):
                yield sse_pack({"type": "token", "token": answer[i:i + 3]}).encode("utf-8")

        if user_id:
            # results_count 记的是"这一轮检索到几篇论文"，不是消息条数
            _persist_turn(user_id, conversation_id, message, answer, len(state["papers"]))

        yield sse_pack({"type": "done"}).encode("utf-8")

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        generate(),
        headers=headers,
        mimetype="text/event-stream",
        direct_passthrough=True,
    )


@app.get("/api/sources")
def list_sources():
    """当前可用的文献数据源（前端据此显示来源开关的提示）。"""
    return jsonify({"success": True, "sources": providers.source_status()})


@app.get("/api/paper/<path:paper_id>")
def paper_detail(paper_id):
    """按论文 id 取详情。

    单独开这个接口而不是让前端再走一轮 LLM：点"详情"只是取数据，
    不该花一次模型调用，也不该被大模型的幻觉影响。
    """
    try:
        payload = json.loads(providers.get_detail(paper_id))
    except Exception as e:
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500

    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "返回内容无法解析"}), 502
    if payload.get("error"):
        return jsonify({"success": False, "error": payload.get("message") or payload["error"],
                        "retryable": payload.get("retryable", False)}), 502
    # 成功时 payload 就是论文本身；没找到时是 {"message": ..., "paper": None}
    # 所以判断依据是"有没有 title"，不能去看有没有 paper 键
    if not payload.get("title"):
        return jsonify({"success": False, "error": payload.get("message", "未找到该论文")}), 404

    return jsonify({"success": True, "paper": payload})


@app.get("/api/paper/<path:paper_id>/pdf")
def paper_pdf(paper_id):
    """把论文的开放获取 PDF 转发下来，并**由我们用可读的文件名**保存。

    为什么要做这个转发：那些期刊的 PDF 直链是 OJS 生成的
    （`48962-Article Text-60922-1-10-20241217.pdf`），下载到本地完全看不出是哪篇。
    而浏览器会**忽略跨域链接上的 HTML download 属性**（这是安全限制，改不了），
    所以只能由同源的我们自己把文件取回来、再设置 Content-Disposition。

    安全上有意收紧：**只使用我们自己数据源里记录的 openAccessPdf**，
    不接受任何调用方传入的 URL —— 否则这就成了一个可以被拿来抓任意地址的代理。
    """
    try:
        paper = json.loads(providers.get_detail(paper_id))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"获取论文信息失败：{type(e).__name__}"}), 500

    if not isinstance(paper, dict) or paper.get("error"):
        return jsonify({"error": (paper or {}).get("message", "未找到该论文")}), 404
    # 成功时 payload 就是论文本身；没找到时是 {"message":..., "paper": None}
    if not paper.get("title"):
        return jsonify({"error": paper.get("message", "未找到该论文")}), 404

    url = (paper.get("openAccessPdf") or "").strip()
    if not url:
        return jsonify({"error": "该论文没有可下载的全文 PDF 直链，请点「阅读」打开来源页"}), 404
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "全文链接格式无效"}), 400

    filename = _pdf_filename(paper)
    try:
        upstream = requests.get(
            url, stream=True, timeout=(10, 120),
            headers={"User-Agent": S2_USER_AGENT, "Accept": "application/pdf,*/*"},
        )
    except requests.RequestException as e:
        return jsonify({"error": f"下载失败：{type(e).__name__}，请稍后重试或点「阅读」"}), 502

    if upstream.status_code != 200:
        code = upstream.status_code
        upstream.close()
        return jsonify({"error": f"来源站返回 {code}，请点「阅读」打开原页面"}), 502

    ctype = (upstream.headers.get("Content-Type") or "").lower()
    if "pdf" not in ctype and "octet-stream" not in ctype:
        upstream.close()
        return jsonify({
            "error": f"这个链接不是 PDF（{ctype or '未知类型'}），请点「阅读」打开原页面",
        }), 415

    # Content-Disposition 里非 ASCII 文件名要用 RFC 5987 的 filename*，
    # 同时给一个 ASCII 的 filename 兜底（老浏览器只认前者）
    disposition = (
        'attachment; filename="paper.pdf"; '
        "filename*=UTF-8''" + quote(filename)
    )

    def stream():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(stream()),
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": disposition,
            # 给前端/调试看最终用了什么文件名
            "X-Paper-Filename": quote(filename),
            "Cache-Control": "no-store",
        },
        direct_passthrough=True,
    )


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        db.init_db()
        print("✅ 数据库已就绪")
    except Exception as e:
        print(f"⚠️ 数据库初始化警告: {e}")

    if not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未设置。请在系统环境变量中设置，或在项目根目录创建 .env 文件并写入：\n"
            "DEEPSEEK_API_KEY=sk-xxx"
        )

    print("=" * 50)
    print("   📚 学术文献检索助手")
    print(f"   🌐 http://127.0.0.1:8000")
    print("=" * 50)

    app.run(host="127.0.0.1", port=8000, debug=True, threaded=True, use_reloader=False)
