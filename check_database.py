import sqlite3

from db import DATABASE_PATH


connection = sqlite3.connect(DATABASE_PATH)

try:
    tables = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name;
        """
    ).fetchall()

    print("Database location:")
    print(DATABASE_PATH)

    print("\nTables found:")

    for table in tables:
        print("-", table[0])

finally:
    connection.close()