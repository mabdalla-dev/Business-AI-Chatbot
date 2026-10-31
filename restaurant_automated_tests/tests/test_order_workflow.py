"""First deterministic regression test for the restaurant order workflow.

Run from the project root with: python -m pytest -q
The test uses a temporary SQLite database and never calls the OpenAI API.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from contextlib import closing

import pytest

# app.py reads these environment variables while importing. Intent detection
# is mocked below, so no real OpenAI API call is made.
os.environ.setdefault("SECRET_KEY", "pytest-only-secret-key")
os.environ.setdefault("OPENAI_API_KEY", "pytest-not-a-real-api-key")

import app as app_module  # noqa: E402

SCHEMA = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'staff')),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX idx_messages_conversation_id ON messages(conversation_id, id);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'pending_review', 'confirmed', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    staff_acknowledged_at TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_one_draft_order_per_conversation
    ON orders(conversation_id) WHERE status = 'draft';
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    UNIQUE (order_id, item_name)
);
CREATE TABLE handoff_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'resolved', 'cancelled')),
    staff_notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX idx_handoff_requests_status_created_at
    ON handoff_requests(status, created_at);
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Give each test a fresh, isolated database and Flask test client."""
    database_path = tmp_path / "restaurant_test.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    monkeypatch.setitem(app_module.app.config, "DATABASE", str(database_path))

    with app_module.app.test_client() as test_client:
        test_client.database_path = database_path  # type: ignore[attr-defined]
        yield test_client


def make_action(
    intent: str, items: list[tuple[str, int]] | None = None
) -> dict[str, Any]:
    return {
        "intent": intent,
        "requested_time": None,
        "items": [
            {"item": item_name, "quantity": quantity}
            for item_name, quantity in (items or [])
        ],
    }


def menu_item_key(fragment: str) -> str:
    matches = [
        name for name in app_module.restaurant["menu"] if fragment in name.lower()
    ]
    assert matches, (
        f"Expected restaurant_data.py to include an item matching {fragment!r}. "
        f"Available items: {list(app_module.restaurant['menu'])}"
    )
    return matches[0]


def send(client, message: str):
    response = client.post("/chat", json={"message": message})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def get_conversation_id(client) -> str:
    with client.session_transaction() as session:
        value = session.get("conversation_id")
    assert value, "The chat should have created a conversation ID."
    return value


def connect_to_test_database(client) -> sqlite3.Connection:
    connection = sqlite3.connect(client.database_path)  # type: ignore[attr-defined]
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def test_removing_last_item_deletes_empty_draft(client, monkeypatch):
    """Removing the only item should remove the empty draft, not confirm it."""
    burger = menu_item_key("classic burger")
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Remove the burger": [make_action("remove_from_order", [(burger, 1)])],
    }

    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")
        return {"actions": message_actions[message]}

    # The tests use predictable intents instead of making live AI requests.
    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)

    send(client, "Add a burger")
    conv_id = get_conversation_id(client)

    with connect_to_test_database(client) as db:
        draft = db.execute(
            "SELECT id FROM orders WHERE conversation_id = ? AND status = 'draft'",
            (conv_id,),
        ).fetchone()
        assert draft is not None, "Adding an item should create a draft order."
        saved_item = db.execute(
            "SELECT quantity FROM order_items WHERE order_id = ? AND item_name = ?",
            (draft["id"], burger),
        ).fetchone()
        assert saved_item is not None and saved_item["quantity"] == 1

    send(client, "Remove the burger")

    with connect_to_test_database(client) as db:
        remaining_drafts = db.execute(
            "SELECT COUNT(*) FROM orders WHERE conversation_id = ? AND status = 'draft'",
            (conv_id,),
        ).fetchone()[0]
        remaining_items = db.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]

    assert (
        remaining_drafts == 0
    ), "An empty draft should be deleted after its last item is removed."
    assert remaining_items == 0, "The removed item should not remain in the database."


def test_submitting_order_sets_pending_review_and_preserves_items(client, monkeypatch):
    """Submitting an order should await staff acceptance and preserve items."""

    # Arrange: prepare the menu item and predictable AI actions.
    burger = menu_item_key("classic burger")

    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [make_action("place_order")],
    }

    # Replace the real AI intent detector for this test.
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # Act: add the burger and retrieve the conversation ID.
    send(client, "Add a burger")
    conv_id = get_conversation_id(client)

    # Check that the burger was saved in a draft.
    with connect_to_test_database(client) as db:
        draft = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'draft'
            """,
            (conv_id,),
        ).fetchone()

        assert draft is not None, "Adding an item should create a draft."

        saved_item = db.execute(
            """
            SELECT quantity, unit_price
            FROM order_items
            WHERE order_id = ?
              AND item_name = ?
            """,
            (draft["id"], burger),
        ).fetchone()

        assert saved_item is not None
        assert saved_item["quantity"] == 1

    # Place the order.
    placement_response = send(client, "Place my order")

    assert "awaiting staff review" in placement_response["reply"].lower()
    assert "not yet been accepted" in placement_response["reply"].lower()

    # Assert: the submitted request is pending staff review and retains its item.
    with connect_to_test_database(client) as db:
        remaining_drafts = db.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE conversation_id = ?
              AND status = 'draft'
            """,
            (conv_id,),
        ).fetchone()[0]

        pending_orders = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'pending_review'
            """,
            (conv_id,),
        ).fetchall()

        assert (
            remaining_drafts == 0
        ), "No draft should remain after the order is placed."

        assert (
            len(pending_orders) == 1
        ), "The conversation should have exactly one request awaiting review."

        pending_order_id = pending_orders[0]["id"]

        saved_item = db.execute(
            """
            SELECT quantity, unit_price
            FROM order_items
            WHERE order_id = ?
              AND item_name = ?
            """,
            (pending_order_id, burger),
        ).fetchone()

        assert saved_item is not None, "The submitted request should retain the burger."

        assert saved_item["quantity"] == 1

        assert saved_item["unit_price"] == (
            app_module.restaurant["menu"][burger]
        ), "The saved price should match the restaurant menu."


def test_view_order_after_submission_shows_latest_submitted_request(
    client, monkeypatch
):
    """Customers can still review their request after submitting it."""
    burger = menu_item_key("classic burger")
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [make_action("place_order")],
        "What is my order": [make_action("view_order")],
    }

    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")
        return {"actions": message_actions[message]}

    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)

    send(client, "Add a burger")
    placed = send(client, "Place my order")
    assert "awaiting staff review" in placed["reply"].lower()

    viewed = send(client, "What is my order")
    assert "Your most recently submitted order request" in viewed["reply"]
    assert "Classic Burger - £6.50" in viewed["reply"]
    assert "not yet been accepted" in viewed["reply"]


def test_ambiguous_quantity_change_lists_current_order_items(monkeypatch):
    """Ambiguous quantity changes should help the customer choose an item."""
    order = {
        menu_item_key("spicy chicken burger"): 2,
        menu_item_key("fries"): 2,
    }

    reply = app_module.execute_intent(
        make_action("ambiguous"),
        order,
        customer_message="make it 3",
    )

    assert "Which item's quantity would you like to change?" in reply
    assert "Spicy Chicken Burger" in reply
    assert "Fries" in reply


def test_adding_multiple_items_saves_both_correctly(client, monkeypatch):
    """Adding multiple items should save both correctly."""

    # 1. Arrange: get the menu keys for the burger and fries.
    burger = menu_item_key("classic burger")
    fries = menu_item_key("fries")

    message_actions = {
        "Add a burger and fries": [
            make_action("add_to_order", [(burger, 1), (fries, 1)])
        ],
    }

    # 2. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 3. Replace the real intent detector.
    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)

    # 4. Send one message requesting both items.
    send(client, "Add a burger and fries")
    conv_id = get_conversation_id(client)

    # 5. Query the database for the draft order and its items.
    with connect_to_test_database(client) as db:
        draft = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'draft'
            """,
            (conv_id,),
        ).fetchone()

        assert draft is not None, "Adding items should create a draft order."

        saved_items = db.execute(
            """
            SELECT item_name, quantity, unit_price
            FROM order_items
            WHERE order_id = ?
            """,
            (draft["id"],),
        ).fetchall()

        # Store the results by item name so we can check each item easily.
        items_by_name = {item["item_name"]: item for item in saved_items}

        # 6. Check that both items exist and there are no unexpected items.
        assert set(items_by_name.keys()) == {
            burger,
            fries,
        }, "The order should contain exactly the burger and fries."

        # Check the burger's quantity and price.
        assert items_by_name[burger]["quantity"] == 1

        assert items_by_name[burger]["unit_price"] == (
            app_module.restaurant["menu"][burger]
        ), "The burger price should match the restaurant menu."

        # Check the fries' quantity and price.
        assert items_by_name[fries]["quantity"] == 1

        assert items_by_name[fries]["unit_price"] == (
            app_module.restaurant["menu"][fries]
        ), "The fries price should match the restaurant menu."


def test_adding_items_separately_saves_both_correctly(client, monkeypatch):
    """Adding items in seperate messages should save both correctly."""

    # 1. Arrange: get the menu keys for the burger and fries.
    burger = menu_item_key("classic burger")
    fries = menu_item_key("fries")

    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Add fries": [make_action("add_to_order", [(fries, 1)])],
    }

    # 2. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 3. Replace the real intent detector.
    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)

    # 4. Send two messages requesting both items.
    send(client, "Add a burger")
    send(client, "Add fries")
    conv_id = get_conversation_id(client)

    # 5. Query the database for the draft order and its items.
    with connect_to_test_database(client) as db:
        draft = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'draft'
            """,
            (conv_id,),
        ).fetchall()

        assert len(draft) == 1
        draft_id = draft[0]["id"]

        saved_items = db.execute(
            """
            SELECT item_name, quantity, unit_price
            FROM order_items
            WHERE order_id = ?
            """,
            (draft_id,),
        ).fetchall()

        # Store the results by item name so we can check each item easily.
        items_by_name = {item["item_name"]: item for item in saved_items}

        # 6. Check that both items exist and there are no unexpected items.
        assert set(items_by_name.keys()) == {
            burger,
            fries,
        }, "The order should contain exactly the burger and fries."

        # Check the burger's quantity and price.
        assert items_by_name[burger]["quantity"] == 1

        assert items_by_name[burger]["unit_price"] == (
            app_module.restaurant["menu"][burger]
        ), "The burger price should match the restaurant menu."

        # Check the fries' quantity and price.
        assert items_by_name[fries]["quantity"] == 1

        assert items_by_name[fries]["unit_price"] == (
            app_module.restaurant["menu"][fries]
        ), "The fries price should match the restaurant menu."


def test_adding_same_item_twice_increases_quantity(client, monkeypatch):
    """Adding the same item twice should increase its quantity."""

    # 1. Arrange: Get the menu key for the Classic Burger.
    burger = menu_item_key("classic burger")

    # 2. Create message_actions.
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)

    # 5. Act: Send "Add a burger" twice.
    send(client, "Add a burger")
    send(client, "Add a burger")
    conv_id = get_conversation_id(client)

    # 6. Connect to the test database.
    with connect_to_test_database(client) as db:
        drafts = db.execute(
            """SELECT id FROM orders
            WHERE conversation_id = ?
            AND status = 'draft' """,
            (conv_id,),
        ).fetchall()

        assert len(drafts) == 1
        draft_id = drafts[0]["id"]

        # 7. Query order_items for the burger in this draft.
        saved_items = db.execute(
            """SELECT item_name, quantity, unit_price
            FROM order_items
            WHERE order_id = ?""",
            (draft_id,),
        ).fetchall()

        assert (
            len(saved_items) == 1
        ), "The order should contain exactly one burger record."
        items_by_name = {item["item_name"]: item for item in saved_items}

        # 8. Assert that exactly ONE database record exists for the burger.
        assert set(items_by_name.keys()) == {
            burger
        }, "The order should contain exactly the burger."

    # 9. Assert that the burger's quantity is 2.
    assert items_by_name[burger]["quantity"] == 2

    # 10. Assert that the saved burger price matches the restaurant menu.
    assert items_by_name[burger]["unit_price"] == (
        app_module.restaurant["menu"][burger]
    ), "The burger price should match the restaurant menu."


def test_removing_one_item_preserves_other_items(client, monkeypatch):
    """Removing one item should preserve the other items in the order."""

    # 1. Arrange: Get the menu keys for the burger and fries.
    burger = menu_item_key("classic burger")
    fries = menu_item_key("fries")

    # 2. Create message_actions.
    message_actions = {
        "Add a burger and fries": [
            make_action("add_to_order", [(burger, 1), (fries, 1)])
        ],
        "Remove the burger": [make_action("remove_from_order", [(burger, 1)])],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")
        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)

    # 5. Act: Send "Add a burger and fries".
    send(client, "Add a burger and fries")
    send(client, "Remove the burger")
    conv_id = get_conversation_id(client)

    # 6. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:
        drafts = db.execute(
            """SELECT id FROM orders
            WHERE conversation_id = ?
            AND status ='draft'""",
            (conv_id,),
        ).fetchall()

        assert len(drafts) == 1
        draft_id = drafts[0]["id"]

        # 7. Query order_items for all items in this draft.
        saved_items = db.execute(
            """SELECT item_name, quantity, unit_price
            FROM order_items
            WHERE order_id = ?""",
            (draft_id,),
        ).fetchall()

        assert len(saved_items) == 1, "The order should contain exactly one fries"

    # 8. Create a dictionary called items_by_name.
    items_by_name = {item["item_name"]: item for item in saved_items}

    # 9. Assert that exactly the fries remain in the order.
    assert set(items_by_name.keys()) == {
        fries
    }, "The order should contain exactly the fries"

    # 10. Assert that the fries quantity is still 1.
    assert items_by_name[fries]["quantity"] == 1

    # 11. Assert that the fries price matches the restaurant menu.
    assert items_by_name[fries]["unit_price"] == (
        app_module.restaurant["menu"][fries]
    ), "The fries price should match the restaurant menu."


def test_removing_one_unit_reduces_quantity_correctly(client, monkeypatch):
    """Removing one unit should reduce quantity without removing the item."""

    # 1. Arrange: Get the menu key for the Classic Burger.
    burger = menu_item_key("classic burger")

    # 2. Create message_actions.
    message_actions = {
        "Add two burgers": [make_action("add_to_order", [(burger, 2)])],
        "Remove one burger": [make_action("remove_from_order", [(burger, 1)])],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Replace the real intent detector.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add two burgers and retrieve the conversation ID.
    send(client, "Add two burgers")
    conv_id = get_conversation_id(client)

    # 6. Check the database BEFORE removing a burger.
    with closing(connect_to_test_database(client)) as db:
        drafts = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'draft'
            """,
            (conv_id,),
        ).fetchall()

        assert len(drafts) == 1, "There should be exactly one draft order."

        draft_id = drafts[0]["id"]

        # Retrieve the burger record.
        saved_items = db.execute(
            """
            SELECT item_name, quantity, unit_price
            FROM order_items
            WHERE order_id = ?
              AND item_name = ?
            """,
            (draft_id, burger),
        ).fetchall()

        # Verify that exactly one burger record exists.
        assert (
            len(saved_items) == 1
        ), "The order should contain exactly one burger record."

        # Verify the initial quantity is two.
        assert (
            saved_items[0]["quantity"] == 2
        ), "The burger quantity should initially be two."

    # 7. Act: Remove one burger.
    send(client, "Remove one burger")

    # 8. Check the database AFTER removing the burger.
    with closing(connect_to_test_database(client)) as db:
        drafts = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'draft'
            """,
            (conv_id,),
        ).fetchall()

        assert len(drafts) == 1, "There should still be exactly one draft order."

        draft_id = drafts[0]["id"]

        # Retrieve all remaining items in the draft.
        saved_items = db.execute(
            """
            SELECT item_name, quantity, unit_price
            FROM order_items
            WHERE order_id = ?
            """,
            (draft_id,),
        ).fetchall()

        # Verify exactly one item record remains.
        assert (
            len(saved_items) == 1
        ), "The order should contain exactly one item record."

        # Organise the database records by item name.
        items_by_name = {item["item_name"]: item for item in saved_items}

        # 9. Verify the burger remains in the order.
        assert set(items_by_name.keys()) == {
            burger
        }, "The order should still contain the burger."

        # 10. Verify the quantity has decreased from two to one.
        assert (
            items_by_name[burger]["quantity"] == 1
        ), "The burger quantity should be one after removal."

        # 11. Verify the burger's price remains correct.
        assert items_by_name[burger]["unit_price"] == (
            app_module.restaurant["menu"][burger]
        ), "The burger price should match the restaurant menu."


def test_empty_order_cannot_be_placed(client, monkeypatch):
    """An empty order should not be confirmed."""

    # 1. Arrange: Create message_actions.
    message_actions = {
        "Place my order": [make_action("place_order")],
    }

    # 2. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 3. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 4. Act: Send "Place my order" WITHOUT adding any items first.
    placement_response = send(client, "Place my order")
    conv_id = get_conversation_id(client)

    # 5. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:
        confirmed_orders = db.execute(
            """
                SELECT id
                FROM orders
                WHERE conversation_id = ?
                  AND status = 'pending_review'
                """,
            (conv_id,),
        ).fetchall()

        # 6. Assert that there are ZERO requests awaiting staff review.
        assert (
            len(confirmed_orders) == 0
        ), "There should be no pending requests for an empty order"

        # 7. Check that no items were saved for any order in this conversation.
        saved_items = db.execute(
            """
            SELECT oi.item_name
            FROM order_items AS oi
            JOIN orders AS o ON o.id = oi.order_id
            WHERE o.conversation_id = ?
            """,
            (conv_id,),
        ).fetchall()

        assert (
            len(saved_items) == 0
        ), "No items should be saved when placing an empty order."


def test_invalid_menu_item_is_not_saved(client, monkeypatch):

    # 1. Arrange: Configure an action that attempts to add an invalid item.
    message_actions = {
        "Add wings": [
            make_action(
                "add_to_order",
                items=[{"item": "Dragon Wings", "quantity": 1}],
            )
        ],
    }

    # Replace the real AI intent detector for this test.
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # Act: add the wings and retrieve the conversation ID.
    send(client, "Add wings")
    conv_id = get_conversation_id(client)

    # 5. Connect to the test database and check saved items.
    with closing(connect_to_test_database(client)) as db:
        saved_items = db.execute(
            """
            SELECT oi.item_name
            FROM order_items AS oi
            JOIN orders AS o ON o.id = oi.order_id
            WHERE o.conversation_id = ?
            """,
            (conv_id,),
        ).fetchall()

        # 6. Assert that no items were saved.
        assert (
            len(saved_items) == 0
        ), "An invalid menu item should not be saved to the order."


def test_zero_quantity_item_is_not_saved(client, monkeypatch):

    # 1. Arrange: Get a valid menu item and configure message_actions.
    fries = menu_item_key("fries")

    message_actions = {
        "Add fries": [
            make_action(
                "add_to_order",
                items=[{"item": fries, "quantity": 0}],
            )
        ],
    }

    # 2. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 3. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 4. Act: Attempt to add the item with a quantity of zero.
    send(client, "Add fries")
    conv_id = get_conversation_id(client)

    # 5. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:

        # 6. Query the database for fries saved in this conversation.
        saved_items = db.execute(
            """
            SELECT oi.item_name, oi.quantity
            FROM order_items AS oi
            JOIN orders AS o ON o.id = oi.order_id
            WHERE o.conversation_id = ?
              AND oi.item_name = ?
            """,
            (conv_id, fries),
        ).fetchall()

        # 7. Assert that the item was not saved.
        assert (
            len(saved_items) == 0
        ), "Fries with a quantity of zero should not be saved to the order."


def test_removing_item_not_in_order_preserves_existing_items(client, monkeypatch):

    # 1. Arrange: Get the actual menu names for a burger and fries.
    burger = menu_item_key("classic burger")
    fries = menu_item_key("fries")

    # 2. Configure message_actions for:
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Remove fries": [make_action("remove_from_order", [(fries, 1)])],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add the burger, then attempt to remove the fries.
    send(client, "Add a burger")
    send(client, "Remove fries")
    conv_id = get_conversation_id(client)

    # 6. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:

        # 7. Query the database to retrieve the items belonging to this conversation.
        saved_items = db.execute(
            """SELECT oi.item_name, oi.quantity
        FROM order_items AS oi
        JOIN orders AS o ON O.id = oi.order_id
        WHERE o.conversation_id =?
        """,
            (conv_id,),
        ).fetchall()

        items_by_name = {item["item_name"]: item for item in saved_items}

        # 8. Assert that exactly one item record exists.
        assert len(saved_items) == 1, "Only one record should exist."

        # 9. Assert that the burger remains in the order with quantity 1.
        assert burger in items_by_name, "The burger should remain in the order."

        assert items_by_name[burger]["quantity"] == 1

        # 10. Assert that fries were not saved to the order.
        assert fries not in items_by_name, "Fries should not be in the order."


def test_negative_quantity_item_is_not_saved(client, monkeypatch):

    # 1. Arrange: Get a valid menu item and configure message_actions.
    burger = menu_item_key("classic burger")
    message_actions = {
        "Add a burger": [
            make_action("add_to_order", items=[{"item": burger, "quantity": -1}])
        ]
    }

    # 2. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 3. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 4. Act: Attempt to add the item with a quantity of -1.
    send(client, "Add a burger")
    conv_id = get_conversation_id(client)

    # 6. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:
        # 7. Query the database to check whether the invalid item was saved.
        saved_items = db.execute(
            """SELECT oi.item_name, oi.quantity
            FROM order_items AS oi
            JOIN orders AS o ON o.id = oi.order_id
            WHERE o.conversation_id =?""",
            (conv_id,),
        ).fetchall()

        # 8. Assert that the negative-quantity item was not saved.
        assert len(saved_items) == 0, "The invalid quantity burger should not be saved"


def test_removing_zero_quantity_preserves_existing_item(client, monkeypatch):

    # 1. Arrange: Get the actual menu name for a burger.
    burger = menu_item_key("classic burger")

    # 2. Configure message_actions for adding one burger
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Remove a burger": [make_action("remove_from_order", [(burger, 0)])],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add one burger, then attempt to remove zero burgers.
    send(client, "Add a burger")
    send(client, "Remove a burger")
    conv_id = get_conversation_id(client)

    # 7. Connect to the test database and retrieve the saved items
    with closing(connect_to_test_database(client)) as db:
        saved_items = db.execute(
            """
    SELECT oi.item_name, oi.quantity
    FROM order_items AS oi
    JOIN orders AS o ON o.id = oi.order_id
    WHERE o.conversation_id = ?
    """,
            (conv_id,),
        ).fetchall()

    # 8. Assert that exactly one item record exists.
    assert len(saved_items) == 1, "Only one record should exist."

    # 9. Create items_by_name to organise the database results.
    items_by_name = {item["item_name"]: item for item in saved_items}

    # 10. Assert that the burger exists and its quantity is still 1.
    assert burger in items_by_name, "The burger should remain in the order"
    assert items_by_name[burger]["quantity"] == 1, "The burger quantity should remain 1"


def test_removing_more_units_than_available(client, monkeypatch):

    # 1. Arrange: Get the actual menu name for a burger.
    burger = menu_item_key("classic burger")

    # 2. Configure message_actions
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Remove two burgers": [make_action("remove_from_order", [(burger, 2)])],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add one burger, then attempt to remove two burgers.
    send(client, "Add a burger")
    send(client, "Remove two burgers")
    conv_id = get_conversation_id(client)

    # 7. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:

        # 8. Query the database to retrieve the remaining items
        saved_items = db.execute(
            """
        SELECT oi.item_name, oi.quantity
        FROM order_items AS oi
        JOIN orders AS o ON o.id = oi.order_id
        WHERE o.conversation_id = ?""",
            (conv_id,),
        ).fetchall()

        items_by_name = {item["item_name"]: item for item in saved_items}

        # 9. Assert that no invalid item quantity was saved.
        assert len(saved_items) == 0, "No records should exist."

        # 10. Assert that no items remain in the order.
        assert burger not in items_by_name, "The burger should not exist"

        # 11. Assert that the empty draft order was deleted.
        drafts = db.execute(
            """SELECT id
            FROM orders
            WHERE conversation_id =?
            AND status = 'draft'""",
            (conv_id,),
        ).fetchall()

        assert len(drafts) == 0, "Empty draft order should be deleted"


def test_cancelling_order_updates_status(client, monkeypatch):

    # 1. Arrange: Get the actual menu name for a burger.
    burger = menu_item_key("classic burger")

    # 2. Configure message_actions for adding and placing the order.
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [make_action("place_order")],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Replace the real AI intent detector.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add the burger and place the order.
    send(client, "Add a burger")
    send(client, "Place my order")
    conv_id = get_conversation_id(client)

    # 6. Retrieve the submitted request's ID.
    with closing(connect_to_test_database(client)) as db:
        pending_orders = db.execute(
            """
            SELECT id
            FROM orders
            WHERE conversation_id = ?
              AND status = 'pending_review'
            """,
            (conv_id,),
        ).fetchall()

        assert len(pending_orders) == 1, "Exactly one pending request should exist."
        order_id = pending_orders[0]["id"]

    # 7. Set up an authenticated staff session.
    csrf_token = "pytest-test-csrf-token"
    with client.session_transaction() as session:
        session["staff_logged_in"] = True
        session["csrf_token"] = csrf_token

    # 8. Staff must explicitly accept the request before it can be cancelled.
    response = client.post(
        f"/api/staff/orders/{order_id}/decision",
        json={"decision": "accept"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["status"] == "confirmed"

    # 9. Cancel the accepted order through the staff API.
    response = client.patch(
        f"/api/staff/orders/{order_id}",
        headers={"X-CSRF-Token": csrf_token},
    )

    # 10. Verify that the API reports successful cancellation.
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["status"] == "cancelled"

    # 10. Verify that the database records the cancelled status.
    with closing(connect_to_test_database(client)) as db:
        order = db.execute(
            """
            SELECT status
            FROM orders
            WHERE id = ?
              AND conversation_id = ?
            """,
            (order_id, conv_id),
        ).fetchone()

        assert order is not None, "The order should still exist."
        assert (
            order["status"] == "cancelled"
        ), "The order's status should be cancelled in the database."


def test_staff_dashboard_displays_pending_review_orders(client, monkeypatch):

    # 1. Arrange: Get the actual menu name for a burger.
    burger = menu_item_key("classic burger")

    # 2. Configure message_actions for adding a burger and placing an order
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [
            make_action(
                "place_order",
            )
        ],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add a burger
    send(client, "Add a burger")
    send(client, "Place my order")

    # 6. get conversation id
    conv_id = get_conversation_id(client)

    # 7. Set up an authenticated staff session.
    with client.session_transaction() as session:
        session["staff_logged_in"] = True

    # 8. Send a GET request to the pending-review orders endpoint.
    response = client.get("/api/staff/orders?status=pending_review")

    # 9. Assert that the response has HTTP status 200.
    assert response.status_code == 200, response.get_data(as_text=True)

    # 10. Retrieve the orders from the response JSON.
    orders = response.get_json()["orders"]

    # 11. Assert that exactly one order was returned.
    assert len(orders) == 1, "Exactly one pending request should be returned."

    # 12. Retrieve the order.
    order = orders[0]

    # 13. Assert that the order belongs to our conversation
    assert order["conversation_id"] == conv_id

    # 14. The customer has submitted the request, but staff have not accepted it.
    assert order["status"] == "pending_review"

    # 15. Assert that the order contains exactly one burger.
    assert len(order["items"]) == 1
    assert order["items"][0]["item_name"] == burger
    assert order["items"][0]["quantity"] == 1

    # 16. Assert that the unit price and total are correct.
    assert order["items"][0]["unit_price"] == (app_module.restaurant["menu"][burger])

    assert order["total"] == app_module.restaurant["menu"][burger]


def test_order_status_filtering(client, monkeypatch):

    # 1. Arrange: Get the actual menu name for a burger.
    burger = menu_item_key("classic burger")

    # 2. Configure message_actions for adding a burger and placing one
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [make_action("place_order")],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Add the burger and place the order.
    send(client, "Add a burger")
    send(client, "Place my order")
    conv_id = get_conversation_id(client)

    # 6. Set up an authenticated staff session.
    with client.session_transaction() as session:
        session["staff_logged_in"] = True

    # 7. A customer-submitted request appears under pending_review.
    response = client.get("/api/staff/orders?status=pending_review")
    assert response.status_code == 200, response.get_data(as_text=True)
    orders = response.get_json()["orders"]
    assert len(orders) == 1, "Exactly one pending request should be returned."
    assert orders[0]["status"] == "pending_review"
    order_id = orders[0]["id"]

    # 8. An authenticated staff member accepts the request explicitly.
    csrf_token = "pytest-test-csrf-token"
    with client.session_transaction() as session:
        session["csrf_token"] = csrf_token
    decision_response = client.post(
        f"/api/staff/orders/{order_id}/decision",
        json={"decision": "accept"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert decision_response.status_code == 200, decision_response.get_data(
        as_text=True
    )
    assert decision_response.get_json()["status"] == "confirmed"

    # 9. It no longer appears in pending review and is returned as confirmed.
    response = client.get("/api/staff/orders?status=pending_review")
    assert response.status_code == 200
    assert response.get_json()["orders"] == []

    response = client.get("/api/staff/orders?status=confirmed")
    assert response.status_code == 200
    accepted_orders = response.get_json()["orders"]
    assert len(accepted_orders) == 1
    assert accepted_orders[0]["status"] == "confirmed"

    # 10. No cancelled orders exist yet.
    response = client.get("/api/staff/orders?status=cancelled")
    assert response.status_code == 200
    assert response.get_json()["orders"] == []


def test_staff_can_decline_pending_order_and_prevent_second_decision(
    client, monkeypatch
):
    burger = menu_item_key("classic burger")
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [make_action("place_order")],
    }

    def fake_detect_intent(message, conversation, order):
        return {"actions": message_actions[message]}

    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)
    send(client, "Add a burger")
    send(client, "Place my order")
    conv_id = get_conversation_id(client)

    with closing(connect_to_test_database(client)) as db:
        row = db.execute(
            "SELECT id FROM orders WHERE conversation_id = ? AND status = 'pending_review'",
            (conv_id,),
        ).fetchone()
        assert row is not None
        order_id = row["id"]

    csrf_token = "pytest-test-csrf-token"
    with client.session_transaction() as session:
        session["staff_logged_in"] = True
        session["csrf_token"] = csrf_token

    response = client.post(
        f"/api/staff/orders/{order_id}/decision",
        json={"decision": "reject"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["status"] == "rejected"

    # A second conflicting decision must not silently overwrite the first.
    response = client.post(
        f"/api/staff/orders/{order_id}/decision",
        json={"decision": "accept"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 409

    with closing(connect_to_test_database(client)) as db:
        row = db.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "rejected"


def test_customer_order_status_question_uses_database_state(client, monkeypatch):
    burger = menu_item_key("classic burger")
    message_actions = {
        "Add a burger": [make_action("add_to_order", [(burger, 1)])],
        "Place my order": [make_action("place_order")],
    }

    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(
                f"Status question should be handled without AI intent detection: {message!r}"
            )
        return {"actions": message_actions[message]}

    monkeypatch.setattr(app_module, "detect_intent", fake_detect_intent)
    send(client, "Add a burger")
    send(client, "Place my order")

    # This reproduces the manual-demo wording that previously fell through to AI.
    response = send(client, "whats my order status ?")
    assert "awaiting staff review" in response["reply"].lower()
    assert "hasn't accepted it yet" in response["reply"].lower()

    # Pronoun-only follow-ups should still use the saved order state.
    response = send(client, "is it on the way?")
    assert "awaiting staff review" in response["reply"].lower()
    assert "hasn't accepted it yet" in response["reply"].lower()

    response = send(client, "Has my order been confirmed?")
    assert "awaiting staff review" in response["reply"].lower()
    assert "hasn't accepted it yet" in response["reply"].lower()

    conv_id = get_conversation_id(client)
    with closing(connect_to_test_database(client)) as db:
        row = db.execute(
            "SELECT id FROM orders WHERE conversation_id = ? AND status = 'pending_review'",
            (conv_id,),
        ).fetchone()
        assert row is not None
        order_id = row["id"]

    csrf_token = "pytest-test-csrf-token"
    with client.session_transaction() as session:
        session["staff_logged_in"] = True
        session["csrf_token"] = csrf_token

    accepted = client.post(
        f"/api/staff/orders/{order_id}/decision",
        json={"decision": "accept"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert accepted.status_code == 200

    # The same status question now reflects staff's actual decision.
    response = send(client, "whats my order status ?")
    assert "confirmed by the restaurant" in response["reply"].lower()

    # A confirmed order does not mean the demo can truthfully claim it is en route.
    response = send(client, "is it on the way?")
    assert "confirmed by the restaurant" in response["reply"].lower()
    assert "doesn't track preparation or delivery progress" in response["reply"].lower()


def test_human_handoff_request_is_saved(client, monkeypatch):

    # 1. Arrange: Choose a message in which the customer requests assitance from a human

    # 2. Configure message_actions to trigger the human_handoff intent.
    message_actions = {
        "Can I speak to someone": [
            make_action(
                "human_handoff",
            )
        ],
    }

    # 3. Define fake_detect_intent().
    def fake_detect_intent(message, conversation, order):
        if message not in message_actions:
            pytest.fail(f"No mocked intent configured for message: {message!r}")

        return {"actions": message_actions[message]}

    # 4. Use monkeypatch.setattr() to replace app_module.detect_intent.
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        fake_detect_intent,
    )

    # 5. Act: Send the customer's message.
    send(client, "Can I speak to someone")

    # 6. Retrieve the conversation ID.
    conv_id = get_conversation_id(client)

    # 7. Connect to the test database.
    with closing(connect_to_test_database(client)) as db:
        # 8. Query handoff_requests for requests belonging to this conversation.
        hand_off_requests = db.execute(
            """
            SELECT id, reason, status
            FROM handoff_requests
            WHERE conversation_id = ?
            """,
            (conv_id,),
        ).fetchall()

        # 9. Assert that exactly one handoff request exists.
        assert len(hand_off_requests) == 1, "Exactly one handoff request should exist."

        # 10. Assert that its status is 'pending'.
        assert (
            hand_off_requests[0]["status"] == "pending"
        ), "The handoff request should have pending status."

        # 11. Assert that the saved reason matches the customer's request.
        assert (
            hand_off_requests[0]["reason"] == "Can I speak to someone"
        ), "The saved reason should match the customer's message."


def test_opening_hours_response_is_grammatical(client, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "detect_intent",
        lambda message, conversation, order: {
            "actions": [make_action("opening_hours")]
        },
    )
    response = send(client, "What time do you open?")
    assert response["reply"] == "We're open daily from 12 noon to 10 pm."
