"""数据库操作模块 - 用户系统和搜索历史"""

import sqlite3
import bcrypt
from datetime import datetime
import json

DB_PATH = "literature.db"


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

    conn.commit()
    conn.close()
    print("✅ 数据库初始化完成！")


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
        return True, "注册成功"
    except Exception as e:
        conn.close()
        return False, f"注册失败: {str(e)}"


def login_user(username, password):
    """用户登录验证"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, username, password FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        return None

    # 验证密码
    if bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
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


def rename_conversation(user_id, conversation_id, title):
    """重命名会话"""
    if not user_id or not conversation_id or not title:
        return False

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
        (title[:50], conversation_id, user_id)
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


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
