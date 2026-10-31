from playwright.sync_api import Page, expect
import json


def test_customer_chat_page_loads(page: Page):

    # 1. Open the restaurant chatbot website.
    page.goto("http://127.0.0.1:5000")

    # 2. Check that the page title is correct.
    expect(page).to_have_title("The Sample Grill | AI Assistant Demo")

    # 3. Check that the main heading is visible.
    expect(page.get_by_role("heading", name="The Sample Grill Assistant")).to_be_visible()

    # 4. Check that the customer can see the message input.
    expect(page.get_by_label("Type your message")).to_be_visible()

    # 5. Check that the Send button is visible.
    expect(page.get_by_role("button", name="Send")).to_be_visible()


def test_customer_can_send_message(page: Page):

    # 1. Prepare a fake conversation history.
    messages = []

    assistant_reply = "This is a mocked test reply."

    # 2. Mock the conversation history endpoint.
    def mock_history(route):
        response_data = {
            "messages": messages,
            "handoff_active": False,
        }

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response_data),
        )

    # 3. Mock the endpoint that receives customer messages.
    def mock_chat(route):
        request_data = json.loads(route.request.post_data or "{}")
        customer_message = request_data["message"]

        messages.append(
            {
                "id": len(messages) + 1,
                "role": "user",
                "message": customer_message,
                "created_at": "2026-10-27 12:00:00",
            }
        )

        messages.append(
            {
                "id": len(messages) + 1,
                "role": "assistant",
                "message": assistant_reply,
                "created_at": "2026-10-27 12:00:01",
            }
        )

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "reply": assistant_reply,
                    "handoff_active": False,
                }
            ),
        )

    # 4. Register our mocked endpoints before opening the website.
    page.route("**/history", mock_history)
    page.route("**/chat", mock_chat)

    # 5. Open the restaurant chatbot.
    page.goto("http://127.0.0.1:5000")

    # 6. Find the message input and type a message.
    message_input = page.get_by_label("Type your message")
    message_input.fill("Hi there!")

    # 7. Click the Send button.
    page.get_by_role("button", name="Send").click()

    # 8. Verify that the customer's message appears.
    expect(page.locator(".message.user:not(.typing) .bubble")).to_contain_text(
        "Hi there!"
    )

    # 9. Verify that the chatbot's reply appears.
    expect(page.locator(".message.assistant:not(.typing) .bubble")).to_contain_text(
        assistant_reply
    )


def test_conversation_history_persists_after_reload(page: Page):

    # 1. Prepare an empty messages list and a predictable assistant reply.
    messages = []

    assistant_reply = "This is a mocked test reply"

    # 2. Define mock_history(route) to return the messages list as JSON.
    def mock_history(route):
        response_data = {
            "messages": messages,
            "handoff_active": False,
        }

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response_data),
        )

    # 3. Define mock_chat(route)
    def mock_chat(route):
        request_data = json.loads(route.request.post_data or "{}")
        customer_message = request_data["message"]

        messages.append(
            {
                "id": len(messages) + 1,
                "role": "user",
                "message": customer_message,
                "created_at": "2026-10-27 12:00:00",
            }
        )
        messages.append(
            {
                "id": len(messages) + 1,
                "role": "assistant",
                "message": assistant_reply,
                "created_at": "2026-10-27 12:00:01",
            }
        )

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "reply": assistant_reply,
                    "handoff_active": False,
                }
            ),
        )

    # 4. Register the mocked /history and /chat endpoints.
    page.route("**/history", mock_history)
    page.route("**/chat", mock_chat)

    # 5. Open the restaurant chatbot website.
    page.goto("http://127.0.0.1:5000")

    # 6. Find the message input and enter a customer message.
    message_input = page.get_by_label("Type your message")
    message_input.fill("Hi there!")

    # 7. Click the Send button.
    page.get_by_role("button", name="Send").click()

    # 8. Assert that the customer's message is visible.
    expect(page.locator(".message.user:not(.typing) .bubble")).to_contain_text(
        "Hi there!"
    )

    # 9. Assert that the assistant's reply is visible.
    expect(page.locator(".message.assistant:not(.typing) .bubble")).to_contain_text(
        assistant_reply
    )

    # 10. Reload the browser page.
    page.reload()

    # 11. Assert that the customer's original message is still visible.
    expect(page.locator(".message.user:not(.typing) .bubble")).to_contain_text(
        "Hi there!"
    )

    # 12. Assert that the assistant's original reply is still visible.
    expect(page.locator(".message.assistant:not(.typing) .bubble")).to_contain_text(
        assistant_reply
    )


def test_customer_can_add_multiple_items(page: Page):

    # 1. Prepare an empty messages list and a predictable assistant reply.
    messages = []
    assistant_reply = "Added 1 burger and 1 fries to your order"

    # 2. Define mock_history(route) to return the messages list as JSON.
    def mock_history(route):
        response_data = {
            "messages": messages,
            "handoff_active": False,
        }

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response_data),
        )

    # 3. Define mock_chat(route) to:
    def mock_chat(route):
        request_data = json.loads(route.request.post_data or "{}")
        customer_message = request_data["message"]

        messages.append(
            {
                "id": len(messages) + 1,
                "role": "user",
                "message": customer_message,
                "created_at": "2026-10-27 12:00:00",
            }
        )
        messages.append(
            {
                "id": len(messages) + 1,
                "role": "assistant",
                "message": assistant_reply,
                "created_at": "2026-10-27 12:00:01",
            }
        )

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "reply": assistant_reply,
                    "handoff_active": False,
                }
            ),
        )

    # 4. Register the mocked /history and /chat endpoints.
    page.route("**/history", mock_history)
    page.route("**/chat", mock_chat)

    # 5. Open the restaurant chatbot website.
    page.goto("http://127.0.0.1:5000")

    # 6. Find the message input and enter:
    message_input = page.get_by_label("Type your message")
    message_input.fill("Add a burger and fries")

    # 7. Click the Send button.
    page.get_by_role("button", name="Send").click()

    # 8. Assert that the customer's message is visible.
    expect(page.locator(".message.user:not(.typing) .bubble")).to_contain_text(
        "Add a burger and fries"
    )

    # 9. Assert that the assistant's reply mentions both the burger and fries.
    expect(page.locator(".message.assistant:not(.typing) .bubble")).to_contain_text(
        assistant_reply
    )


def test_customer_can_place_order(page: Page):

    # 1. Prepare an empty messages list.
    messages = []

    # 2. Define mock_history(route) to return the messages list as JSON.
    def mock_history(route):
        response_data = {
            "messages": messages,
            "handoff_active": False,
        }

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response_data),
        )

    # 3. Define mock_chat(route)
    def mock_chat(route):
        request_data = json.loads(route.request.post_data or "{}")
        customer_message = request_data["message"]

        if customer_message == "Add a burger":
            assistant_reply = "Added 1 burger to your order."

        elif customer_message == "Place my order":
            assistant_reply = "Your order request has been submitted and is awaiting staff review."

        else:
            assistant_reply = "Unexpected test message."

        messages.append(
            {
                "id": len(messages) + 1,
                "role": "user",
                "message": customer_message,
                "created_at": "2026-10-27 12:00:00",
            }
        )
        messages.append(
            {
                "id": len(messages) + 1,
                "role": "assistant",
                "message": assistant_reply,
                "created_at": "2026-10-27 12:00:01",
            }
        )

        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "reply": assistant_reply,
                    "handoff_active": False,
                }
            ),
        )

    # 4. Register the mocked /history and /chat endpoints.
    page.route("**/history", mock_history)
    page.route("**/chat", mock_chat)

    # 5. Open the restaurant chatbot website.
    page.goto("http://127.0.0.1:5000")

    # 6. Find the message input and enter "Add a burger".
    message_input = page.get_by_label("Type your message")
    message_input.fill("Add a burger")

    # 7. Click the Send button.
    page.get_by_role("button", name="Send").click()

    # 8. Assert that the assistant's reply confirms the burger was added.
    expect(page.locator(".message.assistant:not(.typing) .bubble")).to_contain_text(
        "Added 1 burger to your order"
    )

    # 9. Find the message input again and enter "Place my order".
    message_input = page.get_by_label("Type your message")
    message_input.fill("Place my order")

    # 10. Click the Send button again.
    page.get_by_role("button", name="Send").click()

    # 11. Assert that the customer's placement message appears in the conversation.
    expect(page.locator(".message.user:not(.typing) .bubble").last).to_contain_text(
        "Place my order"
    )

    # 12. Assert that the assistant's reply confirms the order was placed.
    expect(
        page.locator(".message.assistant:not(.typing) .bubble").last
    ).to_contain_text("Your order request has been submitted")


def test_pending_review_order_appears_on_staff_dashboard(page: Page):

    # 1. Prepare a fake order request awaiting staff review.
    orders_data = {
        "orders": [
            {
                "id": 101,
                "conversation_id": "test-conversation-123",
                "status": "pending_review",
                "created_at": "2026-10-27 12:00:00",
                "updated_at": "2026-10-27 12:00:00",
                "staff_acknowledged_at": None,
                "items": [
                    {
                        "item_name": "Classic Burger",
                        "quantity": 1,
                        "unit_price": 650,
                        "subtotal": 650,
                    }
                ],
                "total": 650,
            }
        ]
    }

    # 2. Mock the orders endpoint.
    def mock_orders(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(orders_data),
        )

    # 3. Mock the handoffs endpoint.
    def mock_handoffs(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"requests": []}),
        )

    # 4. Register the mocked API endpoints.
    page.route("**/api/staff/orders**", mock_orders)
    page.route("**/api/staff/handoffs**", mock_handoffs)

    # 5. Open the staff login page.
    page.goto("http://127.0.0.1:5000/staff/login")

    # 6. Enter the staff username.
    page.locator('input[name="username"]').fill("playwright-staff")

    # 7. Enter the password and submit the login form.
    password_input = page.locator('input[name="password"]')
    password_input.fill("TestPassword123!")
    password_input.press("Enter")

    # 8. Assert that the Staff Dashboard heading is visible.
    expect(
        page.get_by_role("heading", name="The Sample Grill — Staff Dashboard")
    ).to_be_visible()

    # 9. Locate the order rows.
    order_rows = page.locator("#orders-table tr")

    # 10. Assert that exactly one order row is displayed.
    expect(order_rows).to_have_count(1)

    # 11. Assert that the displayed order contains the expected details.
    order_row = order_rows.first

    expect(order_row).to_contain_text("101")
    expect(order_row).to_contain_text("Classic Burger")
    expect(order_row).to_contain_text("Awaiting staff review")



def test_order_status_filter_works(page: Page):

    # 1. Prepare fake pending-review order data.
    confirmed_orders = {
        "orders": [
            {
                "id": 101,
                "conversation_id": "conversation-confirmed",
                "status": "pending_review",
                "created_at": "2026-10-27 12:00:00",
                "updated_at": "2026-10-27 12:00:00",
                "staff_acknowledged_at": None,
                "items": [
                    {
                        "item_name": "Classic Burger",
                        "quantity": 1,
                        "unit_price": 650,
                        "subtotal": 650,
                    }
                ],
                "total": 650,
            }
        ]
    }

    # 2. Prepare fake cancelled-order data.
    cancelled_orders = {
        "orders": [
            {
                "id": 202,
                "conversation_id": "conversation-cancelled",
                "status": "cancelled",
                "created_at": "2026-10-27 13:00:00",
                "updated_at": "2026-10-27 13:30:00",
                "staff_acknowledged_at": None,
                "items": [
                    {
                        "item_name": "Chicken Bites (5)",
                        "quantity": 1,
                        "unit_price": 450,
                        "subtotal": 450,
                    }
                ],
                "total": 450,
            }
        ]
    }

    # 3. Mock the orders API.
    def mock_orders(route):

        # Check which filter the browser requested.
        request_url = route.request.url

        # Return cancelled orders when that filter is selected.
        if "status=cancelled" in request_url:
            response_data = cancelled_orders

        # By default, the dashboard shows requests awaiting staff review.
        else:
            response_data = confirmed_orders

        # Send the appropriate fake response to the browser.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response_data),
        )

    # 4. Mock the handoffs API.
    def mock_handoffs(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"requests": []}),
        )

    # 5. Register the mocked API endpoints.
    page.route("**/api/staff/orders**", mock_orders)
    page.route("**/api/staff/handoffs**", mock_handoffs)

    # 6. Open the staff login page.
    page.goto("http://127.0.0.1:5000/staff/login")

    # 7. Log in as staff.
    page.locator('input[name="username"]').fill("playwright-staff")

    password_input = page.locator('input[name="password"]')
    password_input.fill("TestPassword123!")
    password_input.press("Enter")

    # 8. Confirm that the dashboard has loaded.
    expect(
        page.get_by_role("heading", name="The Sample Grill — Staff Dashboard")
    ).to_be_visible()

    # 9. Locate the order rows.
    order_rows = page.locator("#orders-table tr")

    # 10. Confirm that the pending-review request appears.
    expect(order_rows).to_have_count(1)
    expect(order_rows.first).to_contain_text("101")
    expect(order_rows.first).to_contain_text("Classic Burger")
    expect(order_rows.first).to_contain_text("Awaiting staff review")

    # 11. Change the filter to cancelled orders.
    page.locator("#order-status-filter").select_option("cancelled")

    # 12. Verify that the cancelled order now appears.
    expect(order_rows).to_have_count(1)
    expect(order_rows.first).to_contain_text("202")
    expect(order_rows.first).to_contain_text("Chicken Bites (5)")
    expect(order_rows.first).to_contain_text("cancelled")



def test_staff_can_accept_order(page: Page):
    """Accepting a pending request changes it to confirmed and removes it from review."""
    orders_data = {
        "orders": [
            {
                "id": 101,
                "conversation_id": "conversation-pending-review",
                "status": "pending_review",
                "created_at": "2026-10-27 12:00:00",
                "updated_at": "2026-10-27 12:00:00",
                "staff_acknowledged_at": None,
                "items": [
                    {
                        "item_name": "Classic Burger",
                        "quantity": 1,
                        "unit_price": 650,
                        "subtotal": 650,
                    }
                ],
                "total": 650,
            }
        ]
    }
    decision_request = {"received": False, "decision": None}

    def mock_orders(route):
        method = route.request.method
        request_url = route.request.url

        if method == "GET":
            requested_status = "pending_review"
            if "status=" in request_url:
                requested_status = request_url.split("status=", 1)[1].split("&", 1)[0]
            all_orders = orders_data["orders"]
            filtered = all_orders if requested_status == "" else [
                order for order in all_orders if order["status"] == requested_status
            ]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"orders": filtered}),
            )
            return

        request_data = json.loads(route.request.post_data or "{}")
        decision_request["received"] = True
        decision_request["decision"] = request_data.get("decision")
        orders_data["orders"][0]["status"] = (
            "confirmed" if request_data.get("decision") == "accept" else "rejected"
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "success": True,
                "order_id": 101,
                "status": orders_data["orders"][0]["status"],
                "decision": request_data.get("decision"),
            }),
        )

    def mock_handoffs(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"requests": []}),
        )

    page.route("**/api/staff/orders**", mock_orders)
    page.route("**/api/staff/handoffs**", mock_handoffs)
    page.goto("http://127.0.0.1:5000/staff/login")
    page.locator('input[name="username"]').fill("playwright-staff")
    password_input = page.locator('input[name="password"]')
    password_input.fill("TestPassword123!")
    password_input.press("Enter")

    expect(
        page.get_by_role("heading", name="The Sample Grill — Staff Dashboard")
    ).to_be_visible()
    order_row = page.locator("#orders-table tr").first
    expect(order_row).to_contain_text("101")
    expect(order_row).to_contain_text("Awaiting staff review")

    page.get_by_role("button", name="Accept order").click()

    expect(page.get_by_text("Order #101 has been accepted by the restaurant.")).to_be_visible()
    assert decision_request["received"] is True
    assert decision_request["decision"] == "accept"
    assert orders_data["orders"][0]["status"] == "confirmed"
    expect(page.locator("#orders-table tr")).to_have_count(0)




def test_customer_request_for_human_activates_handoff_state(page: Page):
    # 1. Prepare lists to store messages and requests.
    received_messages = []
    messages = []

    # 2. Track whether a handoff is active.
    handoff_state = {"active": False}

    # 3. Mock the conversation history endpoint.
    def mock_history(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "messages": messages,
                "handoff_active": handoff_state["active"],
            }),
        )

    # 4. Mock the chat endpoint.
    def mock_chat(route):

        # Read the customer's message from the request.
        request_data = json.loads(route.request.post_data or "{}")
        customer_message = request_data["message"]

        # Record the message received by the mock.
        received_messages.append(customer_message)

        # Save the customer's message to simulated history.
        messages.append({
            "id": len(messages) + 1,
            "role": "user",
            "message": customer_message,
            "created_at": "2026-10-27 14:00:00",
        })

        # Prepare the chatbot's response.
        reply = "I'll connect you with a member of our staff."

        # Save the assistant's response to simulated history.
        messages.append({
            "id": len(messages) + 1,
            "role": "assistant",
            "message": reply,
            "created_at": "2026-10-27 14:00:01",
        })

        # Mark the handoff as active.
        handoff_state["active"] = True

        # Return the mocked response to the browser.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "reply": reply,
                "handoff_active": True,
            }),
        )

    # 5. Register the mocked API endpoints.
    page.route("**/history", mock_history)
    page.route("**/chat", mock_chat)

    # 6. Open the customer chat page.
    page.goto("http://127.0.0.1:5000")

    # 7. Enter a request for human assistance.
    customer_message = "I would like to speak to a human, please."

    message_input = page.get_by_label("Type your message")
    message_input.fill(customer_message)

    # 8. Click Send.
    page.get_by_role("button", name="Send").click()

    # 9. Verify that the customer's message appears in the chat.
    expect(
        page.locator(".message.user:not(.typing) .bubble").last
    ).to_contain_text(customer_message)

    # 10. Verify that the assistant's response appears.
    expect(
        page.locator(".message.assistant:not(.typing) .bubble").last
    ).to_contain_text(
        "I'll connect you with a member of our staff."
    )

    # 11. Verify that the mock received the customer's request.
    assert customer_message in received_messages

    # 12. Verify that exactly one chat request was received.
    assert len(received_messages) == 1

    # 13. Verify that both messages are stored in simulated history.
    assert len(messages) == 2

    # 14. Verify that the handoff is marked as active.
    assert handoff_state["active"] is True




def test_staff_reply_persists_when_reopening_conversation(page: Page):

    # 1. Prepare a fake customer handoff request.
    handoff_data = {
        "requests": [
            {
                "id": 301,
                "reason": "Customer requested human assistance",
                "status": "pending",
                "conversation_id": "conversation-support-301",
                "created_at": "2026-10-27 14:00:00",
            }
        ]
    }

    # 2. Prepare the initial conversation history.
    conversation_history = [
        {
            "id": 1,
            "role": "user",
            "message": "Can someone help with my order?",
            "created_at": "2026-10-27 14:00:00",
        }
    ]

    # 3. Record replies sent by the staff member.
    sent_replies = []

    # 4. Mock the staff handoff API.
    def mock_handoff_api(route):

        # Get the request URL without query parameters.
        request_url = route.request.url.split("?")[0]

        # Get the HTTP method.
        method = route.request.method

        # 5. Return the conversation when the staff member opens it.
        if (
            method == "GET"
            and request_url.endswith("/conversation")
        ):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "request_id": 301,
                    "conversation_id": "conversation-support-301",
                    "status": "pending",
                    "staff_notes": "",
                    "messages": conversation_history,
                }),
            )

        # 6. Return new messages when the dashboard polls the API.
        elif (
            method == "GET"
            and request_url.endswith("/messages/new")
        ):

            # Extract the last message ID requested by the browser.
            full_url = route.request.url

            if "after_id=" in full_url:
                after_id = int(
                    full_url.split("after_id=")[1].split("&")[0]
                )
            else:
                after_id = 0

            # Return messages newer than the requested message ID.
            new_messages = [
                message
                for message in conversation_history
                if message["id"] > after_id
            ]

            # Determine the latest message ID.
            last_message_id = (
                new_messages[-1]["id"]
                if new_messages
                else after_id
            )

            # Send the new messages to the browser.
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "request_id": 301,
                    "messages": new_messages,
                    "last_message_id": last_message_id,
                }),
            )

        # 7. Handle a staff member sending a reply.
        elif (
            method == "POST"
            and request_url.endswith("/reply")
        ):

            # Read the reply from the request body.
            request_data = json.loads(
                route.request.post_data or "{}"
            )

            staff_message = request_data["message"]

            # Record the submitted reply.
            sent_replies.append(staff_message)

            # Create a new message in the conversation history.
            new_message = {
                "id": len(conversation_history) + 1,
                "role": "staff",
                "message": staff_message,
                "created_at": "2026-10-27 14:05:00",
            }

            # Save the reply to our simulated history.
            conversation_history.append(new_message)

            # Return a successful response to the browser.
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "request_id": 301,
                    "conversation_id": "conversation-support-301",
                    "id": new_message["id"],
                    "role": "staff",
                    "message": staff_message,
                }),
            )

        # 8. Return the handoff list for other requests.
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(handoff_data),
            )

    # 9. Mock the orders API used by the dashboard.
    def mock_orders(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "orders": [],
            }),
        )

    # 10. Register the mocked API endpoints.
    page.route("**/api/staff/handoffs**", mock_handoff_api)
    page.route("**/api/staff/orders**", mock_orders)

    # 11. Open the staff login page.
    page.goto("http://127.0.0.1:5000/staff/login")

    # 12. Log in as staff.
    page.locator('input[name="username"]').fill("playwright-staff")

    password_input = page.locator('input[name="password"]')
    password_input.fill("TestPassword123!")
    password_input.press("Enter")

    # 13. Confirm that the dashboard has loaded.
    expect(
        page.get_by_role("heading", name="The Sample Grill — Staff Dashboard")
    ).to_be_visible()

    # 14. Verify that the handoff request appears.
    handoff_rows = page.locator("#handoff-requests tr")

    expect(handoff_rows).to_have_count(1)

    expect(
        handoff_rows.first
    ).to_contain_text("Customer requested human assistance")

    # 15. Open the customer's conversation.
    page.get_by_role(
        "button",
        name="View conversation",
    ).click()

    # 16. Confirm that the conversation dialog is visible.
    conversation_dialog = page.locator("#conversation-dialog")

    expect(conversation_dialog).to_be_visible()

    # 17. Locate the conversation's message container.
    conversation_messages = page.locator("#conversation-messages")

    # 18. Verify that the original customer message appears.
    expect(
        conversation_messages
    ).to_contain_text("Can someone help with my order?")

    # 19. Prepare a reply from the staff member.
    staff_reply = "Of course! I'll check your order for you."

    page.locator("#staff-reply").fill(staff_reply)

    # 20. Send the reply.
    page.get_by_role(
        "button",
        name="Send reply",
    ).click()

    # 21. Confirm that the dashboard reports a successful submission.
    expect(
        page.locator("#staff-reply-message")
    ).to_contain_text("Reply sent. Updating conversation...")

    # 22. Verify that polling retrieves and displays the reply.
    expect(
        conversation_messages
    ).to_contain_text(staff_reply)

    # 23. Verify that exactly one reply was submitted.
    assert sent_replies == [staff_reply]

    # 24. Verify that the reply was added to simulated history.
    assert any(
        message["role"] == "staff"
        and message["message"] == staff_reply
        for message in conversation_history
    )

    # 25. Close the conversation.
    page.locator("#close-conversation").click()

    # 26. Reopen the same conversation.
    page.get_by_role(
        "button",
        name="View conversation",
    ).click()

    expect(conversation_dialog).to_be_visible()

    # 27. Verify that the original customer message remains visible.
    expect(
        conversation_messages
    ).to_contain_text("Can someone help with my order?")

    # 28. Verify that the staff reply remains visible after reopening.
    expect(
        conversation_messages
    ).to_contain_text(staff_reply)

    # 29. Verify that the reply was not submitted a second time.
    assert sent_replies == [staff_reply]

    # 30. Confirm that the simulated history contains both messages.
    assert len(conversation_history) == 2