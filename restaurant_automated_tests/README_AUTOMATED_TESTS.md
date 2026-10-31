# Business AI — automated regression checks

The project includes a pytest suite for order/database workflows and a Playwright suite for browser interactions. Preserve these tests; they are the existing regression checks, not a reason to rebuild the test suite.

## Order workflow tests

From the project root, activate the existing virtual environment and run:

```bash
python -m pytest -q restaurant_automated_tests/tests/test_order_workflow.py
```

The order-workflow tests use a temporary SQLite database and mock intent detection. They do not call the live OpenAI API.

## Playwright customer tests

The browser tests are in `restaurant_automated_tests/tests/test_playwright_customer.py`. They require Playwright and its pytest integration, plus a browser installation. Start the Flask app locally in one terminal, then run in another:

```bash
python -m pytest -q restaurant_automated_tests/tests/test_playwright_customer.py
```

Several browser tests deliberately mock API responses and in-memory browser state. Passing them does not by itself verify that every workflow persists to the actual SQLite database.

## UK demo pricing

The demo menu now stores prices as integer pence (for example, `650` means £6.50) to avoid floating-point currency arithmetic. The customer UI and staff dashboard format them as GBP. Do not use old records from a database created with the previous EGP sample configuration without first backing it up and creating a separate clean demo database.
