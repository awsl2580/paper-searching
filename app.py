"""学术文献检索助手 —— Web 服务入口

启动时自动检查并安装缺失依赖。
"""

import os
import sys
import subprocess
import json
import re
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
from flask import Flask, Response, jsonify, request, session, stream_with_context, send_from_directory
from functools import wraps

from langchain_deepseek import ChatDeepSeek

# 自动加载 .env（如果存在）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
S2_USER_AGENT = os.getenv("S2_USER_AGENT", "LiteratureSearchAssistant/1.0 (personal research tool)")

# ── 对话模型 ───────────────────────────────────────────────
# 默认 "deepseek-chat" 是 DeepSeek 的稳定别名，不是固定到某一代模型：
# 实测 2026-09 它由 DeepSeek-V4.1-Flash 承接（/models 只返回
# deepseek-flash 与 deepseek-v4-pro 两个正式名）。
#
# 为什么默认用这个别名而不是直接写 "deepseek-flash"：
# 这个别名走的恰好是**非思考模式**（实测 reasoning_content 为空），
# 而 V4.1-Flash 默认开思考、会产生 reasoning_content。本项目只在 /api/search
# 里做一次"查询理解"，只读 content；开思考的话推理内容拿不到、只是白等
# 并多付推理 token 的钱，对"提取关键词"这种任务毫无收益。
#
# 想换模型只改 .env 里的 DEEPSEEK_MODEL，不用动代码：
#   deepseek-v4-pro        能力更强，贵约 3~5 倍，不支持 vision
#   deepseek-flash         正式名，会开思考（需另行处理推理内容）
#   deepseek-reasoner      旧 R1 别名，实测同样由 V4.1-Flash 承接且支持工具调用
DEEPSEEK_MODEL = (os.getenv("DEEPSEEK_MODEL") or "").strip() or "deepseek-chat"

# 导入数据库模块
import database as db

# ── 静态文件：白名单，不做整目录托管 ──────────────────────
# 原来写的是 Flask(__name__, static_folder="static", static_url_path="")，
# 那会把整个 static/ 目录挂到根路径的通配路由 /<path:filename> 上——
# 目录里放什么就能下载什么。实测当时 static/app.py（一份 561 行的旧服务端
# 副本）可以直接 GET /app.py 拿到 200 + 21715 字节源码，app.js / styles.css
# 同理。这几个文件已经删掉，但根因是"整目录托管"这个配置本身：
# 以后任何人往里丢一个 .py / .bak / .sql，都会立刻变成公开可下载的文件。
#
# 现在改成只服务前端真正需要的 index.html（前端是单文件，内联 CSS/JS，
# 不引用任何外部资源），其余一律 404。
STATIC_FILES = {"index.html"}
app = Flask(__name__, static_folder=None)
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


# ── 检索意图理解 ──────────────────────────────────────────
# 界面是搜索框而不是对话框，所以这里不需要 Agent 的工具循环：
# 只需要一次模型调用，把自然语言查询变成"关键词 + 检索范围"，
# 然后直接交给 providers 去检索。比原来跑 Agent 快得多，也便宜得多。
#
# 注意：模型只负责理解意图，**不参与渲染**。结果列表里的标题、作者、
# 年份、被引、链接全部来自 providers 的结构化数据，模型碰不到，
# 所以不存在"编造一篇不存在的论文"的问题。
EXTRACT_PROMPT = """你是学术检索的查询理解模块。把用户输入转成检索参数，只输出 JSON。

输出字段：
- "query"：真正用于检索的关键词。中文主题用中文，英文主题用英文；
  去掉"帮我找""我想看""有没有"这类口语词；保留专业术语和限定词
  （例如年份、方法名、综述/review）。如果用户输入本身就是关键词，原样返回。
- "source"："all" | "cn" | "en"。用户明确说"中文文献/国内研究"给 "cn"，
  说"英文文献/外文/SCI"给 "en"，否则一律 "all"。
- "limit"：想要的条数，1-10 之间的整数，没提就给 10。
- "note"：一句话说明你怎么理解这次检索（给用户看的中文短句，20 字以内）。
  例如"按关键词检索：图神经网络 综述"。
- "plan"：一句话说明你的检索策略（给用户看，25 字以内）。
  例如"先按主题词检索，兼顾中英文来源"或"用户要综述，优先找 review 类文献"。

只输出 JSON，不要任何解释文字或 markdown 代码块。"""

# 智能体"反思后重规划"用的提示词：上一轮结果不合格时，换一个策略
REPLAN_PROMPT = """你是学术检索智能体。上一轮检索的结果不理想，请换一个检索策略重试。

你会收到：上一轮的检索词、检索范围、失败原因、以及已经拿到的部分标题。
要求：
- 换一个**不同角度**的表达，不要只是把原词删掉一两个词。
  可用的策略：改用同义术语或英文对应词、去掉过窄的限定词只留核心概念、
  换成更通用的上位概念、补充该领域公认的方法名。
- 如果失败原因是"跑题"，说明原词太宽泛或字面匹配到了无关领域，
  应该换成更精确的术语。
- 如果失败原因是"结果太少"，说明原词太窄，应该放宽。

输出 JSON：
- "query"：新的检索词，必须和上一轮不同
- "source"："all" | "cn" | "en"，拿不准就保持原来的
- "thought"：一句话说明为什么这样改（给用户看，35 字以内）

只输出 JSON。"""

# 智能体"归纳"用的提示词：读懂检索结果，给出要点与后续方向
SYNTHESIS_PROMPT = """你是学术检索智能体。用户提了一个需求，系统检索到了一批文献，
请你读懂这批结果，给用户一个有用的导读。

严格约束（必须遵守）：
1. **只能依据下面给出的文献**。不要引入列表之外的论文、作者、年份或引用数。
2. 不要编造任何数字。列表里没有的信息就说"文献未提供"。
3. 如果结果与用户需求不太匹配，要**如实说明**，不要硬凑。
4. 中文回答。

输出 JSON：
- "summary"：2-4 句话的导读。说清楚这批文献大致覆盖什么方向、
  彼此是什么关系（同一路线 / 不同方法 / 综述与实证之分）、
  有没有明显的空白或分歧。不要逐条复述标题。
- "highlights"：最多 3 条要点，每条一句话。
  可以用"第1篇…"指代，但不要只复述标题 —— 要说出它的特点
  （例如"第2篇是综述，适合先建立整体认识"）。
- "followups"：最多 3 条**用户可能想接着搜的方向**，每条是一个可以直接拿去检索的
  短语（中英文按主题定），不要是问句。例如"图神经网络 可解释性"。
- "caveat"：一句话说明这批结果的局限（例如"中文文献覆盖有限，建议同时看英文"）。
  没有明显局限就给空字符串。

只输出 JSON。"""


def _extract_search_params(message):
    """把用户的自然语言查询解析成检索参数（智能体的"规划"步骤）。

    模型不可用或返回不合法 JSON 时，退化成"原样当关键词检索"——
    搜索框必须始终能用，不能因为一次模型调用失败就搜不了。
    """
    fallback = {
        "query": (message or "").strip(),
        "source": "all",
        "limit": 10,
        "note": "按关键词检索：" + (message or "").strip()[:20],
        "plan": "直接按输入的关键词检索",
    }
    if not fallback["query"]:
        return fallback

    parsed = _call_json(EXTRACT_PROMPT, message, max_tokens=320)
    if not parsed:
        return fallback

    query = str(parsed.get("query") or "").strip() or fallback["query"]
    source = str(parsed.get("source") or "all").strip().lower()
    if source not in ("all", "cn", "en"):
        source = "all"
    try:
        limit = int(parsed.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = min(max(1, limit), 10)
    note = str(parsed.get("note") or "").strip()[:40] or fallback["note"]
    plan = str(parsed.get("plan") or "").strip()[:60] or fallback["plan"]

    return {"query": query, "source": source, "limit": limit, "note": note, "plan": plan}


def _strip_json_fence(text):
    """模型偶尔仍会包一层 ```json ... ```，这里容错剥掉。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _has_cjk(text):
    """检索词里有没有汉字 —— 用来决定「全部」该走哪条检索分支。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _core_term(query):
    """取检索词里最核心的一段，用于结果太差时重试。

    中文技术检索经常是混合写法（"图神经网络 GNN 综述"），而拉丁词才是
    最有辨识度的那部分，所以优先返回最长的英文/数字串；
    没有拉丁词时才取最长的连续汉字片段
    （"图神经网络 综述" -> "图神经网络"）。
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", query or "")
    if words:
        return max(words, key=len)
    runs = re.findall(r"[\u4e00-\u9fff]+", query or "")
    return max(runs, key=len) if runs else ""


def _looks_off_topic(papers, query):
    """判断结果是不是"跑题"了（而不只是"没结果"）。

    为什么不能只看"有没有结果"：providers 的相关度是按 2-gram 覆盖率算的，
    "图神经网络 综述" 会命中《基于BP神经网络的股票价格预测研究》
    —— 因为标题里有"神经网络"，分数不为 0，于是过滤不会触发。
    但这类结果对用户毫无价值。

    这里用更贴题的标准：检索词里**最长的中文片段**是否出现在靠前结果里。
    实测 "图神经网络 综述" 的前三名标题都不含"图神经网络"，判定跑题；
    而 "乡村振兴 农村经济" 的前三名标题都含"乡村振兴"，判定正常。
    只对中文片段做这个判断（英文侧由 S2 的相关度排序兜着，本来就比较准）。
    """
    runs = re.findall(r"[\u4e00-\u9fff]+", query or "")
    core = max(runs, key=len) if runs else ""
    if len(core) < 3 or not papers:
        return False
    for p in papers[:3]:
        title = p.get("title") or ""
        if core in title:
            return False
        # 也接受"核心词组被拆开但主要部分命中"的情况，避免误判
    return True


def _compact_papers(papers, title_chars=90, abstract_chars=180):
    """把结果压成给模型看的最小字段，避免把整篇摘要塞进上下文。

    只给模型"它需要用来判断和归纳"的东西；列表渲染仍然走完整的 papers，
    模型碰不到那些字段。
    """
    out = []
    for p in papers or []:
        out.append({
            "title": (p.get("title") or "")[:title_chars],
            "abstract": (p.get("abstract") or "")[:abstract_chars],
            "year": p.get("year"),
            "venue": (p.get("journal") or p.get("venue") or "")[:40],
            "citations": p.get("citationCount") or 0,
            "source": p.get("sourceLabel") or p.get("source") or "",
            "relevance": p.get("relevance"),
        })
    return out


def _judge_results(papers, query, limit):
    """智能体的"观察"步骤：这次检索到底够不够好？

    用便宜的本地规则判断，而不是再调一次模型 —— 判断标准是可解释的，
    也省掉一次往返。返回 (是否够好, 给模型看的原因)。
    """
    if not papers:
        return False, "一条结果都没有"

    reasons = []
    got = len(papers)

    # 1. 数量够不够。
    #    阈值要跟着 limit 走：limit=5 时只返回 2 条，完全可能是语料本身就只有
    #    这么多，这时再触发一轮重检索纯属浪费（还要多花一次模型调用）。
    floor = 2 if limit <= 5 else 3
    if got < min(floor, limit):
        reasons.append(f"只拿到 {got} 条，偏少")

    # 2. 是否跑题（核心中文片段没出现在靠前标题里）
    if _looks_off_topic(papers, query):
        core = _core_term(query) or query
        reasons.append(f"靠前结果的标题里没有「{core}」，很可能跑题")

    # 3. 平均相关度
    scores = [p.get("relevance") for p in papers if isinstance(p.get("relevance"), (int, float))]
    if scores and sum(scores) / len(scores) < 35:
        reasons.append(f"平均相关度只有 {round(sum(scores) / len(scores))}")

    # 4. 是不是只有一个源在供给（另一个源可能被限流了）
    srcs = {p.get("source") for p in papers if p.get("source")}
    if len(srcs) == 1 and got >= 3:
        reasons.append("只有一个数据源返回了结果")

    # 第 4 条单独出现不算失败（中文检索本来就只有 OpenAlex）
    blocking = [r for r in reasons if "只有一个数据源" not in r]
    if blocking:
        return False, "；".join(reasons)
    return True, f"{got} 条，覆盖正常"


def _should_keep_old(new_payload, old_papers):
    """第二次检索是否比第一次更好？不好就保留旧的，别越搜越差。"""
    new_papers = (new_payload or {}).get("papers") or []
    if not new_papers:
        return False
    new_avg = _avg_relevance(new_papers)
    old_avg = _avg_relevance(old_papers)
    # 结果更多，或者量差不多但平均相关度明显更高
    if len(new_papers) > len(old_papers):
        return True
    return new_avg > old_avg + 10


def _avg_relevance(papers):
    scores = [p.get("relevance") for p in papers if isinstance(p.get("relevance"), (int, float))]
    return sum(scores) / len(scores) if scores else 0


def _make_llm(max_tokens=400):
    """统一的模型构造：强制 JSON 输出、温度为 0。"""
    return ChatDeepSeek(
        model=DEEPSEEK_MODEL,
        temperature=0,
        max_tokens=max_tokens,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def _call_json(prompt, user_content, max_tokens=400):
    """调一次模型并解析 JSON；任何失败都返回 None（由调用方决定降级策略）。"""
    try:
        resp = _make_llm(max_tokens).invoke([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        parsed = json.loads(_strip_json_fence(raw))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _replan(query, reason, papers, source):
    """智能体的"反思后重规划"：上一轮不够好时，让模型换一个检索策略。

    给它的信息包括"为什么不够好"和"已经拿到什么"，避免它给出重复的策略。
    """
    parsed = _call_json(REPLAN_PROMPT, json.dumps({
        "上一轮检索词": query,
        "上一轮范围": source,
        "问题": reason,
        "已拿到的标题": [p.get("title", "")[:60] for p in (papers or [])[:5]],
    }, ensure_ascii=False), max_tokens=300)

    if not parsed:
        return None
    new_query = str(parsed.get("query") or "").strip()
    if not new_query or new_query == query:
        return None
    new_source = str(parsed.get("source") or source).strip().lower()
    if new_source not in ("all", "cn", "en"):
        new_source = source
    return {
        "query": new_query,
        "source": new_source,
        "thought": str(parsed.get("thought") or "").strip()[:60],
    }


def _synthesize(message, papers, query):
    """智能体的"归纳"步骤：读懂结果，给出要点与后续方向。

    硬性约束：只能依据给定结果，不许出现列表之外的论文/作者/数字。
    这一步纯属附加价值 —— 失败了也只是少一块内容，不影响结果列表。
    """
    parsed = _call_json(SYNTHESIS_PROMPT, json.dumps({
        "用户原始需求": message,
        "实际检索词": query,
        "检索到的文献": _compact_papers(papers),
    }, ensure_ascii=False), max_tokens=700)
    if not parsed:
        return None

    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        return None

    def _str_list(key, cap=3):
        raw = parsed.get(key)
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip()[:120])
            elif isinstance(item, dict):
                t = item.get("text") or item.get("question") or ""
                if str(t).strip():
                    out.append(str(t).strip()[:120])
            if len(out) >= cap:
                break
        return out

    return {
        "summary": summary[:600],
        "highlights": _str_list("highlights"),
        "followups": _str_list("followups"),
        "caveat": str(parsed.get("caveat") or "").strip()[:160],
    }


# ═══════════════════════════════════════════════════════════════
#  路由：静态页面（白名单见 STATIC_FILES）
# ═══════════════════════════════════════════════════════════════

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/index.html")
def index_alias():
    """旧版把整个 static/ 挂在根路径上，所以 /static/index.html 曾经也能打开。
    保留这个别名只为不破坏别人可能存过的书签，仍然只放行这一个白名单文件。"""
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_whitelist(filename):
    """只放行白名单里的静态文件；其余（例如误放进去的 .py/.bak/.db）一律 404。

    前端是单文件应用（内联 CSS/JS，无外部引用），所以这个白名单只有一项。
    send_from_directory 自身会拒绝越权路径，这里的成员判断是第二道闸。
    """
    if filename not in STATIC_FILES:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(STATIC_DIR, filename)


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

# ═══════════════════════════════════════════════════════════════
#  路由：会话 / 对话记录（登录后可用）
# ═══════════════════════════════════════════════════════════════

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
        llm = ChatDeepSeek(model=DEEPSEEK_MODEL, temperature=0.3)

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


@app.post("/api/search")
def search():
    """检索接口 —— 一个受控的智能体循环。

    四步（每一步都往前端推 agent_step 事件，界面能看到它在做什么）：
        1. 规划   理解需求，产出检索词 / 范围 / 策略说明
        2. 检索   直连 providers 多源并发检索
        3. 观察   自评结果够不够好；不够就用**不同角度**重规划再检索一轮（最多 1 次）
        4. 归纳   读懂结果给出导读、要点、可继续检索的方向

    为什么这样设计而不是让模型自由调工具：
    检索路径保持确定、可控（最多 2 轮、每轮都有超时与降级），
    模型只在"规划 / 评估后的重规划 / 归纳"这三处发挥判断力。
    这也保证了**模型编不出论文**：列表数据始终来自 providers。

    任何一步失败都不会让搜索不可用：
    规划失败 -> 原样当关键词；重规划失败 -> 沿用第一轮结果；
    归纳失败 -> 只是少一块导读，结果列表照常返回。

    SSE 事件：
        agent_step  智能体的阶段动作（前端渲染成步骤条）
        query       最终采用的检索参数
        papers      结构化结果
        synthesis   导读 / 要点 / 后续方向
        warn        某个数据源本轮失败
        result      结果统计
        error       出错
        done        结束
    """
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    source_pref = (data.get("source_pref") or "").strip().lower()

    if not message:
        return jsonify({"error": "请输入检索内容"}), 400

    try:
        limit = min(max(1, int(data.get("per_page") or data.get("limit") or 20)), 50)
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(data.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    page = offset // limit + 1

    # 是否要跑"归纳"这一步。关掉可以省一次模型调用（例如压测时）。
    want_synthesis = data.get("synthesis", True) is not False

    def sse_pack(obj):
        # SSE 事件之间用空行分隔
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def step(phase, text, status="run"):
        return sse_pack({"type": "agent_step", "phase": phase,
                         "text": text, "status": status}).encode("utf-8")

    def generate():
        # 翻第 2 页以后：检索词与来源沿用上次，不再重规划，归纳也不必重做
        # （内容没变）—— 省掉两次模型调用，翻页要快。
        is_paged = offset > 0

        # ── 1. 规划 ────────────────────────────────────────────────
        if is_paged:
            yield step("plan", f"继续查看「{message}」第 {page} 页", "done")
            source = source_pref if source_pref in ("all", "cn", "en") else "all"
            params = {"query": message, "source": source, "limit": limit,
                      "note": "", "plan": ""}
            if source == "all":
                source = "cn" if _has_cjk(params["query"]) else "en"
            params["source"] = source
        else:
            yield step("plan", "理解检索需求", "run")
            params = _extract_search_params(message)
            source = source_pref if source_pref in ("all", "cn", "en") else params["source"]
            # 「全部」再按检索词语言收窄：providers 的 "all" 是"英文源 + OpenAlex 中文过滤"
            # 两路并发，英文检索词下那路中文纯属噪音（实测查 graph neural network 会
            # 返回《光学神经网络及其应用》）。中文词走 cn、英文词走 en。
            if source == "all":
                source = "cn" if _has_cjk(params["query"]) else "en"
            params["source"] = source
            params["limit"] = limit
            yield step("plan", params.get("plan") or "已确定检索策略", "done")
        yield sse_pack({"type": "query", **params}).encode("utf-8")

        # 排序说明：结果按「相关度 -> 被引/年份综合分」降序（打分函数在 providers 里）
        if not is_paged:
            yield step("rank", "按相关度 + 被引/年份排序", "done")

        # ── 2. 检索 ────────────────────────────────────────────────
        # 让聚合层直接把 [offset, offset+limit) 这一段取回来 —— 不要在路由里
        # 拿 offset=0 的一整段再切片：那样第 2 页会切到空（踩过这个坑）。
        # 翻页时窗口就是本页大小，避免为第 N 页重复拉前面的内容。
        window = limit

        def fetch(q, src, win, off=0):
            """取回 [off, off+win) 这段；异常收敛成错误 payload，不抛出。"""
            try:
                pl = json.loads(providers.search_papers(q, win, src, 0, off))
            except Exception as e:  # noqa: BLE001 - 任何异常都不该让搜索框失效
                return {"error": "exception",
                        "message": f"{type(e).__name__}: {str(e)[:300]}"}, []
            return pl, (pl.get("papers") or [])

        yield step("search", f"检索「{params['query']}」", "run")
        payload, papers = fetch(params["query"], source, window, offset)
        if payload.get("error"):
            yield sse_pack({
                "type": "error",
                "message": payload.get("message") or payload["error"],
                "retryable": payload.get("retryable", False),
                "suggestion": payload.get("suggestion", ""),
            }).encode("utf-8")
            yield sse_pack({"type": "done"}).encode("utf-8")
            return

        used_query = params["query"]
        used_source = source
        warnings = list(payload.get("warnings") or [])
        yield step("search", f"取回 {len(papers)} 条（第 {page} 页）", "done")

        # ── 3. 观察 + 重规划（只在第一页做）──────────────────────────
        if is_paged:
            enough, reason = True, ""
        else:
            enough, reason = _judge_results(papers, used_query, limit)
            if enough:
                yield step("observe", f"结果评估：{reason}", "done")
            else:
                yield step("observe", f"结果不够理想：{reason}", "warn")

                core = _core_term(used_query)
                plan2 = None
                if core and core != used_query:
                    # 先试最便宜的一招：回退到核心词（不必花一次模型调用）
                    plan2 = {"query": core, "source": used_source,
                             "thought": f"原词过窄，退回核心概念「{core}」"}
                else:
                    yield step("replan", "换个角度重新组织检索词", "run")
                    plan2 = _replan(used_query, reason, papers, used_source)

                if plan2:
                    if plan2.get("thought"):
                        yield step("replan", plan2["thought"], "run")
                    yield step("search", f"重新检索「{plan2['query']}」", "run")
                    # 换词后从第 1 页重新取（offset 归零）
                    payload2, papers2 = fetch(plan2["query"], plan2["source"], window, 0)

                    if _should_keep_old(payload2, papers):
                        papers = papers2
                        used_query = plan2["query"]
                        used_source = plan2["source"]
                        warnings += list(payload2.get("warnings") or [])
                        enough2, reason2 = _judge_results(papers, used_query, limit)
                        yield step("search", f"取回 {len(papers)} 条", "done")
                        yield step("observe", f"结果评估：{reason2}", "done" if enough2 else "warn")
                        # 检索词变了，把最终参数补报给前端
                        yield sse_pack({"type": "query", "query": used_query, "source": used_source,
                                        "limit": limit, "note": params.get("note", ""),
                                        "plan": plan2.get("thought") or params.get("plan", ""),
                                        "revised": True}).encode("utf-8")
                    else:
                        yield step("replan", "新策略没有更好，沿用第一轮结果", "done")
                    yield step("replan", "新策略没有更好，沿用第一轮结果", "done")

        sources = sorted({p.get("sourceLabel") for p in papers if p.get("sourceLabel")})
        if papers:
            yield sse_pack({"type": "papers", "papers": papers}).encode("utf-8")

        for w in warnings:
            yield sse_pack({"type": "warn", "text": w}).encode("utf-8")

        # ── 4. 归纳（只在第一页做：翻页内容相同，重做纯属浪费）────────
        synthesis = None
        if papers and want_synthesis and not is_paged:
            yield step("synthesize", f"阅读这 {len(papers)} 篇并归纳", "run")
            synthesis = _synthesize(message, papers, used_query)
            yield step("synthesize",
                       "已生成导读" if synthesis else "结果已返回（导读生成失败）",
                       "done" if synthesis else "warn")
        if synthesis:
            yield sse_pack({"type": "synthesis", **synthesis}).encode("utf-8")

        # 分页信息：total_is_partial 表示"这一批取到的条数"，
        # 不是数据源报告的总命中数（那需要额外一次请求，不值当）。
        yield sse_pack({
            "type": "result",
            "total": len(papers),
            "page": page,
            "per_page": limit,
            "offset": offset,
            "has_more": len(papers) == limit,
            "is_last_page": len(papers) < limit,
            "sources": sources,
            "filtered_out": payload.get("filtered_out") or 0,
            "query": used_query,
        }).encode("utf-8")
        yield sse_pack({"type": "done"}).encode("utf-8")

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(generate(), headers=headers, mimetype="text/event-stream",
                    direct_passthrough=True)


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
