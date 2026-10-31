import sqlite3

from db import DATABASE_PATH


connection = sqlite3.connect(DATABASE_PATH)

connection.row_factory = sqlite3.Row

try:
    # Display the saved orders.
    print("========== ORDERS ==========")

    orders = connection.execute(
        """
        SELECT
            id,
            conversation_id,
            status,
            created_at,
            updated_at
        FROM orders
        ORDER BY id
        """
    ).fetchall()

    if orders:
        for order in orders:
            print(dict(order))
    else:
        print("No orders found.")

    # Display the items belonging to those orders.
    print("\n========== ORDER ITEMS ==========")

    items = connection.execute(
        """
        SELECT
            order_id,
            item_name,
            quantity,
            unit_price,
            quantity * unit_price AS line_total
        FROM order_items
        ORDER BY order_id, id
        """
    ).fetchall()

    if items:
        for item in items:
            print(dict(item))
    else:
        print("No order items found.")

finally:
    connection.close()