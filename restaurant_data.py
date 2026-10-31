"""Fictional UK restaurant data used only for the Business AI demonstration.

Menu prices are stored as integer pence to avoid floating-point currency maths.
Replace this entire configuration with restaurant-approved information before a live pilot.
"""

restaurant = {
    "name": "The Sample Grill",
    "demo_mode": True,
    "currency_code": "GBP",
    "currency_symbol": "£",
    "minor_units_per_major": 100,
    "opening_hours": "12 noon to 10 pm",
    "address": "Morriston, Swansea",
    "vegetarian_options": (
        "The menu includes a veggie burger and fries. Ingredients and preparation "
        "have not been verified, so please check with the restaurant team about "
        "ingredients and dietary requirements."
    ),
    # No telephone number is configured for the fictional restaurant.
    "phone": "",
    "delivery": "Delivery may be available within 3 miles, subject to staff confirmation.",
    "payment": "Please contact the restaurant team to confirm accepted payment methods.",
    # Integer pence: £6.50 is stored as 650, £2.50 as 250, etc.
    "menu": {
        "classic burger": 650,
        "spicy chicken burger": 725,
        "double beef burger": 850,
        "veggie burger": 675,
        "fries": 250,
        "cheesy fries": 350,
        "chicken bites (5)": 450,
        "cola": 150,
        "bottled water": 120,
    },
}
