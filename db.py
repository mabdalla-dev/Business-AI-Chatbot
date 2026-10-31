import sqlite3
from pathlib import Path

from flask import current_app, g


# --------------------------------------------------
# DATABASE CONFIGURATION
# --------------------------------------------------

DATABASE_PATH = Path(__file__).resolve().parent / "restaurant.db"


# --------------------------------------------------
# MIGRATION FUNCTIONS
# --------------------------------------------------

def migrate_messages_to_support_staff(db):
    """
    Update the existing messages table to allow three roles:
    user, assistant and staff.

    Existing messages are preserved, including their IDs,
    conversation IDs, message text and timestamps.
    """

    result = db.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'messages'
        """
    ).fetchone()

    if result is None or result[0] is None:
        return

    table_sql = result[0]

    # Do nothing if the existing table already allows staff.
    if "'staff'" in table_sql:
        return

    # Rebuild the table inside a recoverable transaction.
    db.execute("SAVEPOINT migrate_messages_to_staff")

    try:
        db.execute("DROP TABLE IF EXISTS messages_new")

        db.execute(
            """
            CREATE TABLE messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                conversation_id TEXT NOT NULL,

                role TEXT NOT NULL
                    CHECK (
                        role IN ('user', 'assistant', 'staff')
                    ),

                message TEXT NOT NULL,

                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            )
            """
        )

        # Copy every existing message without changing its data.
        db.execute(
            """
            INSERT INTO messages_new (
                id,
                conversation_id,
                role,
                message,
                created_at
            )
            SELECT
                id,
                conversation_id,
                role,
                message,
                created_at
            FROM messages
            """
        )

        # Replace the old table with the migrated table.
        db.execute("DROP TABLE messages")

        db.execute(
            """
            ALTER TABLE messages_new
            RENAME TO messages
            """
        )

        # Restore the index used for conversation message retrieval.
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_messages_conversation_id
            ON messages(conversation_id, id)
            """
        )

        db.execute(
            "RELEASE SAVEPOINT migrate_messages_to_staff"
        )

    except Exception:
        db.execute(
            "ROLLBACK TO SAVEPOINT migrate_messages_to_staff"
        )

        db.execute(
            "RELEASE SAVEPOINT migrate_messages_to_staff"
        )

        raise


def migrate_orders_add_staff_acknowledged_at(db):
    """Add the staff acknowledgement timestamp to existing orders."""

    columns = {
        row[1]
        for row in db.execute(
            "PRAGMA table_info(orders)"
        ).fetchall()
    }

    if "staff_acknowledged_at" not in columns:
        db.execute(
            "ALTER TABLE orders ADD COLUMN staff_acknowledged_at TEXT"
        )




def migrate_order_status_lifecycle(db):
    """Add explicit pending-review and rejected order states.

    In older versions, submitting a customer request set status='confirmed'.
    Since those versions did not have a separate restaurant-acceptance action,
    legacy confirmed rows are migrated to pending_review. Existing accepted
    orders cannot be distinguished from submitted requests in that schema.
    """
    result = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
    ).fetchone()
    if result is None or result[0] is None:
        return

    table_sql = result[0]
    if "pending_review" in table_sql and "rejected" in table_sql:
        return

    # Foreign keys must be disabled before starting the table rebuild, otherwise
    # dropping the old parent table can cascade-delete order_items.
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("SAVEPOINT migrate_order_status_lifecycle")

    try:
        db.execute("DROP TABLE IF EXISTS orders_new")
        db.execute(
            """
            CREATE TABLE orders_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN (
                        'draft',
                        'pending_review',
                        'confirmed',
                        'rejected',
                        'cancelled'
                    )),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                staff_acknowledged_at TEXT,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            )
            """
        )

        # Historical 'confirmed' orders represented customer submissions in the
        # old code. Move these to pending_review so they are not falsely treated
        # as accepted by the restaurant.
        db.execute(
            """
            INSERT INTO orders_new (
                id, conversation_id, status, created_at, updated_at,
                staff_acknowledged_at
            )
            SELECT
                id,
                conversation_id,
                CASE
                    WHEN status = 'confirmed' THEN 'pending_review'
                    ELSE status
                END,
                created_at,
                updated_at,
                staff_acknowledged_at
            FROM orders
            """
        )

        db.execute("DROP TABLE orders")
        db.execute("ALTER TABLE orders_new RENAME TO orders")
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_one_draft_order_per_conversation
            ON orders(conversation_id)
            WHERE status = 'draft'
            """
        )
        db.execute("RELEASE SAVEPOINT migrate_order_status_lifecycle")
        db.commit()

    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT migrate_order_status_lifecycle")
        db.execute("RELEASE SAVEPOINT migrate_order_status_lifecycle")
        db.rollback()
        raise

    finally:
        db.execute("PRAGMA foreign_keys = ON")



# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"]
        )

        g.db.row_factory = sqlite3.Row

        # Enable foreign-key enforcement for this connection.
        g.db.execute("PRAGMA foreign_keys = ON")

    return g.db


def close_db(e=None):
    db = g.pop("db", None)

    if db is not None:
        db.close()


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def init_db():
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.execute("PRAGMA foreign_keys = ON")

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,

                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            );


            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                conversation_id TEXT NOT NULL,

                role TEXT NOT NULL
                    CHECK (
                        role IN ('user', 'assistant', 'staff')
                    ),

                message TEXT NOT NULL,

                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            );


            CREATE INDEX IF NOT EXISTS
                idx_messages_conversation_id
            ON messages(conversation_id, id);


            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                conversation_id TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (
                        status IN (
                            'draft',
                            'pending_review',
                            'confirmed',
                            'rejected',
                            'cancelled'
                        )
                    ),

                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                staff_acknowledged_at TEXT,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            );


            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                order_id INTEGER NOT NULL,

                item_name TEXT NOT NULL,

                quantity INTEGER NOT NULL
                    CHECK (quantity > 0),

                unit_price INTEGER NOT NULL
                    CHECK (unit_price >= 0),

                FOREIGN KEY (order_id)
                    REFERENCES orders(id)
                    ON DELETE CASCADE,

                UNIQUE (order_id, item_name)
            );


            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_one_draft_order_per_conversation
            ON orders(conversation_id)
            WHERE status = 'draft';


            CREATE TABLE IF NOT EXISTS handoff_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                conversation_id TEXT NOT NULL,

                reason TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (
                        status IN (
                            'pending',
                            'in_progress',
                            'resolved',
                            'cancelled'
                        )
                    ),

                staff_notes TEXT,

                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            );


            CREATE INDEX IF NOT EXISTS
                idx_handoff_requests_status_created_at
            ON handoff_requests(status, created_at);
            """
        )

        # IMPORTANT:
        # CREATE TABLE IF NOT EXISTS does not change an existing
        # table's schema. Run the migration to update older databases.
        migrate_messages_to_support_staff(connection)
        migrate_orders_add_staff_acknowledged_at(connection)
        migrate_order_status_lifecycle(connection)

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# --------------------------------------------------
# GET OR CREATE A DRAFT ORDER
# --------------------------------------------------

def get_or_create_draft_order(conversation_id):
    db = get_db()

    # Check whether this conversation already has a draft order.
    row = db.execute(
        """
        SELECT id
        FROM orders
        WHERE conversation_id = ?
          AND status = 'draft'
        """,
        (conversation_id,)
    ).fetchone()

    # Reuse the existing draft order if one exists.
    if row is not None:
        return row["id"]

    # Otherwise, create a new draft order.
    cursor = db.execute(
        """
        INSERT INTO orders (
            conversation_id,
            status
        )
        VALUES (?, 'draft')
        """,
        (conversation_id,)
    )

    db.commit()

    return cursor.lastrowid


# --------------------------------------------------
# LOAD ORDER ITEMS FROM THE DATABASE
# --------------------------------------------------

def load_order_items(order_id):
    db = get_db()

    rows = db.execute(
        """
        SELECT item_name, quantity
        FROM order_items
        WHERE order_id = ?
        ORDER BY id
        """,
        (order_id,)
    ).fetchall()

    # Convert the database rows into our existing Python dictionary.
    order = {
        row["item_name"]: row["quantity"]
        for row in rows
    }

    return order


# --------------------------------------------------
# SAVE ORDER ITEMS TO THE DATABASE
# --------------------------------------------------

def save_order_items(order_id, order, menu):
    db = get_db()

    try:
        # Remove the existing items for this order.
        db.execute(
            """
            DELETE FROM order_items
            WHERE order_id = ?
            """,
            (order_id,)
        )

        # Insert the current items and their quantities.
        for item, quantity in order.items():

            if quantity <= 0:
                continue

            # Retrieve the current menu price.
            unit_price = menu[item]

            db.execute(
                """
                INSERT INTO order_items (
                    order_id,
                    item_name,
                    quantity,
                    unit_price
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    order_id,
                    item,
                    quantity,
                    unit_price
                )
            )

        # Update the order's last-modified timestamp.
        db.execute(
            """
            UPDATE orders
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,)
        )

        db.commit()

    except Exception:
        db.rollback()
        raise


# --------------------------------------------------
# CREATE A HUMAN-HANDOFF REQUEST
# --------------------------------------------------

def create_handoff_request(conversation_id, reason):
    db = get_db()

    reason = reason.strip()

    if not reason:
        raise ValueError("A handoff reason is required.")

    # Check whether this conversation already has an active request.
    existing_request = db.execute(
        """
        SELECT id
        FROM handoff_requests
        WHERE conversation_id = ?
          AND status IN ('pending', 'in_progress')
        ORDER BY id DESC
        LIMIT 1
        """,
        (conversation_id,)
    ).fetchone()

    # Avoid creating duplicate active requests.
    if existing_request is not None:
        return existing_request["id"], False

    try:
        cursor = db.execute(
            """
            INSERT INTO handoff_requests (
                conversation_id,
                reason
            )
            VALUES (?, ?)
            """,
            (conversation_id, reason)
        )

        db.commit()

        return cursor.lastrowid, True

    except Exception:
        db.rollback()
        raise


# --------------------------------------------------
# RETRIEVE HUMAN-HANDOFF REQUESTS
# --------------------------------------------------

def list_handoff_requests(status=None):
    db = get_db()

    allowed_statuses = {
        "pending",
        "in_progress",
        "resolved",
        "cancelled"
    }

    if status is not None and status not in allowed_statuses:
        raise ValueError("Invalid handoff request status.")

    if status is None:
        rows = db.execute(
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
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    else:
        rows = db.execute(
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
            WHERE status = ?
            ORDER BY created_at ASC, id ASC
            """,
            (status,)
        ).fetchall()

    return [dict(row) for row in rows]


# --------------------------------------------------
# UPDATE A HUMAN-HANDOFF REQUEST
# --------------------------------------------------

def update_handoff_request(request_id, status, staff_notes=None):
    db = get_db()

    allowed_statuses = {
        "pending",
        "in_progress",
        "resolved",
        "cancelled"
    }

    if status not in allowed_statuses:
        raise ValueError("Invalid handoff request status.")

    try:
        cursor = db.execute(
            """
            UPDATE handoff_requests
            SET
                status = ?,
                staff_notes = COALESCE(?, staff_notes),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, staff_notes, request_id)
        )

        db.commit()

        return cursor.rowcount > 0

    except Exception:
        db.rollback()
        raise