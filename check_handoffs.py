import sqlite3

from db import DATABASE_PATH


connection = sqlite3.connect(DATABASE_PATH)

connection.row_factory = sqlite3.Row

try:
    rows = connection.execute(
        """
        SELECT
            id,
            conversation_id,
            reason,
            status,
            staff_notes,
            created_at,
            updated_at
        FROM handoff_requests
        ORDER BY id DESC
        """
    ).fetchall()

    print("========== HANDOFF REQUESTS ==========")

    if rows:
        for row in rows:
            print(dict(row))
    else:
        print("No handoff requests found.")

finally:
    connection.close()