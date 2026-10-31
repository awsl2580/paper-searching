"""数据库操作模块 - 用户系统和搜索历史"""

import json
import os
import sqlite3

# 账号（用户/密码哈希/头像）现在存在项目根目录的 accounts.json 里，
# 见 accounts.py 的说明。这里保留同名函数做转发，调用方不用改。
import accounts
import bcrypt

# 数据库固定放在项目根目录（本文件在 core/，库在上一级），不用相对路径。
#
# 原因：原来写的是 DB_PATH = "literature.db"，而 sqlite3.connect 会相对**当前工作目录**
# 解析，于是"数据库在哪儿"取决于从哪个目录启动。实测已经踩到：从 AIagent2\ 启动过
# 一次，就在那儿留下了一个 0 行的 literature.db，而真正的库在 AIagent2\AIagent1\ 下
# ——用户看到的现象是"我的收藏和历史都没了"。
# 用 __file__ 锚定后，无论从哪个目录启动都指向同一个库。
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "literature.db")


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_db()
    cursor = conn.cursor()

    # 用户表
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS users
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       username
                       TEXT
                       UNIQUE
                       NOT
                       NULL,
                       password
                       TEXT
                       NOT
                       NULL,
                       email
                       TEXT,
                       created_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP
                   )
                   ''')

    # 搜索历史表
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS search_history
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       user_id
                       INTEGER
                       NOT
                       NULL,
                       query
                       TEXT
                       NOT
                       NULL,
                       search_time
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       results_count
                       INTEGER
                       DEFAULT
                       0,
                       FOREIGN
                       KEY
                   (
                       user_id
                   ) REFERENCES users
                   (
                       id
                   )
                       )
                   ''')

    # 收藏表
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS favorites
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       user_id
                       INTEGER
                       NOT
                       NULL,
                       paper_id
                       TEXT
                       NOT
                       NULL,
                       paper_title
                       TEXT
                       NOT
                       NULL,
                       authors
                       TEXT,
                       year
                       TEXT,
                       added_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       FOREIGN
                       KEY
                   (
                       user_id
                   ) REFERENCES users
                   (
                       id
                   ),
                       UNIQUE
                   (
                       user_id,
                       paper_id
                   )
                       )
                   ''')

    # 会话表（登录用户的完整对话记录）
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS conversations
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       user_id
                       INTEGER
                       NOT
                       NULL,
                       title
                       TEXT
                       NOT
                       NULL,
                       created_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       updated_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       FOREIGN
                       KEY
                   (
                       user_id
                   ) REFERENCES users
                   (
                       id
                   )
                       )
                   ''')

    # 会话消息表
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS conversation_messages
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       conversation_id
                       INTEGER
                       NOT
                       NULL,
                       role
                       TEXT
                       NOT
                       NULL,
                       content
                       TEXT
                       NOT
                       NULL,
                       created_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       FOREIGN
                       KEY
                   (
                       conversation_id
                   ) REFERENCES conversations
                   (
                       id
                   )
                       )
                   ''')

    # 科研工作流表
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS workflows
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       user_id
                       INTEGER,
                       goal
                       TEXT
                       NOT
                       NULL,
                       plan_json
                       TEXT,
                       status
                       TEXT
                       DEFAULT
                       'pending',
                       context_json
                       TEXT,
                       report_md
                       TEXT,
                       created_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       updated_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       FOREIGN
                       KEY
                   (
                       user_id
                   ) REFERENCES users
                   (
                       id
                   )
                       )
                   """)

    # 工作流步骤表（状态管理 / 中断恢复靠它）
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS workflow_steps
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       workflow_id
                       INTEGER
                       NOT
                       NULL,
                       step_key
                       TEXT
                       NOT
                       NULL,
                       status
                       TEXT
                       DEFAULT
                       'pending',
                       detail
                       TEXT,
                       error
                       TEXT,
                       updated_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       FOREIGN
                       KEY
                   (
                       workflow_id
                   ) REFERENCES workflows
                   (
                       id
                   ),
                       UNIQUE
                   (
                       workflow_id,
                       step_key
                   )
                       )
                   """)

    # users 表补一列 last_login_at（老库升级用）。
    # 为什么值得单独一列：登录界面要显示"本机已注册的账号"，
    # 带上"上次登录时间"才分得清哪个是自己的常用号。
    # SQLite 加列只能 ALTER，重复执行会报 duplicate column，所以 try/except。
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    # 头像：存成 data URL（前端压到 128×128 再传，通常 < 30KB）。
    # 为什么不落文件：项目里所有用户数据都在 literature.db 一个文件里，
    # 备份/搬家只要拷它；头像再另建目录会多一份要同步的东西。
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    except sqlite3.OperationalError:
        pass

    # conversation_messages 补一列 papers（JSON 文本）。
    #
    # 为什么必须存：聊天里检索到的论文是**结构化卡片**（标题/作者/来源/链接/摘要），
    # 正文只是模型写的一句话。原来只存正文，于是"切到别的会话再切回来"时，
    # 卡片没了、只剩一串没有链接的标题文字 —— 用户看到的就是"论文变成纯文本、
    # 点不开原文"。存的是数据不是渲染好的 HTML：前端换样式时历史卡片跟着一起变，
    # 也避免把 XSS 面从"渲染时转义"挪到"入库时转义"。
    try:
        cursor.execute("ALTER TABLE conversation_messages ADD COLUMN papers TEXT")
    except sqlite3.OperationalError:
        pass

    # 长周期研究记忆（memory.py 也自带一份建表语句，两边幂等）
    #
    # 为什么不放进 workflows.context_json 就好：上下文是**单次工作流**的产物，
    # 而记忆要跨任务、跨会话、跨周复用，还得能按相关度召回、能被遗忘。
    # 这两件事的生命周期完全不同，塞在同一张表里会让清理逻辑变得没法写。
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS research_memory
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       user_id
                       INTEGER,
                       scope
                       TEXT
                       NOT
                       NULL
                       DEFAULT
                       '',
                       kind
                       TEXT
                       NOT
                       NULL
                       DEFAULT
                       'note',
                       text
                       TEXT
                       NOT
                       NULL,
                       tags
                       TEXT
                       DEFAULT
                       '',
                       weight
                       REAL
                       DEFAULT
                       1.0,
                       ref
                       TEXT
                       DEFAULT
                       '',
                       hits
                       INTEGER
                       DEFAULT
                       0,
                       fingerprint
                       TEXT
                       DEFAULT
                       '',
                       created_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP,
                       updated_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP
                   )
                   """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_user "
                   "ON research_memory(user_id, scope)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_fp "
                   "ON research_memory(fingerprint)")

    conn.commit()
    conn.close()
    # 首次运行：把老库里的账号迁到 accounts.json（已存在则什么都不做）
    _mig = accounts.migrate_from_db(DB_PATH)
    if _mig.get("migrated"):
        print(f"✅ 已把 {_mig['migrated']} 个老账号迁移到 accounts.json")
    print("✅ 数据库初始化完成！")


# ---------- 用户相关 ----------

def register_user(username, password, email=""):
    """注册新用户（存 accounts.json）"""
    return accounts.register_user(username, password, email)


def login_user(username, password):
    """用户登录验证（读 accounts.json，顺手记登录时间）"""
    return accounts.login_user(username, password)






def get_user_by_id(user_id):
    """根据 ID 获取用户信息（不含密码哈希）"""
    return accounts.get_user_by_id(user_id)


def verify_password(user_id, password):
    return accounts.verify_password(user_id, password)


def update_username(user_id, new_username):
    return accounts.update_username(user_id, new_username)


def update_password(user_id, new_password):
    return accounts.update_password(user_id, new_password)


def update_avatar(user_id, data_url):
    return accounts.update_avatar(user_id, data_url)


def get_user_by_username(username):
    """按用户名取用户（不含密码哈希）。"""
    return accounts.get_user_by_username(username)


def user_exists(username):
    """本机是否已有这个用户名（登录页的"本机已注册"提示要用）。"""
    return accounts.user_exists(username)


def list_users(limit=50):
    """本机已注册的账号（只有用户名与时间）。"""
    return accounts.list_users(limit)


def count_users():
    return accounts.count_users()


# ---------- 账号管理（改用户名 / 密码 / 头像） ----------

# ---------- 搜索历史 ----------

def save_search_history(user_id, query, results_count=0):
    """保存搜索历史"""
    if not user_id:
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO search_history (user_id, query, results_count) VALUES (?, ?, ?)",
        (user_id, query, results_count)
    )
    conn.commit()
    conn.close()


def get_search_history(user_id, limit=20):
    """获取用户的搜索历史"""
    if not user_id:
        return []

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, query, search_time, results_count FROM search_history WHERE user_id = ? ORDER BY search_time DESC LIMIT ?",
        (user_id, limit)
    )
    history = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return history


def clear_search_history(user_id):
    """清空搜索历史"""
    if not user_id:
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM search_history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------- 收藏 ----------

def add_favorite(user_id, paper_id, paper_title, authors="", year=""):
    """添加收藏"""
    if not user_id or not paper_id:
        return False

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO favorites (user_id, paper_id, paper_title, authors, year) VALUES (?, ?, ?, ?, ?)",
            (user_id, paper_id, paper_title, authors, year)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False  # 已收藏


def remove_favorite(user_id, paper_id):
    """取消收藏"""
    if not user_id or not paper_id:
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM favorites WHERE user_id = ? AND paper_id = ?", (user_id, paper_id))
    conn.commit()
    conn.close()


def get_favorites(user_id):
    """获取用户收藏列表"""
    if not user_id:
        return []

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, paper_id, paper_title, authors, year, added_at FROM favorites WHERE user_id = ? ORDER BY added_at DESC",
        (user_id,)
    )
    favorites = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return favorites


def is_favorited(user_id, paper_id):
    """检查是否已收藏"""
    if not user_id or not paper_id:
        return False

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM favorites WHERE user_id = ? AND paper_id = ?", (user_id, paper_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None


# ---------- 会话（对话记录） ----------

def create_conversation(user_id, title="新对话"):
    """新建一个会话，返回会话 id"""
    if not user_id:
        return None

    conn = get_db()
    cursor = conn.cursor()
    # 毫秒精度的时间戳：SQLite 的 CURRENT_TIMESTAMP 只到秒，同一秒内创建的会话无法排序
    cursor.execute(
        "INSERT INTO conversations (user_id, title, created_at, updated_at) "
        "VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f','now'), strftime('%Y-%m-%d %H:%M:%f','now'))",
        (user_id, (title or "新对话")[:50])
    )
    conversation_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return conversation_id


def append_message(user_id, conversation_id, role, content, papers=None):
    """向会话追加一条消息；会话不存在或不属于该用户时返回 False

    papers：这一轮检索到的论文（结构化 list，可为空）。
    只对 assistant 消息有意义 —— 前端重新打开历史对话时靠它把卡片原样画回来。
    存成 JSON 文本；序列化失败就当没有（宁可少卡片，也不能让整轮问答存不进去）。
    """
    if not user_id or not conversation_id:
        return False

    papers_json = None
    if papers:
        try:
            papers_json = json.dumps(papers, ensure_ascii=False)
        except (TypeError, ValueError):
            papers_json = None

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user_id)
    )
    if not cursor.fetchone():
        conn.close()
        return False

    cursor.execute(
        "INSERT INTO conversation_messages (conversation_id, role, content, papers, created_at) "
        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'))",
        (conversation_id, role, content, papers_json)
    )
    cursor.execute(
        "UPDATE conversations SET updated_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?",
        (conversation_id,)
    )
    conn.commit()
    conn.close()
    return True


def get_conversations(user_id, limit=50):
    """会话列表，带消息条数，按最近更新倒序；空会话（0 条消息）不返回"""
    if not user_id:
        return []

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
                   SELECT c.id,
                          c.title,
                          c.created_at,
                          c.updated_at,
                          (SELECT COUNT(*) FROM conversation_messages m
                           WHERE m.conversation_id = c.id) AS message_count
                   FROM conversations c
                   WHERE c.user_id = ?
                   ORDER BY c.updated_at DESC, c.id DESC
                   LIMIT ?
                   ''', (user_id, limit))
    rows = [dict(r) for r in cursor.fetchall() if r['message_count'] > 0]
    conn.close()
    return rows


def _load_papers_json(raw):
    """把库里存的 papers JSON 解回 list；坏数据一律当空，别让一条脏记录拖垮整个会话"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def get_conversation_messages(user_id, conversation_id):
    """读取某个会话的全部消息（校验归属），返回 (会话信息, 消息列表)"""
    if not user_id or not conversation_id:
        return None, []

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user_id)
    )
    conversation = cursor.fetchone()
    if not conversation:
        conn.close()
        return None, []

    cursor.execute(
        "SELECT id, role, content, papers, created_at FROM conversation_messages "
        "WHERE conversation_id = ? ORDER BY id",
        (conversation_id,)
    )
    messages = []
    for row in cursor.fetchall():
        item = dict(row)
        item["papers"] = _load_papers_json(item.pop("papers", None))
        messages.append(item)
    conn.close()
    return dict(conversation), messages


def delete_conversation(user_id, conversation_id):
    """删除会话及其消息"""
    if not user_id or not conversation_id:
        return False

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user_id)
    )
    if not cursor.fetchone():
        conn.close()
        return False

    cursor.execute("DELETE FROM conversation_messages WHERE conversation_id = ?", (conversation_id,))
    cursor.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    conn.commit()
    conn.close()
    return True


def clear_conversations(user_id):
    """清空该用户的全部会话"""
    if not user_id:
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM conversations WHERE user_id = ?", (user_id,))
    ids = [r['id'] for r in cursor.fetchall()]
    for cid in ids:
        cursor.execute("DELETE FROM conversation_messages WHERE conversation_id = ?", (cid,))
    cursor.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))


    conn.commit()
    conn.close()


# ---------- 科研工作流 ----------

def create_workflow(user_id, goal, plan):
    """新建工作流，返回 id。plan 是规划出来的字典。"""
    import json as _json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO workflows (user_id, goal, plan_json, status) VALUES (?, ?, ?, ?)",
        (user_id, (goal or "")[:300], _json.dumps(plan, ensure_ascii=False), "pending")
    )
    wid = cursor.lastrowid
    conn.commit()
    conn.close()
    return wid


def update_workflow_status(workflow_id, status):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE workflows SET status = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f','now') "
        "WHERE id = ?", (status, workflow_id))
    conn.commit()
    conn.close()


def set_workflow_step(workflow_id, step_key, status, detail="", error=""):
    """写一步的状态。同一步重复写就是更新（UNIQUE(workflow_id, step_key)）。"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
                   INSERT INTO workflow_steps (workflow_id, step_key, status, detail, error)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id, step_key)
                       DO UPDATE SET status=excluded.status,
                                     detail=excluded.detail,
                                     error=excluded.error,
                                     updated_at=strftime('%Y-%m-%d %H:%M:%f','now')
                   """, (workflow_id, step_key, status, (detail or "")[:500], (error or "")[:500]))
    conn.commit()
    conn.close()


def save_workflow_result(workflow_id, context, report_md):
    """把工作流上下文（含 papers/统计/综述）和最终报告存下来，支持恢复。"""
    import json as _json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE workflows SET context_json = ?, report_md = ?, "
        "updated_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?",
        (_json.dumps(context, ensure_ascii=False, default=str), report_md or "", workflow_id))
    conn.commit()
    conn.close()


def get_workflow(workflow_id, user_id=None):
    """读取工作流（含步骤状态）。传 user_id 时校验归属。"""
    import json as _json
    conn = get_db()
    cursor = conn.cursor()
    if user_id is None:
        cursor.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
    else:
        cursor.execute("SELECT * FROM workflows WHERE id = ? AND user_id = ?",
                       (workflow_id, user_id))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    wf = dict(row)
    for k in ("plan_json", "context_json"):
        try:
            wf[k.replace("_json", "")] = _json.loads(wf.get(k) or "{}")
        except Exception:
            wf[k.replace("_json", "")] = {}
    cursor.execute(
        "SELECT step_key, status, detail, error, updated_at FROM workflow_steps "
        "WHERE workflow_id = ?", (workflow_id,))
    wf["steps"] = {r["step_key"]: dict(r) for r in cursor.fetchall()}
    conn.close()
    return wf


def get_workflows(user_id, limit=20):
    """工作流列表（摘要视图）。"""
    if not user_id:
        return []
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, goal, status, created_at, updated_at FROM workflows "
        "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def delete_workflow(workflow_id, user_id=None):
    conn = get_db()
    cursor = conn.cursor()
    if user_id is None:
        cursor.execute("SELECT id FROM workflows WHERE id = ?", (workflow_id,))
    else:
        cursor.execute("SELECT id FROM workflows WHERE id = ? AND user_id = ?",
                       (workflow_id, user_id))
    if not cursor.fetchone():
        conn.close()
        return False
    cursor.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (workflow_id,))
    cursor.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
    conn.commit()
    conn.close()
    return True
