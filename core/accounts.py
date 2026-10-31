"""账号存储：项目根目录的 `accounts.json`（文件，不是数据库）。

## 为什么改成文件

需求是"账号数据（昵称 / 密码 / 头像）直接存在项目里，暂时不用存数据库"。
一个 JSON 文件的好处很实际：

* 能直接用编辑器打开看、手工改一行；
* 备份 / 搬到另一台机器只拷这一个文件；
* 不会再出现"数据库还没加头像列 → 写头像 500 → 前端只看到网络错误"这类问题。

**密码仍然是 bcrypt 哈希，文件里绝不出现明文。** 这条不因为换存储而放宽。

## 文件结构

    {
      "version": 1,
      "next_id": 3,
      "users": [
        {"id": 1, "username": "x", "password": "$2b$12$...", "email": "",
         "avatar": "data:image/jpeg;base64,...", "created_at": "...",
         "last_login_at": "..."}
      ]
    }

## 并发与落盘

Flask 开发服务器是多线程的，所以用一把 `threading.Lock` 串行化"读-改-写"，
并且写入用「先写 .tmp → os.replace」的原子替换，避免进程被强杀时留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import bcrypt

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_PATH = os.path.join(_HERE, "accounts.json")
_LOCK = threading.RLock()
_OVERRIDE = None          # 测试用：set_path() 覆盖

VERSION = 1


def path():
    """当前账号文件路径。

    优先级：set_path() > 环境变量 ACCOUNTS_FILE > 测试临时目录 > 项目根目录。

    中间那条是给用例用的：`run_all.py` 会给每个套件设 `WF_TEST_TMP`，
    这时账号文件落到临时目录 —— 否则跑一遍测试就往你真实的 accounts.json
    里塞一堆 `test_user` 之类的账号（本项目真的被这么污染过）。
    """
    if _OVERRIDE:
        return _OVERRIDE
    env = (os.environ.get("ACCOUNTS_FILE") or "").strip()
    if env:
        return env
    test_tmp = (os.environ.get("WF_TEST_TMP") or "").strip()
    if test_tmp:
        return os.path.join(test_tmp, "_test_accounts.json")
    return _DEFAULT_PATH


def set_path(p):
    """测试用：把账号文件指到临时目录，别污染真实账号。"""
    global _OVERRIDE
    _OVERRIDE = p
    return p


def reset_path():
    global _OVERRIDE
    _OVERRIDE = None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _empty():
    return {"version": VERSION, "next_id": 1, "users": []}


def load():
    """读整个文件；不存在或坏了就返回空结构（不抛异常，坏文件先备份）。"""
    p = path()
    if not os.path.isfile(p):
        return _empty()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise ValueError("结构不对")
        data.setdefault("version", VERSION)
        data.setdefault("next_id", (max([u.get("id", 0) for u in data["users"]] or [0]) + 1))
        return data
    except Exception:
        # 坏文件别直接丢：改名留证，之后重新开始
        try:
            os.replace(p, p + ".broken")
        except Exception:
            pass
        return _empty()


def save(data):
    p = path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    # 每个进程/线程用独立的临时文件：以前固定叫 accounts.json.tmp，
    # 两个服务实例同时保存时会互相抢同一个 tmp，Windows 上 os.replace 直接报
    # PermissionError（WinError 5），登录/注册都变成 500。
    tmp = "%s.%s.%s.tmp" % (p, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        # Windows 上目标文件偶尔会被杀毒/索引器短暂占用；重试几次再报错。
        last = None
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return p
            except PermissionError as e:
                last = e
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
        raise last
    finally:
        # replace 成功后 tmp 已经不存在；失败时清掉，避免留下半成品。
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _public(user):
    """对外只给这三个字段（不返回密码哈希）。"""
    if not user:
        return None
    return {"id": user.get("id"), "username": user.get("username"),
            "email": user.get("email"), "created_at": user.get("created_at"),
            "last_login_at": user.get("last_login_at"),
            "avatar": user.get("avatar") or ""}


# ── 查询 ────────────────────────────────────────────────────

def user_exists(username):
    name = (username or "").strip()
    return any(u.get("username") == name for u in load()["users"])


def get_user_by_username(username):
    name = (username or "").strip()
    for u in load()["users"]:
        if u.get("username") == name:
            return _public(u)
    return None


def get_user_raw(username):
    """带密码哈希的内部记录（只给 login/verify 用）。"""
    name = (username or "").strip()
    for u in load()["users"]:
        if u.get("username") == name:
            return u
    return None


def get_user_by_id(user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    for u in load()["users"]:
        if u.get("id") == uid:
            return _public(u)
    return None


def list_users(limit=50):
    """只给用户名与时间，按"最近登录/创建"倒序。"""
    users = load()["users"]
    users = sorted(users, key=lambda u: (u.get("last_login_at") or u.get("created_at") or ""),
                   reverse=True)
    return [{"username": u.get("username"),
             "created_at": u.get("created_at"),
             "last_login_at": u.get("last_login_at")} for u in users[:limit]]


def count_users():
    return len(load()["users"])


# ── 写入 ────────────────────────────────────────────────────

def register_user(username, password, email=""):
    """注册。用户名重名返回 (False, 原因)。"""
    username = (username or "").strip()
    with _LOCK:
        data = load()
        if any(u.get("username") == username for u in data["users"]):
            return False, "用户名已存在"
        uid = int(data.get("next_id") or 1)
        data["users"].append({
            "id": uid,
            "username": username,
            "password": bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
            "email": (email or "").strip(),
            "avatar": "",
            "created_at": _now(),
            "last_login_at": None,
        })
        data["next_id"] = uid + 1
        save(data)
        return True, "注册成功"


def login_user(username, password):
    """校验密码并记录登录时间。成功返回 {id, username}。"""
    username = (username or "").strip()
    with _LOCK:
        data = load()
        for u in data["users"]:
            if u.get("username") != username:
                continue
            try:
                if not bcrypt.checkpw(password.encode("utf-8"),
                                      (u.get("password") or "").encode("utf-8")):
                    return None
            except Exception:
                return None
            u["last_login_at"] = _now()
            save(data)
            return {"id": u.get("id"), "username": u.get("username")}
    return None


def verify_password(user_id, password):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    for u in load()["users"]:
        if u.get("id") == uid:
            try:
                return bcrypt.checkpw(password.encode("utf-8"),
                                      (u.get("password") or "").encode("utf-8"))
            except Exception:
                return False
    return False


def update_username(user_id, new_username):
    new_username = (new_username or "").strip()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, "用户不存在"
    with _LOCK:
        data = load()
        if any(u.get("username") == new_username and u.get("id") != uid for u in data["users"]):
            return False, "用户名已存在"
        for u in data["users"]:
            if u.get("id") == uid:
                u["username"] = new_username
                save(data)
                return True, "用户名已更新"
    return False, "用户不存在"


def update_password(user_id, new_password):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, "用户不存在"
    with _LOCK:
        data = load()
        for u in data["users"]:
            if u.get("id") == uid:
                u["password"] = bcrypt.hashpw(
                    new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                save(data)
                return True, "密码已更新"
    return False, "用户不存在"


def update_avatar(user_id, data_url):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, "用户不存在"
    with _LOCK:
        data = load()
        for u in data["users"]:
            if u.get("id") == uid:
                u["avatar"] = (data_url or "").strip()
                save(data)
                return True, "头像已更新"
    return False, "用户不存在"


# ── 从旧数据库迁移 ──────────────────────────────────────────

def migrate_from_db(db_path=None):
    """把老 `literature.db` 里的 users 迁到 accounts.json。

    只在**账号文件还不存在**时做一次；已有文件就完全不动（免得覆盖用户后来的修改）。
    返回 {"migrated": n, "skipped": bool, "reason": str}。
    """
    if os.path.isfile(path()):
        return {"migrated": 0, "skipped": True, "reason": "accounts.json 已存在"}
    if not db_path or not os.path.isfile(db_path):
        return {"migrated": 0, "skipped": True, "reason": "没有旧数据库"}
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM users").fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return {"migrated": 0, "skipped": True, "reason": "旧库里没有 users 表"}
        users, next_id = [], 1
        for r in rows:
            d = dict(r)
            cols = set(d)
            users.append({
                "id": d.get("id") or next_id,
                "username": d.get("username"),
                "password": d.get("password"),
                "email": d.get("email") or "",
                "avatar": (d.get("avatar") or "") if "avatar" in cols else "",
                "created_at": d.get("created_at") or _now(),
                "last_login_at": (d.get("last_login_at") if "last_login_at" in cols else None),
            })
            next_id = max(next_id, (d.get("id") or 0) + 1)
        conn.close()
        save({"version": VERSION, "next_id": next_id, "users": users})
        return {"migrated": len(users), "skipped": False, "reason": ""}
    except Exception as e:  # noqa: BLE001 - 迁移失败不该拦住启动
        return {"migrated": 0, "skipped": True, "reason": f"{type(e).__name__}: {e}"}
