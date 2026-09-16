"""小工具：直接打印数据库里的搜索历史。

走 database.get_db() 而不是自己 sqlite3.connect('literature.db')，
这样和 Web 服务用的是同一个库（database.DB_PATH 用 __file__ 锚定，
不受启动目录影响），也不会因为相对路径而静默打印空结果。
"""

import database as db

conn = db.get_db()
try:
    rows = conn.execute('''
        SELECT u.username, s.query, s.search_time
        FROM search_history s
        JOIN users u ON s.user_id = u.id
        ORDER BY s.search_time DESC
    ''').fetchall()
finally:
    conn.close()

if not rows:
    print("（数据库里还没有搜索历史）")
for row in rows:
    print(f"{row['username']} 搜了: {row['query']} 时间: {row['search_time']}")
