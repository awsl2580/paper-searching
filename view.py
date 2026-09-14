import sqlite3

conn = sqlite3.connect('literature.db')
cursor = conn.cursor()

# 查看搜索历史
cursor.execute('''
    SELECT u.username, s.query, s.search_time 
    FROM search_history s
    JOIN users u ON s.user_id = u.id
    ORDER BY s.search_time DESC
''')
for row in cursor.fetchall():
    print(f"{row[0]} 搜了: {row[1]} 时间: {row[2]}")

conn.close()