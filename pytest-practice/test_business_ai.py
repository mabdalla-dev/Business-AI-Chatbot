from app import calculate_order_total
from restaurant_data import restaurant


def test_calculate_order_total():
    order = {
        "classic burger": 2,
        "fries": 1,
        "cola": 2,
    }

    result = calculate_order_total(order)
    expected = (
        restaurant["menu"]["classic burger"] * 2
        + restaurant["menu"]["fries"]
        + restaurant["menu"]["cola"] * 2
    )

    # Prices are stored in integer pence in the UK demo configuration.
    assert result == expected


def test_calculate_empty_order():
    order = {}
    result = calculate_order_total(order)

    assert result == 0
