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
from urllib.parse import quote

# 先统一输出编码：Windows 控制台/重定向默认是 GBK，下面这些带 emoji 的
# 提示（依赖安装、启动横幅、数据库就绪）会抛 UnicodeEncodeError 把启动流程打断。
# 放在**导入阶段**而不是 __main__ 里，是因为 app 在导入时就会建表并打印。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
from flask import Flask, Response, jsonify, redirect, request, session, stream_with_context, send_from_directory
from functools import wraps

from langchain.agents import create_agent
from langchain_deepseek import ChatDeepSeek
from langchain_core.tools import tool

# 自动加载 .env（如果存在）
#
# 这里**必须用绝对路径**，不能用裸 load_dotenv()：
# 裸调用是按"当前工作目录"向上找 .env 的，从别处执行
# （例如 python C:\...\AIagent2\AIagent1\app.py，或在 IDE 里配了别的工作目录）
# 就会找不到，于是 API Key 缺失、会话密钥回退到公开的默认值 —— 而且不报错。
# 这个坑是接公网域名时踩出来的：换个目录启动，签名密钥就悄悄变了。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
S2_USER_AGENT = os.getenv("S2_USER_AGENT", "LiteratureSearchAssistant/1.0 (personal research tool)")

# 模型名统一从这里取（README「模型说明」里承诺的"改 .env 不用改代码"）。
# 当前用 deepseek-flash（DeepSeek-V4.1-Flash）。它**默认开思考**，
# 会先吐一大段 reasoning_content 再写正文，所以下面 on_chat_model_stream
# 那段专门处理了推理内容：推「模型正在思考…」状态给前端，
# 否则思考期间界面就是个空白泡泡（实测能长达 20 秒以上）。
DEEPSEEK_MODEL = (os.getenv("DEEPSEEK_MODEL") or "").strip() or "deepseek-flash"

# 导入数据库模块
import database as db

# ── 静态文件：白名单，不做整目录托管 ──────────────────────
# 原来写的是 Flask(__name__, static_folder="static", static_url_path="")，
# 那会把整个 static/ 目录挂到根路径的通配路由 /<path:filename> 上——
# 目录里放什么就能下载什么。实测当时 static/app.py（一份 561 行的旧服务端
# 副本）可以直接 GET /app.py 拿到 200 + 21715 字节源码，app.js / styles.css
# 同理。根因是"整目录托管"这个配置本身：以后任何人往里丢一个 .py / .bak / .sql，
# 都会立刻变成公开可下载的文件。
#
# 前端是单文件应用（内联 CSS/JS，不引用任何外部资源），所以这里只放行
# 真正需要的文件。新增静态资源记得加进来，否则会被 404。
STATIC_FILES = {"index.html", "favicon-paper.svg", "logo-paper.svg"}
app = Flask(__name__, static_folder=None)

# ── 会话密钥：必须有，且不能是那个公开的兜底值 ─────────────
# 为什么这次改成"缺失就拒绝启动"：本站要挂到公网域名上。
# Flask 的登录态就是一枚用 secret_key 签名的 cookie，签名密钥一旦可猜，
# 任何人都能自己签一个 {"user_id": 1} 冒充别人登录 —— 别人的对话记录、
# 收藏就都能读。而原来的兜底值 "your-secret-key-change-this-in-production"
# 写在开源代码里，等于公开密钥。
#
# 本地跑不受影响：.env 里已经生成了一个随机值（见 .env.example 的说明）。
_SECRET_DEFAULT = "your-secret-key-change-this-in-production"
_secret = (os.getenv("FLASK_SECRET_KEY") or "").strip()
if not _secret or _secret == _SECRET_DEFAULT:
    raise RuntimeError(
        "FLASK_SECRET_KEY 未设置（或仍是示例里的默认值）。\n"
        "本站的登录态由它签名，用默认值等于任何人都能伪造登录。\n"
        "请在 .env 里设置一个随机值，例如：\n"
        "  python -c \"import secrets; print('FLASK_SECRET_KEY=' + secrets.token_hex(32))\"\n"
        "把输出那行加进 .env 即可。"
    )
app.secret_key = _secret

# ── 反向代理（natapp / ngrok 这类内网穿透）────────────────
# 穿透工具在中间做了一层代理：浏览器访问的是 https://xxx.natappfree.cc，
# 但它转发到本地是 http。不加这一层的话 Flask 以为自己是 http，
# 生成绝对 URL、判断 is_secure 都会出错（安全 cookie 就发不出去）。
# x_proto=1 表示只信一层代理的 X-Forwarded-Proto。
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

# ── 公网访问口令（可选，设了才生效）─────────────────────
# 为什么需要：本站在公网暴露的是**共享的 DeepSeek 额度**，
# 随手转发的链接就可能被爬虫或陌生人刷掉费用。
# 不设 PUBLIC_PASSWORD 就完全开放（本地开发/评委直连都方便）；
# 设了之后所有页面和接口都要求先过这一关。
PUBLIC_PASSWORD = (os.getenv("PUBLIC_PASSWORD") or "").strip()

# 放行清单：口令页本身、健康检查、站点图标。其余一律先过口令。
_GATE_EXEMPT = {"/gate", "/api/health", "/favicon-paper.svg", "/logo-paper.svg"}

_GATE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>学术检索 · AI 助手</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#f0f4f8;font-family:'Segoe UI','PingFang SC',Roboto,sans-serif}
 .box{background:#fff;border-radius:20px;padding:32px 28px;width:340px;
      box-shadow:0 16px 44px rgba(20,40,90,.14);text-align:center}
 h1{font-size:17px;color:#1a2a6c;margin:0 0 6px}
 p{font-size:13px;color:#8892a4;margin:0 0 18px;line-height:1.7}
 input{width:100%;box-sizing:border-box;padding:11px 14px;border:1px solid #dde1e8;
       border-radius:10px;font-size:14px;font-family:inherit;outline:none}
 input:focus{border-color:#4f7df3}
 button{width:100%;margin-top:12px;padding:11px;border:none;border-radius:10px;
        background:linear-gradient(135deg,#1a2a6c,#2d4373);color:#fff;font-size:14px;
        cursor:pointer;font-family:inherit}
 .err{margin-top:10px;font-size:12.5px;color:#c0392b;min-height:16px}
</style></head><body>
<div class="box">
  <h1>📚 学术检索 · AI 助手</h1>
  <p>本站部署在公网，请先输入访问口令</p>
  <form method="post" action="/gate">
    <input type="password" name="password" placeholder="访问口令" autofocus autocomplete="current-password">
    <button type="submit">进入</button>
  </form>
  <div class="err">__ERROR__</div>
</div></body></html>"""


@app.before_request
def _public_access_gate():
    """设了 PUBLIC_PASSWORD 才生效的门禁。

    用 cookie 里的一个签名标记记住"已通过"，而不是每次请求都带口令。
    标记内容就是 'ok'，靠 Flask 的会话签名保证不可伪造
    （所以上面那条"必须设置 FLASK_SECRET_KEY"是这套东西的前提）。

    实现上有个坑：POST /gate 必须由**路由**处理，不能在这里返回重定向 ——
    原来把表单提交也写在这个钩子里，而 Flask 在 before_request 返回非 None 之后
    仍会按 URL 规则匹配路由，/gate 只声明了 GET，于是 POST 变成 405。
    所以这里只负责"放行判断"，口令校验交给下面的 /gate 路由。
    """
    if not PUBLIC_PASSWORD:
        return None
    if request.path in _GATE_EXEMPT:
        return None
    if session.get("gate_ok"):
        return None
    return _GATE_PAGE.replace("__ERROR__", ""), 401


@app.route("/gate", methods=["GET", "POST"])
def gate():
    """口令页：GET 显示表单，POST 校验口令。"""
    if not PUBLIC_PASSWORD:
        return redirect("/")
    if request.method == "POST":
        # 直接比较口令。这里不做常数时间比较 ——
        # 这是一个共享口令而不是每人一个密码，收益极低。
        if (request.form.get("password") or "") == PUBLIC_PASSWORD:
            session["gate_ok"] = True
            session.permanent = True      # 口令也只问一次，30 天内别再烦用户
            return redirect("/")
        return _GATE_PAGE.replace("__ERROR__", "口令不正确"), 401
    return redirect("/")

# ── 登录验证装饰器 ────────────────────────────────────────
def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "请先登录"}), 401
        return f(*args, **kwargs)
    return decorated_function


# ── 登录态记忆时长 ────────────────────────────────────────
# 需求："不用每次运行项目都重新登录"。
# Flask 的会话 cookie 默认是**会话 cookie**，浏览器一关就没了；
# 这里改成持久 cookie（30 天），只要 FLASK_SECRET_KEY 不变，
# 重启服务后浏览器里的登录态依然有效。
from datetime import timedelta  # noqa: E402

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


# ── 启动即建表 ────────────────────────────────────────────
# 以前只在 `if __name__ == "__main__"` 里调 db.init_db()，
# 用 `flask run` / gunicorn 之类方式启动就没有 users 表，注册直接 500。
# 这里在导入时补一次（CREATE TABLE IF NOT EXISTS，幂等）。
# 账号备份 accounts.json 也会在 init_db 里自动合并进来。
try:
    db.init_db()
except Exception as _db_exc:      # 数据库目录不可写也不能让整个站起不来
    print(f"数据库初始化失败（登录/收藏会不可用）：{_db_exc}")

# ── 文献数据源：多源聚合层 ────────────────────────────────
# 数据源不是单一的 Semantic Scholar，而是 providers/ 下的多源聚合：
#   semantic_scholar  英文，免费公共配额（高峰期会限流）
#   openalex          英+中，完全免费无需 key，中文靠 filter=language:zh
# 节流 / 指数退避 / 缓存 / 跨源去重都在 providers 里，CLI 版 Agent.py 共用同一份。
# 换源或加源只需要新增 providers/xxx.py 并在注册表里加一行，本文件不用动。
import providers
import workflow as wf
import attachments


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
    不要自己在正文里人工剔除。
11. 如果用户要的是**成篇的东西**——"帮我写篇综述""做个文献调研/研究现状""整理一份
    带参考文献的报告""系统梳理一下这个方向的进展"——不要自己一段段写，
    也不要连续调用十几次 search_papers 硬凑。改用 start_research_workflow：
    它会自动跑完 检索筛选 → 信息抽取 → 综述生成 → 统计可视化 → 参考文献格式化，
    产出带 GB/T 7714 参考文献的完整报告，而且中途可中断、可从断点续跑。
    调用后告诉用户"已规划好研究方案，点按钮开始执行"，
    并简单说一下你规划出的检索重点，不要假装报告已经写好了。
12. **绝对不要把工具的返回内容原样复制到正文里**。工具返回的是给你自己看的
    结构化数据（JSON）或执行回执，用户不需要、也看不懂。
    实测踩过的坑：start_research_workflow 返回 JSON 时，正文里直接出现了
    `{"topic": "...", "query": "...", "sections": [...]}` 这样一坨。
    无论工具返回什么格式，你都要用自己的话**转述**成中文：
    "已规划好方案，检索式是 X，重点看 Y，执行流程是 …"。
    正文里出现 `{` `}` `"key":` 这类结构化痕迹，就是错的。
13. 用户可能随消息附上图片（论文截图、参考文献页、拍摄的实验照片、
    图表、论文 PDF 转成的页面）。这些图片**你自己能看**（多模态），
    按下面两种情形处理，**不要**回答"我无法查看图片"：

    (a) **图里有论文/文献信息**（标题、作者、期刊、DOI、参考文献列表）：
        先读图取出题录信息，再**立刻调用 search_papers 去检索**，
        把图里的论文真正找出来给用户。
        * 检索时分两次调用效果最好（实测模型自己就是这么做的）：
          一次用"标题 + 作者 + 关键词"的英文/中文组合，一次用 DOI 原样检索；
        * 图上没有 DOI 就只用标题+作者，不要编一个 DOI 出来；
        * 若图里是**多篇**文献的列表，逐个检索太慢时，挑最相关的几篇先检索，
          并说明"图中共 N 篇，已为你检索前几篇"。
    (b) **图是实验照片、现象、示意图、图表**（没有题录信息）：
        先客观描述你看到了什么（对象、条件、趋势、异常），
        再据此归纳出 3-5 个**学术检索关键词**，然后调用 search_papers，
        帮用户找到与这个现象相关的已有研究。
        * 描述要基于图上真实可见的内容，**看不清的地方明说"无法辨认"**，
          不要猜测或补全不存在的细节；
        * 表格/曲线图里的具体数值容易看错，只讲趋势和量级，
          不要给出精确读数后就当成事实（可以说"图中显示随 X 增大而上升"）。

    涉及图片时，正文开头先用一句话说明你从图里读到了什么
    （例如"我识别到这篇论文是《X》，作者 Y"），让用户能校验你的识别是否准确。
    识别不确定时明确说明不确定。
14. 如果用户附了图但**什么都没说**，按 (a)/(b) 自行判断属于哪种情形并直接开始工作，
    不要反问"你想让我做什么"。"""


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


def _make_research_tool(notify, user_id):
    """造一个"把整件事移交出去"的工具：模型负责规划，执行交给工作流引擎。

    为什么是每次请求现造，而不是像 search_papers 那样做成全局工具：
    工具跑在 agent 的工作线程里，要把"已规划好哪条工作流"告诉同一请求的
    SSE 生成器。用闭包捕获 notify 回调是最省事也最不会串线的做法 ——
    换成模块级变量，两个用户同时聊天就会互相覆盖。
    """

    @tool
    def start_research_workflow(goal: str) -> str:
        """把"写综述/做文献调研/出研究报告"这类整篇任务移交科研工作流引擎。

        适用：用户要一篇综述、研究现状、文献调研、带参考文献的报告。
        不适用：只查几篇文献（用 search_papers）、查某篇的详情（用 get_paper_detail）。

        参数:
            goal: 研究目标，用一句话写清楚研究对象和范围，
                  例如"图神经网络在遥感图像语义分割中的应用进展"。

        引擎会自动完成：检索筛选 -> 信息抽取 -> 综述生成 -> 统计可视化 -> 参考文献格式化，
        产出带 GB/T 7714 参考文献的 Markdown 报告，支持中断与断点续跑。
        调用后不要臆造综述内容，只需告诉用户方案已就绪、可以开始执行。
        """
        text = (goal or "").strip()
        if len(text) < 4:
            return json.dumps({"error": "goal_too_short",
                               "message": "研究目标太短，请补充研究对象与范围"},
                              ensure_ascii=False)
        try:
            plan = wf.plan_workflow(text)
            workflow_id = db.create_workflow(user_id, text, plan) if user_id else None
        except Exception as e:  # noqa: BLE001 - 规划失败不能让整轮对话崩掉
            return json.dumps({"error": "plan_failed", "message": f"{type(e).__name__}: {e}"},
                              ensure_ascii=False)

        notify({"workflow_id": workflow_id, "goal": text, "plan": plan})
        steps = "、".join(s["title"] for s in wf.STEPS)
        # 返回**一段自然语言**，不要用 "字段：值" 的清单，更不要用 JSON。
        # 实测教训：返回 JSON 时，模型会把 JSON 原样当答复吐出来；
        # 改成 "字段：值" 的清单后，模型照样照着清单拼了一个 JSON 对象
        # （正文里出现 {"topic":"...","query":"...","sections":[...]}）。
        # 模型会模仿工具返回的形状，所以这里就给成散文。
        return (
            f"已为用户规划好一条科研工作流（编号 {workflow_id}），主题是"
            f"「{plan.get('topic') or text}」，准备用检索式「{plan.get('query')}」"
            f"去查{plan.get('source')}范围的文献，重点关注{plan.get('focus')}。"
            f"执行流程依次是{steps}。"
            "界面上的「开始执行」按钮已经就绪，点击后开始跑，"
            "中途可以随时中断，之后能从断点继续。"
            "请用简体中文、以自然的段落转述这些要点，告诉用户方案已就绪；"
            "不要编造综述内容，也不要用花括号或引号列出字段。"
        )

    return start_research_workflow


# ═══════════════════════════════════════════════════════════════
#  路由：静态页面（白名单见 STATIC_FILES）
# ═══════════════════════════════════════════════════════════════

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_whitelist(filename):
    """只放行白名单里的静态文件；其余（例如误放进去的 .py/.bak/.db）一律 404。

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
    if not success:
        return jsonify({"error": message}), 400

    # 注册成功直接建会话（自动登录）：
    # 以前注册完还要再手动登录一次，用户经常以为是"注册没生效"。
    # 账号本身已经写进 literature.db，并同步到项目里的 accounts.json，
    # 下次运行（甚至换个目录）都能用同一套账号登录。
    user = db.login_user(username, password)
    if user:
        session.permanent = True          # 30 天持久会话，别每次都要重登
        session['user_id'] = user['id']
        session['username'] = user['username']
        return jsonify({
            "success": True,
            "message": "注册成功，已自动登录",
            "user": {"id": user['id'], "username": user['username']},
        })

    # 极端情况（库不可写等）：注册成功但建不了会话，让前端切回登录模式
    return jsonify({"success": True, "message": "注册成功，请登录"})


@app.post("/api/login")
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    user = db.login_user(username, password)
    if user:
        session.permanent = True          # 30 天持久会话
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
    # 只发了图没写字时 question 是空的，标题给个可辨认的占位，
    # 否则左侧会话列表会出现一条没有标题的记录。
    title = (question or "").strip()[:30] or "（图片提问）"
    try:
        return db.create_conversation(user_id, title)
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

    # 图片/PDF 附件。产品要求**不保存图片**，所以全程只在内存里过一遍：
    # 解码 -> （PDF 则渲染成 PNG）-> 拼进本轮消息 -> 发完即丢。
    # 不落盘、不入库、不写日志（日志里只有体积和格式，没有 base64 内容）。
    try:
        attach_blocks, attach_notes = attachments.prepare_attachments(
            data.get("files") or [])
    except attachments.AttachmentError as e:
        return jsonify({"error": str(e)}), 400

    # 只发了图没写字也要能工作：此时给一句中性的默认指令，
    # 具体怎么处理交给系统提示词里的图片规则去判断。
    if not message and not attach_blocks:
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

    messages.append({"role": "user",
                     "content": attachments.build_user_content(
                         message, attach_blocks, attach_notes)})


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
        state = {"tool_used": False, "answer": "", "papers": [], "workflow": None,
                 "muted": False, "mute_sent": False,
                 # 思考模型（deepseek-flash）会先吐 reasoning_content：
                 # 用这两个字段记录"思考了多少字""是否已提示过思考中"
                 "reasoning_chars": 0, "thinking_notified": False}

        def suppress_json():
            """判断本轮回答是不是"模型把工具返回的结构化数据当答案吐了出来"。

            实测：移交工作流那一步，模型的回答从 `{"topic": "…", "query": "…",
            "sections": [...]}` 开始 —— 这是把工具的返回原样复读，不是给人看的话。
            改成给它散文形状的返回后仍偶发，所以在出口再兜一层：
            只要回答以 `{` 或 `[` 开头，就静音后续 token，末尾用一句人话代替。
            前端本来就有结构化渲染（方案卡 / 论文卡片），不需要正文再复读一遍。
            """
            if state["muted"]:
                return True
            head = state["answer"].lstrip()
            if head[:1] in ("{", "["):
                state["muted"] = True
            return state["muted"]

        def on_workflow_ready(info):
            state["workflow"] = info

        # 每个请求现造一个 agent：多挂一个"移交科研工作流"的工具，
        # 它要把结果回传给本次请求的 state（见 _make_research_tool 的说明）。
        #
        # 带图片时把 max_tokens 抬高：deepseek-flash 读图会先做很长的推理
        # （实测 241~842 字），推理内容占用的是同一个 max_tokens 预算，
        # 额度给小了就会出现"正文为空"——看起来像模型没回答。
        model_kwargs = {"model": DEEPSEEK_MODEL, "temperature": 0}
        if attach_blocks:
            model_kwargs["max_tokens"] = attachments.VISION_MIN_MAX_TOKENS
        agent = create_agent(
            model=ChatDeepSeek(**model_kwargs),
            tools=[search_papers, get_paper_detail, _make_research_tool(on_workflow_ready, user_id)],
            system_prompt=DEFAULT_SYSTEM_PROMPT,
        )

        async def run_agent():
            try:
                async for ev in agent.astream_events({"messages": messages}, version="v2"):
                    kind = ev.get("event")

                    if kind == "on_tool_start":
                        # 工具调用前模型常会先说一句"我先搜索一下"（还可能是英文），
                        # 那不是给用户看的答案，丢掉它并切到"检索中"状态
                        state["tool_used"] = True
                        state["answer"] = ""
                        name = ev.get("name") or ""
                        events.put(("status", "正在规划研究方案…"
                                    if name == "start_research_workflow"
                                    else "正在检索文献源…"))

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
                        # 只把真正的"回答文字"当正文。tool_call_chunks 非空说明
                        # 这一段是**工具参数**（模型在拼 {"goal": "..."}），
                        # 不是给用户看的内容 —— 实测它一度被当成正文发出去。
                        if getattr(chunk, "tool_call_chunks", None):
                            continue

                        # 思考模型（deepseek-flash / v4-pro）会先吐一大段
                        # reasoning_content，正文要等它结束才开始。
                        # 实测一个需要推理的问题：思考阶段长达 21 秒，
                        # 这期间 content 一直是空的 —— 如果只读 content，
                        # 界面上就是一个空白泡泡转 21 秒，看起来完全像卡死。
                        # 所以这里把"思考中"推给前端，让用户知道它在干活。
                        reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get(
                            "reasoning_content")
                        if reasoning:
                            state["reasoning_chars"] = state.get("reasoning_chars", 0) + len(
                                str(reasoning))
                            # 只在第一片时报一次，避免每个分片都刷状态
                            if not state.get("thinking_notified"):
                                state["thinking_notified"] = True
                                events.put(("status", "模型正在思考…"))

                        text = getattr(chunk, "content", None)
                        if text:
                            # 正文一开始就把状态切回"生成回答"，否则会一直停在
                            # "思考中"。用"是不是第一次拿到正文"来判断，而不是去
                            # 猜 langchain 给分片起的 type 名字。
                            if state.get("thinking_notified"):
                                state["thinking_notified"] = False
                                events.put(("status", "正在生成回答…"))
                            state["answer"] += text
                            # 有工具调用时，工具之后的内容才是答案，直接边生成边发
                            if state["tool_used"] and not suppress_json():
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

        # 被静音的情况（模型把结构化数据当答案复读了）：用一句人话代替，
        # 并把补发的 token 也改成这句 —— 否则用户会看到一坨花括号。
        if state["muted"]:
            if state["workflow"]:
                answer = ("已经为这个题目规划好一条科研工作流（检索筛选 → 信息抽取 → "
                          "综述生成 → 统计可视化 → 参考文献格式化）。"
                          "点下面的「开始执行」就可以跑，中途能中断，之后可以从断点继续。")
            else:
                answer = "已获取到结果，结构化内容见下方卡片。"

        # 没有调用工具的情况（闲聊类问题）：token 没被实时发出去，这里补发
        if not state["tool_used"] or state["muted"]:
            for i in range(0, len(answer), 3):
                yield sse_pack({"type": "token", "token": answer[i:i + 3]}).encode("utf-8")

        if user_id:
            # results_count 记的是"这一轮检索到几篇论文"，不是消息条数
            _persist_turn(user_id, conversation_id, message, answer, len(state["papers"]))

        # 模型决定"这事该交给工作流"时，把方案和入口发给前端，
        # 由界面渲染成「开始执行」按钮（执行是分钟级的，不适合塞在聊天流里）。
        if state["workflow"]:
            yield sse_pack({"type": "workflow_ready", **state["workflow"]}).encode("utf-8")

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


# ═══════════════════════════════════════════════════════════════
#  路由：科研工作流（命题要求的自规划 + 状态管理 + 中断恢复）
# ═══════════════════════════════════════════════════════════════

# 正在跑的工作流 -> 停止标志。中断接口把它置 True，编排器每步结束会检查。
_RUNNING = {}
_RUNNING_LOCK = threading.Lock()


def _wf_state_payload(wf_row, runner=None):
    """把库里的工作流整理成前端要的结构。"""
    steps = []
    by_key = wf_row.get("steps") or {}
    for s in wf.STEPS:
        cur = by_key.get(s["key"]) or {}
        steps.append({
            "key": s["key"], "title": s["title"], "core": s["core"],
            "detail_text": s["detail"],
            "status": cur.get("status") or "pending",
            "progress": cur.get("detail") or "",
            "error": cur.get("error") or "",
            "updated_at": cur.get("updated_at") or "",
        })
    out = {
        "id": wf_row["id"],
        "goal": wf_row["goal"],
        "status": wf_row["status"],
        "plan": wf_row.get("plan") or {},
        "steps": steps,
        "created_at": wf_row.get("created_at"),
        "updated_at": wf_row.get("updated_at"),
    }
    ctx = wf_row.get("context") or {}
    if ctx:
        if ctx.get("papers"):
            out["papers"] = ctx["papers"]
        if ctx.get("stats"):
            out["stats"] = ctx["stats"]
        if ctx.get("synth"):
            out["synth"] = ctx["synth"]
    if wf_row.get("report_md"):
        out["report"] = wf_row["report_md"]
    return out


@app.post("/api/workflow")
@login_required
def run_workflow():
    """启动（或续跑）一条科研工作流。

    SSE 事件：
        plan     规划结果（topic/query/source/limit/focus/sections）
        step     某一步的状态变化：{key, status, detail, error}
        snapshot 阶段性快照（文献数、统计、综述、报告）
        done     结束：{status, workflow_id}
        error    出错
    """
    data = request.get_json(force=True, silent=True) or {}
    goal = (data.get("goal") or "").strip()
    resume_id = data.get("workflow_id")
    papers_override = data.get("papers")      # 人工修正后的文献列表

    if not goal and not resume_id:
        return jsonify({"error": "请填写研究目标"}), 400

    # 必须在进入生成器之前把 session 读出来：生成器是在请求上下文之外被迭代的，
    # 里面再访问 session 会抛 "Working outside of request context"（踩过这个坑）。
    user_id = session.get("user_id")

    def sse_pack(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def generate():
        # 这两个只在"续跑"分支里赋值，但后面无条件使用 ——
        # 必须先给默认值，否则新工作流会抛 UnboundLocalError（同一个坑踩了两次）。
        ctx_prev = {}
        state_prev = {}
        start_from = wf.STEP_KEYS[0]

        # ── 已有工作流：续跑 ──────────────────────────────────
        if resume_id:
            row = db.get_workflow(int(resume_id), user_id)
            if not row:
                yield sse_pack({"type": "error", "message": "工作流不存在"}).encode("utf-8")
                yield sse_pack({"type": "done", "status": "failed"}).encode("utf-8")
                return
            plan = row.get("plan") or wf.plan_workflow(row["goal"])
            ctx_prev = row.get("context") or {}
            # 把上一轮各步骤的状态铺回去，快照才是完整时间线
            # （否则续跑后 search/extract 会显示成 pending，看起来像白跑了）
            state_prev = {k: {"status": (v or {}).get("status", "pending"),
                              "detail": (v or {}).get("detail", "")}
                          for k, v in (row.get("steps") or {}).items()}
            workflow_id = row["id"]
            # 注意：不要在这里写 goal = row["goal"]。
            # 一旦在 generate() 里给 goal 赋值，Python 就把它当成局部变量，
            # 于是同一个函数里 else 分支的 plan_workflow(goal) 会抛
            # UnboundLocalError（踩过这个坑）。续跑用 row["goal"] 就够了。
            workflow_goal = row["goal"]
            # 已完成的步骤：除了要把产物带回来，也让编排器从最早未完成的那步接着跑
            done_keys = [k for k, v in (row.get("steps") or {}).items()
                         if (v or {}).get("status") == "done"]
            start_from = "search"
            for s in wf.STEPS:
                if s["key"] not in done_keys:
                    start_from = s["key"]
                    break
            else:
                start_from = wf.STEP_KEYS[-1]
            yield sse_pack({"type": "plan", "plan": plan, "workflow_id": workflow_id,
                            "resumed": True, "start_from": start_from}).encode("utf-8")
        else:
            # ── 新工作流：自规划 ────────────────────────────
            yield sse_pack({"type": "step", "key": "plan", "status": "running",
                            "title": "规划研究方案", "core": "—",
                            "detail": "正在理解研究目标并规划步骤"}).encode("utf-8")
            plan = wf.plan_workflow(goal)
            workflow_id = db.create_workflow(user_id, goal, plan)
            ctx_prev = {}
            yield sse_pack({"type": "plan", "plan": plan, "workflow_id": workflow_id}).encode("utf-8")
            yield sse_pack({"type": "step", "key": "plan", "status": "done",
                            "title": "规划研究方案", "core": "—",
                            "detail": plan.get("plan") or "已确定检索方案"}).encode("utf-8")

        with _RUNNING_LOCK:
            _RUNNING[workflow_id] = False

        def should_stop():
            with _RUNNING_LOCK:
                return bool(_RUNNING.get(workflow_id))

        # ── 为什么改成"工作线程 + 队列" ──────────────────────────
        # 原来这里是同步跑完 runner.run()，再把事件列表一次性回放。
        # 实测后果（profiler 的时间线）：整条流水线 12.7s，面板在 12.7s 时
        # **一次性收到所有事件** —— 前 12.7 秒界面上只有一句"工作流执行中…"，
        # 看起来就是"卡住了"。真实耗时其实没变，但主观感受差很多。
        # 现在和 /api/chat 用同一套办法：编排器跑在独立线程里，
        # 每步状态变化立刻经 Queue 推给生成器，用户能实时看到走到哪一步。
        events = queue.Queue()
        error_box = {}

        def on_step(key, status, detail):
            # interrupted 也要落库并推给前端，否则界面上只会看到"停在那一步"，
            # 不知道是中断了还是卡住了。
            db.set_workflow_step(workflow_id, key, status, detail,
                                 detail if status == "failed" else "")
            events.put({"type": "step", "key": key, "status": status, "detail": detail})

        # 注意：人工修正的文献要写进 ctx_prev 本身。
        # 之前写的是 ctx_prev = dict(...) 的副本，等于白改 ——
        # 续跑时编排器从 synthesize 开始，会直接复用旧的 papers，改动被丢掉。
        if papers_override:
            ctx_prev["papers"] = papers_override
        runner = wf.WorkflowRunner(plan, on_step=on_step, should_stop=should_stop)
        db.update_workflow_status(workflow_id, "running")

        def run_pipeline():
            try:
                runner.run(start_from=start_from, previous=ctx_prev,
                           previous_state=state_prev)
            except Exception as e:  # noqa: BLE001 - 异常要送回生成器
                error_box["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                db.update_workflow_status(workflow_id, "failed")
            finally:
                events.put(None)      # 结束哨兵

        threading.Thread(target=run_pipeline, daemon=True).start()

        # 边跑边推。每收到一步就立刻下发，前端面板随即更新。
        while True:
            ev = events.get()
            if ev is None:
                break
            yield sse_pack(ev).encode("utf-8")

        if error_box.get("error"):
            yield sse_pack({"type": "error", "message": error_box["error"]}).encode("utf-8")

        snap = runner.snapshot()
        ctx = {
            "papers": runner.ctx.get("papers") or [],
            "stats": runner.ctx.get("stats"),
            "synth": runner.ctx.get("synth"),
        }
        report = (runner.ctx.get("format") or {}).get("report", "")
        db.save_workflow_result(workflow_id, ctx, report)

        failed = [s for s in snap["steps"] if s["status"] == "failed"]
        interrupted = any(s["status"] == "interrupted" for s in snap["steps"])
        final = "interrupted" if interrupted else ("partial" if failed else "done")
        db.update_workflow_status(workflow_id, final)

        with _RUNNING_LOCK:
            _RUNNING.pop(workflow_id, None)

        yield sse_pack({"type": "snapshot", **snap}).encode("utf-8")
        yield sse_pack({"type": "done", "status": final,
                        "workflow_id": workflow_id}).encode("utf-8")

    return Response(generate(), headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }, mimetype="text/event-stream", direct_passthrough=True)


@app.get("/api/workflow/<int:workflow_id>")
@login_required
def get_workflow(workflow_id):
    """读工作流状态（仅本人）。

    这里**故意不做"读不到就按 id 兜底再读一次"** ——
    工作流 id 是自增的（1、2、3…），任何人都能猜。
    兜底那次查询不带 user_id，等于匿名就能把别人整份报告、检索式、
    文献列表读走。这和之前"整目录托管 static 导致 /app.py 可下载"
    是同一类漏洞，所以四个工作流接口统一要求登录且校验归属。
    """
    row = db.get_workflow(workflow_id, session["user_id"])
    if not row:
        return jsonify({"success": False, "error": "工作流不存在"}), 404
    return jsonify({"success": True, "workflow": _wf_state_payload(row)})


@app.post("/api/workflow/<int:workflow_id>/interrupt")
@login_required
def interrupt_workflow(workflow_id):
    """请求中断。

    编排器在每次步骤内汇报进度时都会查这个标志，所以批处理中的长步骤
    （分批抽取、多节综述）也能被打断；已经跑完的步骤保持 done 不丢，
    中断标记落在"本来要开始却没开始"的那一步上，续跑即以它为起点。
    """
    with _RUNNING_LOCK:
        if workflow_id in _RUNNING:
            _RUNNING[workflow_id] = True
            return jsonify({"success": True, "message": "已请求中断，将在当前步骤结束后停止"})
    return jsonify({"success": True, "message": "该工作流当前没有在运行"})


@app.delete("/api/workflow/<int:workflow_id>")
@login_required
def remove_workflow(workflow_id):
    if db.delete_workflow(workflow_id, session["user_id"]):
        return jsonify({"success": True, "message": "已删除"})
    return jsonify({"success": False, "error": "工作流不存在"}), 404


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 建表已经在导入时做过了（见上面的 db.init_db()），这里不再重复调用，
    # 免得日志里出现两遍"数据库初始化完成"。

    if not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未设置。请在系统环境变量中设置，或在项目根目录创建 .env 文件并写入：\n"
            "DEEPSEEK_API_KEY=sk-xxx"
        )

    host = (os.getenv("APP_HOST") or "127.0.0.1").strip()
    try:
        port = int(os.getenv("APP_PORT") or "8000")
    except ValueError:
        port = 8000

    print("=" * 50)
    print("   📚 学术文献检索助手")
    print(f"   🌐 本机 http://127.0.0.1:{port}")
    # 把账号存放在哪儿打出来：用户不用猜"我的账号到底存在哪"
    print(f"   🗄  账号数据库: {db.DB_PATH}")
    print(f"   🧾 账号备份:   {db.ACCOUNTS_FILE}")
    if PUBLIC_PASSWORD:
        print("   🔒 已启用公网访问口令（PUBLIC_PASSWORD）")
    print("=" * 50)

    # 端口可用 APP_PORT 覆盖：natapp 这类内网穿透转发的是**隧道里配置的那个
    # 本地端口**，如果隧道填的是 80，就用 APP_PORT=80 启动本服务去对齐。
    app.run(host=host, port=port, debug=True, threaded=True, use_reloader=False)
