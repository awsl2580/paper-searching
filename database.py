"""数据库操作模块 - 用户系统和搜索历史"""

import json
import os
import sqlite3
from datetime import datetime

import bcrypt

# 数据库固定放在本文件旁边，不用相对路径。
#
# 原因：原来写的是 DB_PATH = "literature.db"，而 sqlite3.connect 会相对**当前工作目录**
# 解析，于是"数据库在哪儿"取决于从哪个目录启动。实测已经踩到：从 AIagent2\ 启动过
# 一次，就在那儿留下了一个 0 行的 literature.db，而真正的库在 AIagent2\AIagent1\ 下
# ——用户看到的现象是"我的收藏和历史都没了"。
# 用 __file__ 锚定后，无论从哪个目录启动都指向同一个库。
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "literature.db")

# 账号备份文件：和数据库放在同一个（项目）目录里。
#
# 为什么还要多这一份：literature.db 是 SQLite 二进制、而且在 .gitignore 里，
# 一旦工程目录被整体拷贝时漏掉它（或者库文件损坏/被删），用户就得重新注册。
# 这份 JSON 让"账号"能跟着项目走：启动时自动把里面本地库还没有的账号补进去，
# 登录时如果用户名在本地库找不到，也会来这里找一次。
#
# 安全说明：password 字段存的是 **bcrypt 哈希**，不是明文密码。
# 登录是拿哈希做比对，所以这份文件泄露不等于密码泄露（但仍不该提交到版本库，
# 见 .gitignore）。
#
# 可以用环境变量 PAPER_ACCOUNTS_FILE 改位置：测试就是这么把它指到临时目录的，
# 否则跑测试注册的账号会写进项目里真正的 accounts.json。
ACCOUNTS_FILE = ((os.getenv("PAPER_ACCOUNTS_FILE") or "").strip()
                 or os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json"))


def _safe_print(message):
    """控制台编码不是 UTF-8 时（Windows 重定向到文件常见）别让 print 把流程打断。"""
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            print(message.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


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

    conn.commit()
    conn.close()
    # 建表之后再补账号：老库升级上来时，accounts.json 里的账号也就一起回来了
    restored = import_accounts()
    if restored:
        _safe_print(f"已从 accounts.json 恢复 {restored} 个账号")
    _safe_print("✅ 数据库初始化完成！")


# ---------- 账号备份（跟着项目走的那份） ----------

def _has_users_table():
    """users 表在不在？（库文件被删/换目录时会不在）"""
    try:
        conn = get_db()
        try:
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def export_accounts(path=None):
    """把 users 表导出到项目里的 accounts.json（只写 bcrypt 哈希，不写明文密码）。

    每次注册成功后调用，保证这份文件和库一起更新。
    返回导出的账号数；失败返回 None（导出失败不能影响注册本身）。
    """
    target = path or ACCOUNTS_FILE
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT username, password, email, created_at FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "note": "由 database.export_accounts() 自动生成；password 是 bcrypt 哈希，不是明文。"
                "删掉本文件不影响已存在的 literature.db，但换目录/换机器时就找不回账号了。",
        "accounts": [
            {
                "username": r["username"],
                "password": r["password"],
                "email": r["email"] or "",
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }

    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)      # 原子替换：中途断电也不会留下半个文件
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return None
    return len(payload["accounts"])


def import_accounts(path=None):
    """把 accounts.json 里**本地库还没有**的账号补进 users 表，返回补进去的个数。

    密码哈希是原样搬过来的，不需要也不应该有明文密码 —— 登录时 bcrypt 直接比对。
    """
    target = path or ACCOUNTS_FILE
    if not os.path.exists(target):
        return 0
    # 库文件被删/换目录时这里可能是空的：先建表，否则 INSERT 直接报 no such table
    if not _has_users_table():
        init_db()
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0

    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return 0

    conn = get_db()
    added = 0
    try:
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            username = str(acc.get("username") or "").strip()
            password_hash = str(acc.get("password") or "").strip()
            if not username or not password_hash:
                continue
            exists = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            if exists:
                continue
            try:
                conn.execute(
                    "INSERT INTO users (username, password, email) VALUES (?, ?, ?)",
                    (username, password_hash, str(acc.get("email") or "").strip()),
                )
                added += 1
            except sqlite3.Error:
                continue        # 单个账号有问题就跳过，不影响其它账号
        conn.commit()
    finally:
        conn.close()
    return added


# ---------- 用户相关 ----------

def register_user(username, password, email=""):
    """注册新用户"""
    conn = get_db()
    cursor = conn.cursor()

    # 检查用户名是否已存在
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        return False, "用户名已存在"

    # 加密密码
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

    try:
        cursor.execute(
            "INSERT INTO users (username, password, email) VALUES (?, ?, ?)",
            (username, hashed.decode('utf-8'), email)
        )
        conn.commit()
        conn.close()
        # 注册成功就把账号同步到项目里的 accounts.json（失败不影响注册）
        export_accounts()
        return True, "注册成功"
    except Exception as e:
        conn.close()
        return False, f"注册失败: {str(e)}"


def find_user(username):
    """按用户名取一行（含密码哈希），给登录用。"""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT id, username, password FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()


def login_user(username, password):
    """用户登录验证。

    顺序：先查本地 literature.db；本地没有这个用户名时，去项目里的
    accounts.json 找一次（换目录/换机器/库被删的情况），找到就补进库再比对。
    """
    # 库文件被删/换目录：先按需建表，别让登录直接抛 no such table
    if not _has_users_table():
        init_db()

    user = find_user(username)

    if not user:
        # 本地库没有 -> 从项目里的账号备份找（找不到返回 0，直接判失败）
        if import_accounts():
            user = find_user(username)

    if not user:
        return None

    # 验证密码（bcrypt 比对哈希，不存在明文）
    try:
        ok = bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8'))
    except ValueError:
        # 哈希格式不认识（手工改坏了 accounts.json 之类）当作密码错误
        return None

    if ok:
        return {"id": user['id'], "username": user['username']}

    return None


def get_user_by_id(user_id):
    """根据ID获取用户信息"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, email, created_at FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return dict(user) if user else None


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


def append_message(user_id, conversation_id, role, content):
    """向会话追加一条消息；会话不存在或不属于该用户时返回 False"""
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

    cursor.execute(
        "INSERT INTO conversation_messages (conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'))",
        (conversation_id, role, content)
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
        "SELECT id, role, content, created_at FROM conversation_messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,)
    )
    messages = [dict(r) for r in cursor.fetchall()]
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
