# Business AI — UK demonstration notes

This project copy is configured with a fictional UK takeaway called **The Sample Grill**.
It is demonstration data only, not a real business and not a customer.

## Pricing convention

`restaurant_data.py` stores menu prices as integer pence to avoid floating-point currency calculations:
- `650` means £6.50
- `250` means £2.50
- `120` means £1.20

The customer chat and staff dashboard format these amounts as GBP.

## Before any real restaurant pilot

Replace every sample field with information provided and approved by the restaurant, including menu, prices, opening hours, address, telephone, delivery, payment and allergen/dietary information. The current demo does not accept online payments and must not be used to take real orders.

A pre-existing `restaurant.db` may contain records created with the previous sample configuration. Back it up first and use a fresh, separate demo database for the UK demo rather than mixing old records with the new pence-based prices.

The model default is configured as `gpt-6-luna` using the `OPENAI_MODEL` environment-variable override. This must still be verified with the user's own API credentials before demonstrating live AI responses.
