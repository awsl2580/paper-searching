"""运行日志（运维视角）：谁在什么时候做了什么、哪一步失败、慢在哪里。

## 为什么单独一层，而不是直接 logging 写文件

* 需求是"在管理员界面里查"：按**级别 / 模块 / 关键词 / 时间**过滤、翻页。
  写文件只能 tail，还得再写一个读文件的页面，而且中文按行切分很麻烦。
* 但日志绝不允许拖慢业务：所有写入都是 **best-effort** —— 出任何异常都只往
  stderr 打一行，然后把异常吞掉。"记日志"把请求弄挂是最不能接受的失败模式。
* 也不允许把用户内容写进去：只记**长度 / 数量 / 错误摘要**，不记问题原文、
  不记密码、不记 token。管理员应该能看出"谁在什么时候问了多长的问题"，
  但不该在这里读到别人的聊天内容。

## 表结构

    app_logs(id, ts, level, module, message, user_id, ip, detail)

**惰性建表**（第一次用时 `CREATE TABLE IF NOT EXISTS`）：老库不用跑迁移，
也不去改 `database.py` 的建表流程 —— 日志是可选能力，不该让主流程为它让路。

## 用法

    import runlog
    runlog.info("chat", "开始问答", user_id=1, detail={"model": "deepseek-V4.1-flash"})
    runlog.warn("auth", "登录失败", detail={"username": "bob"})
    runlog.error("workflow", "第 3 步失败", detail={"step": "hypothesis"})
    rows = runlog.query(level="ERROR", module="chat", page=1, size=50)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import database as db

# 级别与模块都做成闭集合：界面上是下拉框，脏值会让人筛不出东西
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
MODULES = ("request", "auth", "chat", "workflow", "science", "admin", "system")

# 保留上限：超过就删最旧的。默认 2 万行（正常使用几个月都到不了）
MAX_ROWS = max(500, int(os.getenv("APP_LOG_MAX_ROWS") or 20000))
_MESSAGE_MAX = 500          # 单条消息上限
_DETAIL_MAX = 4000          # detail(JSON) 上限
_PRUNE_EVERY = 200          # 每写这么多条清一次旧日志

_LOCK = threading.Lock()
_WRITES = 0


# ── 内部工具 ─────────────────────────────────────────────────

def _now():
    """本地时间，带毫秒：同一秒内多条日志也能正确排序。"""
    t = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def _norm_level(value):
    """级别统一大写；不在闭集合里就当 INFO。"""
    v = value.strip().upper() if isinstance(value, str) else ""
    return v if v in LEVELS else "INFO"


def _norm_module(value):
    """模块统一小写（别把 request 归一成 REQUEST 再判失败，那是真踩过的 bug）。"""
    v = value.strip().lower() if isinstance(value, str) else ""
    return v if v in MODULES else "system"


def _client_ip():
    """取客户端 IP：优先 X-Forwarded-For（内网穿透/反代场景），否则 remote_addr。"""
    try:
        from flask import request
        fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return fwd or (request.remote_addr or "")
    except Exception:  # noqa: BLE001 - 不在请求上下文里（脚本/测试）
        return ""


def _db_file_exists():
    """库文件在不在。

    sqlite3.connect() 对不存在的路径会**立刻创建空文件**，所以这一步必须在
    连库之前做 —— 否则"库还没初始化"时先被我们造出一个 0 字节的 literature.db，
    init_db 之后看着是好的，但中间任何一次"复制真库"的检查都会拿到一个空库。
    """
    try:
        return bool(db.DB_PATH) and os.path.exists(db.DB_PATH)
    except Exception:  # noqa: BLE001
        return False


def _ensure_table(conn):
    # ⚠️ 先确认主库确实初始化过（有 users 表）再建日志表。
    # 为什么：sqlite3.connect() 对不存在的文件会**直接造一个空库**。如果服务是以
    # `import app; app.run()` 这种方式起的（跳过 __main__ 里的 init_db），
    # 第一次写日志就会造出一个"只有 app_logs 一张表"的半成品 literature.db，
    # 之后所有查用户的接口全报 no such table，连"老库升级"检查都会误判。
    # 这个坑真实踩过，所以宁可不记日志，也不许凭空造库。
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
                        ).fetchone():
        raise RuntimeError("数据库尚未初始化，暂不记录日志")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            module TEXT NOT NULL,
            message TEXT NOT NULL,
            user_id INTEGER,
            ip TEXT,
            detail TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_ts ON app_logs(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_level ON app_logs(level, ts)")


def _to_json(detail):
    if detail is None:
        return None
    try:
        if not isinstance(detail, (dict, list, str, int, float, bool)):
            detail = str(detail)
        text = json.dumps(detail, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 序列化不了就退化成字符串
        text = str(detail)
    return text[:_DETAIL_MAX]


# ── 写 ───────────────────────────────────────────────────────

def log(level, module, message, user_id=None, ip=None, detail=None):
    """写一条日志。**永不抛异常**（失败只往 stderr 说一句）。

    参数刻意都用中性值：level/module 会在闭集合里归一化，
    message 超长截断，detail 序列化成 JSON 后也截断。
    """
    global _WRITES
    try:
        lv = _norm_level(level)
        md = _norm_module(module)
        msg = str(message or "").replace("\r", " ").replace("\n", " ")[:_MESSAGE_MAX]
        entry = (
            _now(), lv, md, msg,
            int(user_id) if user_id is not None else None,
            (ip if ip is not None else _client_ip())[:64] or None,
            _to_json(detail),
        )
    except Exception:  # noqa: BLE001 - 连参数整理都失败，那就别记了
        return

    # WARN / ERROR 镜像到 stderr：控制台里也能看到关键事件（INFO 不刷屏）
    if lv in ("WARN", "ERROR"):
        try:
            sys.stderr.write("[%s] %-5s %-8s %s\n" % (entry[0], lv, md, msg))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

    try:
        if not _db_file_exists():
            return
        with _LOCK:
            conn = db.get_db()
            try:
                _ensure_table(conn)
                conn.execute(
                    "INSERT INTO app_logs (ts, level, module, message, user_id, ip, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", entry)
                conn.commit()
            finally:
                conn.close()
            _WRITES += 1
            need_prune = (_WRITES % _PRUNE_EVERY == 0)
        if need_prune:
            prune()
    except Exception as e:  # noqa: BLE001 - 日志写失败不能影响业务
        try:
            sys.stderr.write("[runlog] 写日志失败（忽略）：%s: %s\n" % (type(e).__name__, e))
        except Exception:  # noqa: BLE001
            pass


def debug(module, message, **kw):
    log("DEBUG", module, message, **kw)


def info(module, message, **kw):
    log("INFO", module, message, **kw)


def warn(module, message, **kw):
    log("WARN", module, message, **kw)


def error(module, message, **kw):
    log("ERROR", module, message, **kw)


# ── 读（管理员界面用）────────────────────────────────────────

def query(level=None, module=None, keyword=None, page=1, size=50):
    """分页查询，新的在前。

    返回 {"rows": [...], "total": n, "page": p, "pages": m, "size": size}
    任何异常都折叠成空结果 —— 管理员界面不该因为一条坏 SQL 整页 500。
    """
    try:
        page = max(1, int(page or 1))
        size = max(1, min(500, int(size or 50)))
    except (TypeError, ValueError):
        page, size = 1, 50

    where, args = [], []
    lv = (level or "").strip().upper()
    if lv in LEVELS:
        where.append("level = ?")
        args.append(lv)
    md = (module or "").strip().lower()
    if md in MODULES:
        where.append("module = ?")
        args.append(md)
    kw = (keyword or "").strip()
    if kw:
        where.append("(message LIKE ? OR detail LIKE ? OR ip LIKE ?)")
        like = "%" + kw + "%"
        args.extend([like, like, like])
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    if not _db_file_exists():
        return {"rows": [], "total": 0, "page": 1, "pages": 1, "size": size,
                "error": "数据库尚未初始化"}
    try:
        conn = db.get_db()
        try:
            _ensure_table(conn)
            total = conn.execute("SELECT COUNT(*) FROM app_logs" + clause, args).fetchone()[0]
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            rows = conn.execute(
                "SELECT id, ts, level, module, message, user_id, ip, detail "
                "FROM app_logs" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size]).fetchall()
        finally:
            conn.close()
        return {"rows": [dict(r) for r in rows], "total": total,
                "page": page, "pages": pages, "size": size}
    except Exception as e:  # noqa: BLE001
        return {"rows": [], "total": 0, "page": 1, "pages": 1, "size": size,
                "error": "%s: %s" % (type(e).__name__, e)}


def stats():
    """给管理员界面顶部用的小统计：总量 / 各级别 / 24 小时 / 各模块。"""
    out = {"total": 0, "by_level": {}, "last24h": 0, "by_module": []}
    if not _db_file_exists():
        return out
    try:
        conn = db.get_db()
        try:
            _ensure_table(conn)
            out["total"] = conn.execute("SELECT COUNT(*) FROM app_logs").fetchone()[0]
            for row in conn.execute("SELECT level, COUNT(*) c FROM app_logs GROUP BY level"):
                out["by_level"][row["level"]] = row["c"]
            since = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(time.time() - 86400))
            out["last24h"] = conn.execute(
                "SELECT COUNT(*) FROM app_logs WHERE ts >= ?", (since,)).fetchone()[0]
            out["by_module"] = [
                {"module": r["module"], "count": r["c"]}
                for r in conn.execute(
                    "SELECT module, COUNT(*) c FROM app_logs GROUP BY module "
                    "ORDER BY c DESC LIMIT 8")]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return out


def clear():
    """清空全部日志。返回删掉的行数（失败返回 -1）。"""
    if not _db_file_exists():
        return -1
    try:
        conn = db.get_db()
        try:
            _ensure_table(conn)
            n = conn.execute("SELECT COUNT(*) FROM app_logs").fetchone()[0]
            conn.execute("DELETE FROM app_logs")
            conn.commit()
        finally:
            conn.close()
        return n
    except Exception:  # noqa: BLE001
        return -1


def prune(keep=None):
    """只保留最新的 N 条。返回删掉的行数。

    下限 10 是防手滑（keep=0 会把日志全删光），但**不要**写死成 100 ——
    那样 `prune(keep=10)` 会被抬成 100，调用方要的语义就悄悄变了。
    """
    keep = max(10, int(keep) if keep is not None else MAX_ROWS)
    if not _db_file_exists():
        return 0
    try:
        conn = db.get_db()
        try:
            _ensure_table(conn)
            total = conn.execute("SELECT COUNT(*) FROM app_logs").fetchone()[0]
            if total <= keep:
                return 0
            conn.execute(
                "DELETE FROM app_logs WHERE id NOT IN "
                "(SELECT id FROM app_logs ORDER BY id DESC LIMIT ?)", (keep,))
            conn.commit()
        finally:
            conn.close()
        return total - keep
    except Exception:  # noqa: BLE001
        return 0
