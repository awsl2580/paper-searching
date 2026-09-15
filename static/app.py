"""学术文献检索助手 —— Web 服务入口

启动时自动检查并安装缺失依赖。
"""

import os
import sys
import subprocess
import json
import re
import threading
import time
from datetime import datetime

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
from flask import Flask, Response, jsonify, request, session
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

# ── Semantic Scholar 访问层 ───────────────────────────────
# 节流 / 指数退避 / 缓存 / 可选 API Key 都封装在 s2_client 里，
# CLI 版 Agent.py 共用同一份实现，避免两边逻辑漂移。
# 关键结论（实测）：不配 API Key 时所有人共用公共通道，/paper/search 高峰期必被限流；
# 配置 SEMANTIC_SCHOLAR_API_KEY 可获得独立配额。详见 s2_client.py 顶部注释。
import s2_client


DEFAULT_SYSTEM_PROMPT = """你是一个专业的学术文献检索助手。你的任务是帮助用户查找和了解学术论文。

你可以使用以下工具：
- search_papers：根据关键词搜索学术论文，返回论文列表（含标题、作者、年份、摘要概要）
- get_paper_detail：获取某篇论文的详细信息（含完整摘要、引用数、发表期刊/会议等）

使用规则：
1. 当用户询问某个领域或主题的论文时，先用 search_papers 搜索
2. 将搜索到的论文以简洁的编号列表呈现给用户，每条包含：标题、作者、年份、简短摘要
3. 回复时请用中文，保持专业且友好的学术交流风格
4. 工具返回 JSON。如果结果是 {"error": "rate_limited", ...}，说明检索接口正在限流
   （这不是服务故障，也不是接口下线）。此时请：
   - 直接把 message 和 suggestion 里的内容转述给用户，不要自己编造"服务不可用/接口故障"之类的结论
   - 可以主动再调用一次 search_papers 重试（相同检索会命中本地缓存，重试成本很低）
   - 如果重试仍被限流，建议用户配置 SEMANTIC_SCHOLAR_API_KEY，或过一会儿再问
   - 不要连续反复重试同一个查询超过两次"""


@tool
def search_papers(query: str, limit: int = 5) -> str:
    """搜索学术论文。参数: query=搜索关键词（英文最佳），limit=返回数量（默认5，最多10）。"""
    return s2_client.search_papers(query, limit)


@tool
def get_paper_detail(paper_id: str) -> str:
    """获取论文详细信息。"""
    if not paper_id or not paper_id.strip():
        return json.dumps({"error": "请提供论文ID或标题"}, ensure_ascii=False)

    pid = paper_id.strip()

    if len(pid) == 40 and all(c in "0123456789abcdef" for c in pid.lower()):
        return s2_client.get_paper_by_id(pid)
    return s2_client.get_paper_by_title(pid)


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
    """添加收藏 - 简化版"""
    data = request.get_json(force=True, silent=True) or {}
    paper_id = data.get('paper_id', '').strip()
    paper_title = data.get('paper_title', '').strip()

    if not paper_title:
        return jsonify({"error": "缺少论文标题"}), 400

    if not paper_id or paper_id.startswith('title_'):
        clean_title = re.sub(r'[《》\s]', '_', paper_title)
        paper_id = 'title_' + clean_title[:50]

    success = db.add_favorite(session['user_id'], paper_id, paper_title, '', '')
    if success:
        return jsonify({
            "success": True,
            "message": "已收藏",
            "paper": {
                "title": paper_title
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


def _extract_answer(result):
    """从 Agent 返回值里取出最后一条助手回复"""
    msgs = result.get("messages") if isinstance(result, dict) else None
    if msgs:
        last = msgs[-1]
        content = getattr(last, "content", None)
        if content is None and isinstance(last, dict):
            content = last.get("content")
        return str(content) if content else "抱歉，未能获取到回复"
    return str(getattr(result, "content", result))


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

    if not message:
        return jsonify({"error": "message is required"}), 400

    messages = []
    for m in history:
        role = m.get("role")
        content = m.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    user_id = session.get('user_id') if 'user_id' in session else None

    # 登录用户：本轮对话写入哪个会话（流式开始前就定下来）
    conversation_id = _resolve_conversation(user_id, _parse_conversation_id(data.get("conversation_id")), message)

    def sse_pack(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def generate():
        import time

        if conversation_id:
            yield sse_pack({"type": "conversation_id", "conversation_id": conversation_id}).encode("utf-8")

        try:
            result = AGENT.invoke({"messages": messages})
        except Exception as e:
            yield sse_pack({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"}).encode("utf-8")
            yield sse_pack({"type": "done"}).encode("utf-8")
            return

        answer = _extract_answer(result)
        msgs = result.get("messages") if isinstance(result, dict) else None

        if user_id:
            _persist_turn(user_id, conversation_id, message, answer, len(msgs) if msgs else 0)

        chunk_size = 3
        for i in range(0, len(answer), chunk_size):
            token = answer[i:i + chunk_size]
            yield sse_pack({"type": "token", "token": token}).encode("utf-8")
            time.sleep(0.02)

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
