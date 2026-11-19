import sqlite3

# Test SQLite connection and table creation
conn = sqlite3.connect('jarvis_projects.db')
conn.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
conn.commit()
print("SQLite test: table created successfully")
conn.close()