import json
import uuid
import os
import re
import secrets

from functools import wraps
from werkzeug.security import check_password_hash
from flask import Flask, render_template, request, jsonify, session, redirect
from restaurant_data import restaurant
from openai import OpenAI
from db import (
    DATABASE_PATH,
    close_db,
    get_db,
    get_or_create_draft_order,
    load_order_items,
    save_order_items,
    create_handoff_request,
    list_handoff_requests,
    update_handoff_request,
)

app = Flask(__name__)

app.secret_key = os.environ["SECRET_KEY"]

app.config["DATABASE"] = str(DATABASE_PATH)

app.teardown_appcontext(close_db)

client = OpenAI()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")


# Frequently asked questions
faqs = {
    "opening_hours": [
        "open",
        "opening",
        "close",
        "closing",
        "hours",
        "when do you close",
        "when do you open",
    ],
    "location": ["address", "location", "located", "where are you"],
    "vegetarian": ["vegetarian", "veggie"],
    "phone": ["phone", "number", "call"],
    "delivery": ["delivery", "deliver", "do you deliver"],
    "payment": [
        "payment",
        "pay",
        "cash",
        "card",
        "contactless",
        "apple pay",
        "google pay",
    ],
}


# Written numbers
number_words = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


# --------------------------------------------------
# BASIC AI AND ORDER FUNCTIONS
# --------------------------------------------------


def format_price(price_in_minor_units):
    """Format an integer price stored in the currency's minor unit (pence for GBP)."""
    minor_units_per_major = int(restaurant.get("minor_units_per_major", 100))
    if minor_units_per_major <= 0:
        raise ValueError("minor_units_per_major must be a positive integer")

    amount = int(price_in_minor_units)
    major_units, minor_units = divmod(amount, minor_units_per_major)
    symbol = restaurant.get("currency_symbol", "£")
    return f"{symbol}{major_units:,}.{minor_units:02d}"


def is_order_status_question(message, conversation=None):
    """Return True when the customer is asking about an existing order's state.

    Order status is read from SQLite rather than guessed by the language model.
    Accept natural wording such as "what's my order status?", "whats my order
    status", "has it been confirmed?", and "is it on the way?".
    """
    text = re.sub(r"\s+", " ", str(message or "").lower().replace("’", "'")).strip()

    mentions_order = bool(
        re.search(r"\b(?:my|the|this|that)?\s*order(?:\s+request)?\b", text)
    )
    recent_context_mentions_order = any(
        "order" in str(item.get("message", "")).lower()
        for item in (conversation or [])[-12:]
        if isinstance(item, dict)
    )

    # The plain word "status" is enough when an order is mentioned. This
    # deliberately handles contractions and common shorthand such as "whats".
    asks_status_directly = bool(re.search(r"\bstatus\b", text))
    known_states = bool(
        re.search(
            r"\b(?:confirmed|accepted|approved|rejected|declined|cancelled|canceled)\b",
            text,
        )
    )
    delivery_progress = bool(
        re.search(
            r"\b(?:on the way|out for delivery|being prepared|preparing|ready|"
            r"ready for collection|ready to collect|arrived|delivered)\b",
            text,
        )
    )

    # Customers often shorten a follow-up to "what's the status now?" after
    # discussing an order. If order-related messages are recent, treat a generic
    # status question as referring to that order even when it omits "order" and
    # pronouns such as "it" or "that".
    refers_to_recent_order = recent_context_mentions_order and (
        asks_status_directly
        or bool(re.search(r"\b(?:it|that|my order|the order|order status)\b", text))
    )
    asks_about_state = asks_status_directly or known_states or delivery_progress

    # These are instructions to submit/cancel an order, not status questions.
    action_instruction = bool(
        re.match(
            r"^(?:please\s+)?(?:confirm|place|submit|cancel)\s+"
            r"(?:my|the|this)?\s*order\b",
            text,
        )
    )
    if action_instruction:
        return False

    return bool(
        (mentions_order and asks_about_state)
        or (refers_to_recent_order and asks_about_state)
    )


def get_latest_order_status_reply(conversation_id, customer_message=""):
    """Describe the latest order state using SQLite as the source of truth."""
    db = get_db()
    order = db.execute(
        """
        SELECT id, status
        FROM orders
        WHERE conversation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    if order is None:
        return "I couldn't find an order for this conversation. Add your items when you're ready to start an order."

    status = order["status"]
    if status == "draft":
        return "Your order is still a draft and hasn't been submitted for staff review yet."
    if status == "pending_review":
        return (
            "Your order request is awaiting staff review. "
            "The restaurant hasn't accepted it yet."
        )
    if status == "confirmed":
        progress_question = bool(
            re.search(
                r"\b(?:on the way|out for delivery|being prepared|preparing|ready|arrived|delivered)\b",
                str(customer_message or "").lower(),
            )
        )
        if progress_question:
            return (
                "Your order has been confirmed by the restaurant. "
                "This demo doesn't track preparation or delivery progress, "
                "so I can't verify whether it is on the way."
            )
        return "Your order has been confirmed by the restaurant."
    if status == "rejected":
        return "Unfortunately, the restaurant has declined your order request. Please contact the restaurant team if you need more information."
    if status == "cancelled":
        return "Your most recent order has been cancelled."

    app.logger.warning(
        "Encountered unexpected order status %r for order %s", status, order["id"]
    )
    return "I couldn't verify your order status right now. Please check with the restaurant team."


def ask_ai(question):
    restaurant_info = f"""
    Restaurant name: {restaurant['name']}
    Opening hours: {restaurant['opening_hours']}
    Address: {restaurant['address']}
    Vegetarian options: {restaurant['vegetarian_options']}
    Phone: {restaurant.get('phone') or 'Not provided'}
    Delivery: {restaurant['delivery']}
    Payment: {restaurant['payment']}

    Menu:
    """

    for item, price in restaurant["menu"].items():
        restaurant_info += f"{item.title()} - {format_price(price)}\n"

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=(
            "Do not invent prices, menu items, opening hours, policies, "
            "ingredients, allergen information, dietary suitability, or other facts. "
            "Do not infer that an item is gluten-free, allergen-safe, vegan, "
            "or suitable for a dietary or medical requirement unless that fact "
            "is explicitly provided. "
            "If the information is missing, clearly say you cannot confirm it "
            "from the available information and recommend contacting the "
            "restaurant team. Do not guess or invent a phone number. "
            "Answer only what is relevant to the customer's question. "
            "Do not repeat a general demonstration/sample disclaimer in each "
            "reply; the page already displays a notice."
        ),
        input=(restaurant_info + f"\nCustomer question: {question}"),
    )

    return response.output_text


def calculate_order_total(order):
    total = 0

    for item, quantity in order.items():
        price = restaurant["menu"][item]
        total += price * quantity

    return total


def get_order_summary(order):
    if not order:
        return "You haven't added anything to your order yet."

    order_items = []

    for item, quantity in order.items():
        price = restaurant["menu"][item]
        item_total = price * quantity

        order_items.append(f"{quantity} x {item.title()} - {format_price(item_total)}")

    total = calculate_order_total(order)

    return "Your order:\n" + "\n".join(order_items) + f"\nTotal: {format_price(total)}"


def add_to_order(order, item, quantity):
    if item in order:
        order[item] += quantity
    else:
        order[item] = quantity


# --------------------------------------------------
# LEGACY MESSAGE PROCESSOR
# --------------------------------------------------


def process_message(message, order):
    message = message.lower().strip()

    if message in ["hi", "hello", "hey", "hiya"]:
        return f"Hi! Welcome to {restaurant['name']}. How can I help you today?"

    elif message in ["thanks", "thank you", "thx"]:
        return "You're welcome! Is there anything else I can help you with?"

    elif message in ["bye", "goodbye", "see you", "see you later"]:
        return f"Thanks for contacting {restaurant['name']}! Have a great day."

    elif message in ["help", "what can you do", "how can you help"]:
        return (
            "I can help you with our menu, prices, opening hours, "
            "location, delivery, payment methods and orders."
        )

    elif any(keyword in message for keyword in faqs["opening_hours"]):
        return f"We're open daily from {restaurant['opening_hours']}."

    elif any(keyword in message for keyword in faqs["location"]):
        return f"We're located at {restaurant['address']}."

    elif any(keyword in message for keyword in faqs["vegetarian"]):
        return restaurant["vegetarian_options"]

    elif any(keyword in message for keyword in faqs["phone"]):
        phone = restaurant.get("phone", "").strip()
        if phone:
            return f"You can call us on {phone}."
        return "I don't have a phone number available. Please check the restaurant's official contact information."

    elif any(keyword in message for keyword in faqs["delivery"]):
        return restaurant["delivery"]

    elif any(keyword in message for keyword in faqs["payment"]):
        return restaurant["payment"]

    elif "menu" in message:
        menu_items = []

        for item, price in restaurant["menu"].items():
            menu_items.append(f"{item.title()} - {format_price(price)}")

        return "Our menu:\n" + "\n".join(menu_items)

    elif (
        "clear my order" in message
        or "cancel my order" in message
        or "start over" in message
    ):
        order.clear()
        return "Your order has been cleared. You can start again."

    elif (
        "place my order" in message
        or "place order" in message
        or "confirm my order" in message
        or "confirm order" in message
    ):
        if order:
            total = calculate_order_total(order)
            reply = (
                f"Your order request has been placed for staff review. "
                f"It has not yet been accepted by the restaurant. Total: {format_price(total)}."
            )
            order.clear()
            return reply

        return "You don't have anything in your order yet."

    elif "my order" in message or "what did i order" in message:
        return get_order_summary(order)

    elif "remove" in message or "cancel" in message:
        found_item = None

        for item in restaurant["menu"]:
            if item in message:
                found_item = item
                break

        if found_item and found_item in order:
            quantity = 1
            words = message.split()

            for word in words:
                if word.isdigit():
                    quantity = int(word)
                    break

                if word in number_words:
                    quantity = number_words[word]
                    break

            if quantity >= order[found_item]:
                del order[found_item]
                return f"Removed {found_item.title()} from your order."

            order[found_item] -= quantity

            return f"Removed {quantity} x {found_item.title()} " "from your order."

        elif found_item and found_item not in order:
            return f"{found_item.title()} isn't currently in your order."

        return "I couldn't find that item in your order."

    else:
        found_items = []

        for item in restaurant["menu"]:
            if item in message:
                found_items.append(item)

        if found_items:
            added_items = []
            words = message.split()

            for item in found_items:
                quantity = 1

                for index, word in enumerate(words):
                    if word.isdigit():
                        if index + 1 < len(words):
                            next_words = " ".join(words[index + 1 : index + 4])

                            if any(
                                menu_word in next_words for menu_word in item.split()
                            ):
                                quantity = int(word)
                                break

                    if word in number_words:
                        if index + 1 < len(words):
                            next_words = " ".join(words[index + 1 : index + 4])

                            if any(
                                menu_word in next_words for menu_word in item.split()
                            ):
                                quantity = number_words[word]
                                break

                add_to_order(order, item, quantity)

                item_total = restaurant["menu"][item] * quantity

                added_items.append(
                    f"{quantity} x {item.title()} - {format_price(item_total)}"
                )

            return "Added to your order:\n" + "\n".join(added_items)

        return ask_ai(message)


# --------------------------------------------------
# CUSTOMER CHAT
# --------------------------------------------------


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request body is required."}), 400

    message = data.get("message", "")

    if not isinstance(message, str):
        return jsonify({"error": "Message must be text."}), 400

    message = message.strip()

    if not message:
        return jsonify({"reply": "Please enter a message."}), 400

    if len(message) > 5000:
        return jsonify({"error": "Message cannot exceed 5000 characters."}), 400

    db = get_db()

    # Find or create the conversation associated with this customer session.
    conversation_id = session.get("conversation_id")

    if conversation_id is None:
        conversation_id = str(uuid.uuid4())

        db.execute("INSERT INTO conversations (id) VALUES (?)", (conversation_id,))

        db.commit()

        session["conversation_id"] = conversation_id

    # Check whether a handoff is already active.
    active_handoff = db.execute(
        """
        SELECT id
        FROM handoff_requests
        WHERE conversation_id = ?
          AND status IN ('pending', 'in_progress')
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    # During an active handoff, save customer messages without invoking AI
    # or repeating the same automatic acknowledgement.
    if active_handoff is not None:
        db.execute(
            """
            INSERT INTO messages (conversation_id, role, message)
            VALUES (?, 'user', ?)
            """,
            (conversation_id, message),
        )

        db.execute(
            """
            UPDATE conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (conversation_id,),
        )

        db.commit()

        return jsonify({"reply": "", "handoff_active": True})

    # Load conversation history for intent detection.
    rows = db.execute(
        """
        SELECT role, message
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id
        """,
        (conversation_id,),
    ).fetchall()

    conversation = [{"role": row["role"], "message": row["message"]} for row in rows]

    # Load the current draft order.
    draft_order = db.execute(
        """
        SELECT id
        FROM orders
        WHERE conversation_id = ?
          AND status = 'draft'
        """,
        (conversation_id,),
    ).fetchone()

    if draft_order is not None:
        order_id = draft_order["id"]
        order = load_order_items(order_id)
    else:
        order_id = None
        order = {}

    previous_order = order.copy()

    # Status questions must be answered from the database, not guessed by the AI.
    # This also means the customer sees staff decisions made after their last message.
    submitted_order = {}
    if is_order_status_question(message, conversation):
        intent_data = {"actions": []}
        reply = get_latest_order_status_reply(conversation_id, message)
    else:
        # Identify and execute the customer's requested actions.
        intent_data = detect_intent(message, conversation, order)
        reply = execute_actions(
            intent_data["actions"],
            order,
            conversation_id,
            message,
            submitted_order=submitted_order,
        )

    place_order_requested = any(
        action["intent"] == "place_order" for action in intent_data["actions"]
    )

    order_to_save = submitted_order if place_order_requested else order

    # Create a draft if this conversation has items but no draft yet.
    if order_id is None and order_to_save:
        order_id = get_or_create_draft_order(conversation_id)

    if order_id is not None:
        # Save the current items, replacing the previous saved item list.
        save_order_items(order_id, order_to_save, restaurant["menu"])

        if place_order_requested and order_to_save:
            # Submission creates a request for staff review; it is not acceptance.
            db.execute(
                """
                UPDATE orders
                SET status = 'pending_review',
                    updated_at = CURRENT_TIMESTAMP,
                    staff_acknowledged_at = NULL
                WHERE id = ?
                  AND status = 'draft'
                """,
                (order_id,),
            )
            db.commit()

    # Clean up every empty draft for this conversation. This also removes
    # empty drafts left behind by earlier versions of the order workflow.
    db.execute(
        """
        DELETE FROM orders
        WHERE conversation_id = ?
          AND status = 'draft'
          AND NOT EXISTS (
              SELECT 1
              FROM order_items
              WHERE order_items.order_id = orders.id
          )
        """,
        (conversation_id,),
    )
    db.commit()

    # Save the customer message.
    db.execute(
        """
        INSERT INTO messages (conversation_id, role, message)
        VALUES (?, 'user', ?)
        """,
        (conversation_id, message),
    )

    # Save the assistant response.
    db.execute(
        """
        INSERT INTO messages (conversation_id, role, message)
        VALUES (?, 'assistant', ?)
        """,
        (conversation_id, reply),
    )

    db.execute(
        """
        UPDATE conversations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (conversation_id,),
    )

    db.commit()

    # The current message may have created a handoff.
    active_handoff = db.execute(
        """
        SELECT 1
        FROM handoff_requests
        WHERE conversation_id = ?
          AND status IN ('pending', 'in_progress')
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    return jsonify({"reply": reply, "handoff_active": active_handoff is not None})


# --------------------------------------------------
# CUSTOMER CONVERSATION HISTORY AND POLLING
# --------------------------------------------------


@app.route("/history", methods=["GET"])
def get_history():
    conversation_id = session.get("conversation_id")

    if conversation_id is None:
        return jsonify({"messages": [], "handoff_active": False})

    db = get_db()

    rows = db.execute(
        """
        SELECT id, role, message, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()

    messages = [
        {
            "id": row["id"],
            "role": row["role"],
            "message": row["message"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    active_handoff = db.execute(
        """
        SELECT 1
        FROM handoff_requests
        WHERE conversation_id = ?
          AND status IN ('pending', 'in_progress')
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    return jsonify({"messages": messages, "handoff_active": active_handoff is not None})


@app.route("/messages/new", methods=["GET"])
def get_new_messages():
    conversation_id = session.get("conversation_id")

    if conversation_id is None:
        return jsonify({"messages": [], "handoff_active": False, "last_message_id": 0})

    try:
        after_id = int(request.args.get("after_id", "0"))

        if after_id < 0:
            raise ValueError

    except (TypeError, ValueError):
        return jsonify({"error": "Invalid message ID."}), 400

    db = get_db()

    rows = db.execute(
        """
        SELECT id, role, message, created_at
        FROM messages
        WHERE conversation_id = ?
          AND id > ?
        ORDER BY id ASC
        """,
        (conversation_id, after_id),
    ).fetchall()

    messages = [
        {
            "id": row["id"],
            "role": row["role"],
            "message": row["message"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    active_handoff = db.execute(
        """
        SELECT 1
        FROM handoff_requests
        WHERE conversation_id = ?
          AND status IN ('pending', 'in_progress')
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    last_message_id = messages[-1]["id"] if messages else after_id

    return jsonify(
        {
            "messages": messages,
            "handoff_active": active_handoff is not None,
            "last_message_id": last_message_id,
        }
    )


# --------------------------------------------------
# STAFF AUTHENTICATION
# --------------------------------------------------


@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if session.get("staff_logged_in"):
        return redirect("/staff")

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        expected_username = os.environ.get("STAFF_USERNAME")
        password_hash = os.environ.get("STAFF_PASSWORD_HASH")

        if not expected_username or not password_hash:
            return "Staff authentication is not configured.", 500

        if username == expected_username and check_password_hash(
            password_hash, password
        ):
            session["staff_logged_in"] = True
            return redirect("/staff")

        return render_template("login.html", error="Invalid username or password."), 401

    return render_template("login.html")


def staff_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not session.get("staff_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required."}), 401

            return redirect("/staff/login")

        return view_function(*args, **kwargs)

    return wrapped_view


@app.route("/staff")
@staff_required
def staff_dashboard():
    csrf_token = session.get("csrf_token")

    if csrf_token is None:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token

    return render_template("staff.html", csrf_token=csrf_token)


# --------------------------------------------------
# STAFF HANDOFF REQUESTS
# --------------------------------------------------


@app.route("/api/staff/handoffs", methods=["GET"])
@staff_required
def get_staff_handoffs():
    status = request.args.get("status")

    try:
        handoffs = list_handoff_requests(status=status)

    except ValueError:
        return jsonify({"error": "Invalid handoff status."}), 400

    return jsonify({"requests": handoffs})


# --------------------------------------------------
# STAFF ORDERS
# --------------------------------------------------


@app.route("/api/staff/orders", methods=["GET"])
@staff_required
def get_staff_orders():
    status = request.args.get("status", "pending_review")

    allowed_statuses = {"draft", "pending_review", "confirmed", "rejected", "cancelled"}

    if status not in allowed_statuses and status != "":
        return jsonify({"error": "Invalid order status."}), 400

    db = get_db()

    query = """
        SELECT o.id AS order_id, o.conversation_id, o.status,
           o.created_at, o.updated_at, o.staff_acknowledged_at,
           oi.item_name, oi.quantity, oi.unit_price
    FROM orders AS o
    LEFT JOIN order_items AS oi ON oi.order_id = o.id
"""

    params = []

    if status:
        query += " WHERE o.status = ?"
        params.append(status)

    query += """
        ORDER BY o.created_at DESC, o.id DESC, oi.id ASC
    """

    rows = db.execute(query, params).fetchall()

    orders_by_id = {}

    for row in rows:
        order_id = row["order_id"]

        if order_id not in orders_by_id:
            orders_by_id[order_id] = {
                "id": order_id,
                "conversation_id": row["conversation_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "staff_acknowledged_at": row["staff_acknowledged_at"],
                "items": [],
                "total": 0,
            }

        if row["item_name"] is not None:
            quantity = row["quantity"]
            unit_price = row["unit_price"]
            subtotal = quantity * unit_price

            orders_by_id[order_id]["items"].append(
                {
                    "item_name": row["item_name"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "subtotal": subtotal,
                }
            )

            orders_by_id[order_id]["total"] += subtotal

    return jsonify({"orders": list(orders_by_id.values())})


# --------------------------------------------------
# CANCEL A CUSTOMER ORDER
# --------------------------------------------------


@app.route("/api/staff/orders/<int:order_id>", methods=["PATCH"])
@staff_required
def cancel_staff_order(order_id):

    # Verify the CSRF token.
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 403

    db = get_db()

    # Check that the order exists.
    order = db.execute(
        """
        SELECT status
        FROM orders
        WHERE id = ?
        """,
        (order_id,),
    ).fetchone()

    if order is None:
        return jsonify({"error": "Order not found."}), 404

    # Cancellation is for orders the restaurant previously accepted.
    # Requests not yet accepted should be declined via the decision endpoint.
    if order["status"] != "confirmed":
        return (
            jsonify(
                {
                    "error": (
                        "Only orders accepted by the restaurant can be cancelled. "
                        "Decline a request that is still awaiting review."
                    )
                }
            ),
            409,
        )

    # Cancel the order without deleting its items or history.
    cursor = db.execute(
        """
        UPDATE orders
        SET status = 'cancelled',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND status = 'confirmed'
        """,
        (order_id,),
    )

    db.commit()

    if cursor.rowcount == 0:
        return jsonify({"error": "The order could not be cancelled."}), 409

    return jsonify({"success": True, "order_id": order_id, "status": "cancelled"})


@app.route("/api/staff/orders/<int:order_id>/decision", methods=["POST"])
@staff_required
def decide_staff_order(order_id):
    """Accept or decline a customer order request awaiting staff review."""
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request body is required."}), 400

    decision = data.get("decision")
    target_status = {"accept": "confirmed", "reject": "rejected"}.get(decision)
    if target_status is None:
        return jsonify({"error": "Decision must be 'accept' or 'reject'."}), 400

    db = get_db()
    order = db.execute(
        "SELECT status FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if order is None:
        return jsonify({"error": "Order not found."}), 404

    if order["status"] != "pending_review":
        return (
            jsonify(
                {
                    "error": "Only requests awaiting staff review can be accepted or declined."
                }
            ),
            409,
        )

    cursor = db.execute(
        """
        UPDATE orders
        SET status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending_review'
        """,
        (target_status, order_id),
    )
    db.commit()

    if cursor.rowcount != 1:
        return (
            jsonify(
                {
                    "error": "The order request was already updated. Refresh and try again."
                }
            ),
            409,
        )

    return jsonify(
        {
            "success": True,
            "order_id": order_id,
            "status": target_status,
            "decision": decision,
        }
    )


# --------------------------------------------------
# ACKNOWLEDGE OF CUSTOMER ORDERS BY STAFF
# --------------------------------------------------


@app.route("/api/staff/orders/<int:order_id>/acknowledge", methods=["POST"])
@staff_required
def acknowledge_staff_order(order_id):
    # Validate the CSRF token.
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 403

    db = get_db()

    # Find the requested order.
    order = db.execute(
        """
        SELECT status, staff_acknowledged_at
        FROM orders
        WHERE id = ?
        """,
        (order_id,),
    ).fetchone()

    # Reject requests for orders that do not exist.
    if order is None:
        return jsonify({"error": "Order not found."}), 404

    # Legacy endpoint: acknowledgement means seen/reviewed, not accepted.
    # The dashboard uses the explicit accept/decline decision endpoint instead.
    if order["status"] != "pending_review":
        return (
            jsonify(
                {"error": "Only requests awaiting staff review can be acknowledged."}
            ),
            409,
        )

    # Make repeated acknowledgement requests safe.
    if order["staff_acknowledged_at"] is not None:
        return jsonify(
            {
                "success": True,
                "already_acknowledged": True,
                "order_id": order_id,
                "staff_acknowledged_at": order["staff_acknowledged_at"],
            }
        )

    # Record the acknowledgement without overwriting an existing one.
    cursor = db.execute(
        """
        UPDATE orders
        SET staff_acknowledged_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND status = 'pending_review'
          AND staff_acknowledged_at IS NULL
        """,
        (order_id,),
    )

    db.commit()

    # Check that the update affected an order.
    if cursor.rowcount == 0:
        return jsonify({"error": "The order could not be acknowledged."}), 409

    # Retrieve the saved acknowledgement timestamp.
    updated_order = db.execute(
        """
        SELECT staff_acknowledged_at
        FROM orders
        WHERE id = ?
        """,
        (order_id,),
    ).fetchone()

    return jsonify(
        {
            "success": True,
            "already_acknowledged": False,
            "order_id": order_id,
            "staff_acknowledged_at": updated_order["staff_acknowledged_at"],
        }
    )


@app.route("/api/staff/handoffs/<int:request_id>", methods=["PATCH"])
@staff_required
def update_staff_handoff(request_id):
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 403

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request body is required."}), 400

    status = data.get("status")

    allowed_statuses = {"pending", "in_progress", "resolved", "cancelled"}

    if not isinstance(status, str) or status not in allowed_statuses:
        return jsonify({"error": "Invalid handoff status."}), 400

    try:
        updated = update_handoff_request(request_id=request_id, status=status)

    except ValueError:
        return jsonify({"error": "Invalid handoff status."}), 400

    if not updated:
        return jsonify({"error": "Handoff request not found."}), 404

    return jsonify({"success": True, "request_id": request_id, "status": status})


@app.route("/api/staff/handoffs/<int:request_id>/conversation", methods=["GET"])
@staff_required
def get_staff_handoff_conversation(request_id):
    db = get_db()

    handoff = db.execute(
        """
        SELECT conversation_id, staff_notes, status
        FROM handoff_requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()

    if handoff is None:
        return jsonify({"error": "Handoff request not found."}), 404

    conversation_id = handoff["conversation_id"]

    rows = db.execute(
        """
        SELECT id, role, message, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()

    messages = [
        {
            "id": row["id"],
            "role": row["role"],
            "message": row["message"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    return jsonify(
        {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "status": handoff["status"],
            "staff_notes": handoff["staff_notes"],
            "messages": messages,
        }
    )


@app.route("/api/staff/handoffs/<int:request_id>/messages/new", methods=["GET"])
@staff_required
def get_new_staff_conversation_messages(request_id):
    db = get_db()

    handoff = db.execute(
        """
        SELECT conversation_id
        FROM handoff_requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()

    if handoff is None:
        return jsonify({"error": "Handoff request not found."}), 404

    try:
        after_id = int(request.args.get("after_id", "0"))

        if after_id < 0:
            raise ValueError

    except (TypeError, ValueError):
        return jsonify({"error": "Invalid message ID."}), 400

    rows = db.execute(
        """
        SELECT id, role, message, created_at
        FROM messages
        WHERE conversation_id = ?
          AND id > ?
        ORDER BY id ASC
        """,
        (handoff["conversation_id"], after_id),
    ).fetchall()

    messages = [
        {
            "id": row["id"],
            "role": row["role"],
            "message": row["message"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    return jsonify(
        {
            "request_id": request_id,
            "messages": messages,
            "last_message_id": messages[-1]["id"] if messages else after_id,
        }
    )


@app.route("/staff/logout", methods=["POST"])
@staff_required
def staff_logout():
    expected_token = session.get("csrf_token")
    provided_token = request.form.get("csrf_token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return "Invalid or missing CSRF token.", 403

    # Preserve the customer's conversation ID when staff log out.
    session.pop("staff_logged_in", None)
    session.pop("csrf_token", None)

    return redirect("/staff/login")


@app.route("/conversation")
def get_conversation():
    conversation = session.get("conversation", [])

    return jsonify(conversation)


# --------------------------------------------------
# STAFF REPLIES
# --------------------------------------------------


@app.route("/api/staff/handoffs/<int:request_id>/reply", methods=["POST"])
@staff_required
def send_staff_handoff_reply(request_id):
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 403

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request body is required."}), 400

    message = data.get("message")

    if not isinstance(message, str):
        return jsonify({"error": "Message must be text."}), 400

    message = message.strip()

    if not message:
        return jsonify({"error": "Reply cannot be empty."}), 400

    if len(message) > 5000:
        return jsonify({"error": "Reply cannot exceed 5000 characters."}), 400

    db = get_db()

    handoff = db.execute(
        """
        SELECT conversation_id
        FROM handoff_requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()

    if handoff is None:
        return jsonify({"error": "Handoff request not found."}), 404

    conversation_id = handoff["conversation_id"]

    try:
        cursor = db.execute(
            """
            INSERT INTO messages (
                conversation_id,
                role,
                message
            )
            VALUES (?, 'staff', ?)
            """,
            (conversation_id, message),
        )

        message_id = cursor.lastrowid

        db.execute(
            """
            UPDATE conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (conversation_id,),
        )

        db.commit()

    except Exception:
        db.rollback()
        raise

    return jsonify(
        {
            "success": True,
            "request_id": request_id,
            "conversation_id": conversation_id,
            "id": message_id,
            "role": "staff",
            "message": message,
        }
    )


# --------------------------------------------------
# STAFF NOTES
# --------------------------------------------------


@app.route("/api/staff/handoffs/<int:request_id>/notes", methods=["POST"])
@staff_required
def save_staff_handoff_notes(request_id):
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if (
        not expected_token
        or not provided_token
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        return jsonify({"error": "Invalid or missing CSRF token."}), 403

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request body is required."}), 400

    staff_notes = data.get("staff_notes")

    if not isinstance(staff_notes, str):
        return jsonify({"error": "Staff notes must be text."}), 400

    staff_notes = staff_notes.strip()

    if len(staff_notes) > 5000:
        return jsonify({"error": "Staff notes cannot exceed 5000 characters."}), 400

    db = get_db()

    handoff = db.execute(
        """
        SELECT status
        FROM handoff_requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()

    if handoff is None:
        return jsonify({"error": "Handoff request not found."}), 404

    updated = update_handoff_request(
        request_id=request_id, status=handoff["status"], staff_notes=staff_notes
    )

    if not updated:
        return jsonify({"error": "Could not save staff notes."}), 500

    return jsonify(
        {"success": True, "request_id": request_id, "staff_notes": staff_notes}
    )


# --------------------------------------------------
# AI INTENT DETECTION
# --------------------------------------------------


def detect_intent(message, conversation, order):
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=(
            "You are an intent detection system for a restaurant customer-service assistant. "
            "Your job is to understand what the customer wants and return one or more actions "
            "and any relevant information.\n\n"
            "Possible intents:\n"
            "- greeting: the customer is greeting the assistant.\n"
            "- thanks: the customer is thanking the assistant.\n"
            "- goodbye: the customer is ending the conversation.\n"
            "- help: the customer is asking what the assistant can help with.\n"
            "- menu: the customer wants to see the full menu.\n"
            "- menu_item: the customer is asking about a specific menu item, such as its price or whether it is available.\n"
            "- opening_hours: the customer is asking about opening or closing times.\n"
            "- location: the customer is asking where the restaurant is located.\n"
            "- vegetarian: the customer is asking about vegetarian options.\n"
            "- phone: the customer wants the restaurant phone number.\n"
            "- delivery: the customer is asking about delivery.\n"
            "- payment: the customer is asking about payment methods.\n"
            "- add_to_order: the customer wants to add items to their order.\n"
            "- remove_from_order: the customer wants to remove items from their order.\n"
            "- change_quantity: the customer wants to change the quantity of an item already in their order.\n"
            "- view_order: the customer wants to see their current order.\n"
            "- clear_order: the customer wants to clear or cancel their entire order.\n"
            "- place_order: the customer wants to confirm or place their order.\n"
            "- ambiguous: the customer is asking for an item or category that could refer to multiple menu items, and the exact item cannot be determined.\n"
            "- unknown: the request does not match any supported intent, "
            "or the customer asks about restaurant information that is not "
            "covered by the available data.\n"
            "Use unknown for questions about gluten-free options, allergens, "
            "ingredients, promotions, or customizations when that information "
            "is not provided. Do not invent an answer or treat these as "
            "vegetarian questions unless the customer specifically asks "
            "about vegetarian options.\n\n"
            "For add_to_order, remove_from_order, and change_quantity, "
            "identify the menu items and quantities when possible.\n"
            "Use the provided menu catalogue and return the exact menu item names. "
            "If the customer's wording matches one available menu item, use that item "
            "rather than asking for clarification. Only use ambiguous if the wording "
            "could refer to two or more actual menu entries. Do not mark one clearly "
            "identified item ambiguous just because another item in the same message "
            "needs clarification.\n\n"
            "For menu_item, identify the specific menu item the customer is asking about "
            "and place it in the items array with quantity 1.\n\n"
            "For change_quantity, the quantity represents the NEW desired quantity, "
            "not an amount to add. For example, if the customer says "
            "'Actually make that three' after ordering two burgers, "
            "identify the burger and quantity 3.\n\n"
            "If the customer refers to a menu category or vague item that could match "
            "multiple menu items, do not guess which item they mean. Use the ambiguous intent instead.\n\n"
            "If the previous assistant message asked the customer to clarify an ambiguous "
            "menu item, and the customer provides a specific menu item in their next "
            "message, treat that message as the customer's answer to the clarification "
            "and identify the appropriate order intent.\n\n"
            "A single customer message may contain multiple actions. "
            "Identify every action in the order the customer requested them. "
            "Do not combine separate actions into one action.\n\n"
            "- human_handoff: the customer wants to speak to a real person, staff member, manager, or someone from the restaurant team.\n"
            "If the customer asks to speak to a human or requests help from a real staff member, "
            "identify the action as human_handoff, even when the request is phrased casually. "
            "Do not use human_handoff merely because the customer mentions staff without "
            "requesting human assistance.\n\n"
            "Do not answer the customer. Only identify the actions and extract relevant information."
        ),
        input=(
            "Available menu items and prices (use these exact item names):\n"
            + json.dumps(
                {
                    name: format_price(price)
                    for name, price in restaurant["menu"].items()
                },
                ensure_ascii=False,
            )
            + "\n\nConversation history:\n"
            + json.dumps(conversation, ensure_ascii=False)
            + "\n\nCurrent order:\n"
            + json.dumps(order, ensure_ascii=False)
            + "\n\nCurrent customer message:\n"
            + message
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "restaurant_actions",
                "description": (
                    "The customer's restaurant-related actions and relevant information."
                ),
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "intent": {
                                        "type": "string",
                                        "enum": [
                                            "greeting",
                                            "thanks",
                                            "goodbye",
                                            "help",
                                            "menu",
                                            "menu_item",
                                            "opening_hours",
                                            "location",
                                            "vegetarian",
                                            "phone",
                                            "delivery",
                                            "payment",
                                            "human_handoff",
                                            "add_to_order",
                                            "remove_from_order",
                                            "change_quantity",
                                            "view_order",
                                            "clear_order",
                                            "place_order",
                                            "ambiguous",
                                            "unknown",
                                        ],
                                    },
                                    "requested_time": {"type": ["string", "null"]},
                                    "items": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "item": {"type": "string"},
                                                "quantity": {"type": "integer"},
                                            },
                                            "required": ["item", "quantity"],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "required": ["intent", "requested_time", "items"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["actions"],
                    "additionalProperties": False,
                },
            }
        },
    )

    return json.loads(response.output_text)


# --------------------------------------------------
# MENU AND INTENT EXECUTION
# --------------------------------------------------


def normalize_menu_item(item):
    item = item.lower().strip()

    if item.endswith("s"):
        singular = item[:-1]

        if singular in restaurant["menu"]:
            return singular

    if item in restaurant["menu"]:
        return item

    return None


def execute_intent(intent_data, order, conversation_id=None, customer_message=None):
    intent = intent_data["intent"]

    if intent == "greeting":
        return f"Hi! Welcome to {restaurant['name']}. How can I help you today?"

    elif intent == "thanks":
        return "You're welcome! Is there anything else I can help you with?"

    elif intent == "goodbye":
        return "Thanks for visiting! Have a great day."

    elif intent == "help":
        return (
            "I can help you with our menu, prices, opening hours, "
            "location, delivery, payment methods and orders."
        )

    elif intent == "menu":
        menu_items = []

        for item, price in restaurant["menu"].items():
            menu_items.append(f"{item.title()} - {format_price(price)}")

        return "Our menu:\n" + "\n".join(menu_items)

    elif intent == "menu_item":
        if not intent_data["items"]:
            return "Which menu item would you like to know about?"

        item = normalize_menu_item(intent_data["items"][0]["item"])

        if item is None:
            return "I couldn't find that item on our menu."

        price = restaurant["menu"][item]

        return f"{item.title()} - {format_price(price)}"

    elif intent == "opening_hours":
        return f"We're open daily from {restaurant['opening_hours']}."

    elif intent == "location":
        return f"We're located at {restaurant['address']}."

    elif intent == "vegetarian":
        return restaurant["vegetarian_options"]

    elif intent == "phone":
        phone = restaurant.get("phone", "").strip()
        if phone:
            return f"Our phone number is {phone}."
        return "I don't have a phone number available. Please check the restaurant's official contact information."

    elif intent == "delivery":
        return restaurant["delivery"]

    elif intent == "payment":
        return restaurant["payment"]

    elif intent == "human_handoff":
        if conversation_id is None:
            return "I couldn't submit your request right now. Please try again."

        reason = (customer_message or "").strip()

        if not reason:
            reason = "Customer requested human assistance."

        request_id, created = create_handoff_request(conversation_id, reason)

        if created:
            return (
                "I've recorded your request for the restaurant team. "
                "A member of staff will respond here as soon as possible. "
                "You can send any additional details while you wait."
            )

        return (
            "Your request is already recorded for this conversation. "
            "The restaurant team will need to review it."
        )

    elif intent == "add_to_order":
        added_items = []

        for item_data in intent_data["items"]:
            item = normalize_menu_item(item_data["item"])
            quantity = item_data["quantity"]

            if item is None or quantity <= 0:
                continue

            add_to_order(order, item, quantity)

            item_total = restaurant["menu"][item] * quantity

            added_items.append(
                f"{quantity} x {item.title()} - {format_price(item_total)}"
            )

        if not added_items:
            return "I couldn't find those items on our menu."

        return "Added to your order:\n" + "\n".join(added_items)

    elif intent == "remove_from_order":
        removed_items = []

        for item_data in intent_data["items"]:
            item = normalize_menu_item(item_data["item"])
            quantity = item_data["quantity"]

            if item is None or quantity <= 0:
                continue

            if item not in order:
                continue

            if quantity >= order[item]:
                del order[item]

                removed_items.append(f"Removed all {item.title()} from your order.")

            else:
                order[item] -= quantity

                removed_items.append(
                    f"Removed {quantity} x {item.title()} from your order."
                )

        if not removed_items:
            return "I couldn't find those items in your order."

        return "\n".join(removed_items)

    elif intent == "change_quantity":
        changed_items = []

        for item_data in intent_data["items"]:
            item = normalize_menu_item(item_data["item"])
            quantity = item_data["quantity"]

            if item is None or quantity <= 0:
                continue

            if item not in order:
                continue

            order[item] = quantity

            changed_items.append(f"Changed {item.title()} to {quantity}.")

        if not changed_items:
            return "I couldn't find those items in your order."

        return "\n".join(changed_items)

    elif intent == "view_order":
        if order:
            return (
                "Your current draft order:\n"
                + get_order_summary(order)
                + "\n\nThis order has not been submitted to the restaurant yet."
            )

        # With no active draft, show the latest submitted order and its real status.
        if conversation_id is not None:
            db = get_db()
            last_order = db.execute(
                """
                SELECT id, status
                FROM orders
                WHERE conversation_id = ?
                  AND status != 'draft'
                ORDER BY id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()

            if last_order is not None:
                submitted_order = load_order_items(last_order["id"])
                status_messages = {
                    "pending_review": "Status: awaiting staff review. It has not yet been accepted by the restaurant.",
                    "confirmed": "Status: confirmed by the restaurant.",
                    "rejected": "Status: declined by the restaurant.",
                    "cancelled": "Status: cancelled.",
                }
                status_message = status_messages.get(
                    last_order["status"],
                    "Status: please check with the restaurant team.",
                )
                if submitted_order:
                    return (
                        "Your most recently submitted order request:\n"
                        + get_order_summary(submitted_order)
                        + "\n\n"
                        + status_message
                    )
                if last_order["status"] == "cancelled":
                    return "Your most recent order request was cancelled. You can start a new order."

        return get_order_summary(order)

    elif intent == "clear_order":
        order.clear()
        return "Your order has been cleared. You can start again."

    elif intent == "place_order":
        if not order:
            return (
                "Your order is empty. "
                "Please add something before placing your order."
            )

        order_summary = get_order_summary(order)
        order.clear()

        return (
            "Your order request has been submitted and is awaiting staff review. "
            "It has not yet been accepted by the restaurant.\n\n" + order_summary
        )

    elif intent == "ambiguous":
        # Ask about the item the detector found unclear instead of assuming it is a burger.
        unclear_items = []
        for item_data in intent_data.get("items", []):
            item_name = str(item_data.get("item", "")).strip()
            if item_name and item_name not in unclear_items:
                unclear_items.append(item_name)

        if len(unclear_items) == 1:
            unclear_name = unclear_items[0]
            search_terms = set(unclear_name.lower().replace("-", " ").split())
            matching_menu_items = []

            for menu_item, price in restaurant["menu"].items():
                menu_terms = set(menu_item.lower().replace("-", " ").split())
                if (
                    unclear_name.lower() in menu_item.lower()
                    or search_terms.intersection(menu_terms)
                ):
                    matching_menu_items.append((menu_item, price))

            if matching_menu_items:
                choices = "\n".join(
                    f"- {menu_item.title()} — {format_price(price)}"
                    for menu_item, price in matching_menu_items
                )
                return f"Which {unclear_name} would you like?\n{choices}"

            return (
                f"Which item did you mean by '{unclear_name}'? "
                "Please choose the exact item from the menu."
            )

        if unclear_items:
            choices = ", ".join(unclear_items)
            return (
                f"Could you clarify which item you mean: {choices}? "
                "Please choose the exact item from the menu."
            )

        if order:
            current_items = "\n".join(
                f"- {quantity} x {item.title()}" for item, quantity in order.items()
            )
            return (
                "Which item's quantity would you like to change? "
                "Your current order contains:\n" + current_items
            )

        return (
            "I'm not sure which menu item you mean. "
            "Could you tell me the exact item name from the menu?"
        )

    elif intent == "unknown":
        try:
            return ask_ai(customer_message or "")
        except Exception:
            app.logger.exception("AI fallback failed for an unknown customer question")

            return (
                "I'm sorry, but I can't confirm that information "
                "right now. Please contact the restaurant team "
                "to check."
            )

    else:
        return (
            "Sorry, I didn't quite understand that. "
            "I can help with our menu, restaurant information "
            "and your order."
        )


def execute_actions(
    actions, order, conversation_id=None, customer_message=None, submitted_order=None
):
    replies = []
    order_changed = False

    place_order_requested = any(
        action.get("intent") == "place_order" for action in actions
    )
    ambiguous_action_requested = any(
        action.get("intent") == "ambiguous" for action in actions
    )

    for action in actions:

        # Preserve the items before place_order clears the current order.
        if (
            submitted_order is not None
            and action.get("intent") == "place_order"
            and order
        ):
            submitted_order.clear()
            submitted_order.update(order)

        reply = execute_intent(action, order, conversation_id, customer_message)

        replies.append(reply)

        if action["intent"] in ["add_to_order", "remove_from_order", "change_quantity"]:
            order_changed = True

    # Don't append an empty order summary after a placement.
    if order_changed and not place_order_requested and not ambiguous_action_requested:
        replies.append(get_order_summary(order))

    return "\n\n".join(replies)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
