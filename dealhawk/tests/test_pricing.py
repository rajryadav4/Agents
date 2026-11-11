import pytest

from dealhawk.pricing import parse_percent_off, parse_prices
from dealhawk.retailers import detect_retailer, retailer_from_title, retailer_from_url


@pytest.mark.parametrize("title,expected", [
    ('Apple MacBook Air 13" M3 16GB/256GB $749 at Amazon', (749.0, None)),
    ("[GPU] MSI RTX 4070 Super - $499 ($599 - $100)", (499.0, 599.0)),
    ("Sony WH-1000XM5 $248 (Reg. $399.99) + Free Shipping", (248.0, 399.99)),
    ("Instant Pot Duo - Save $40 - now $59.99, was $99.99", (59.99, 99.99)),
    ("Get $10 off $50+ orders", (None, None)),
    ("Spend $50, get $10 off sitewide at Kohl's", (None, None)),
    ("Free Game: Hades on Epic", (0.0, None)),
    ("Laptop $1,299.99 + free shipping on orders over $35", (1299.99, None)),
    ("Verizon phone $5/mo", (None, None)),
    ("TV $499 list $399", (499.0, None)),   # "reference" lower than price is discarded
])
def test_parse_prices(title, expected):
    assert parse_prices(title) == expected


def test_percent_off():
    assert parse_percent_off("Jackets 70% off at REI") == 0.70
    assert parse_percent_off("no discount") is None


def test_retailer_detection():
    assert retailer_from_url("https://www.amazon.com/dp/B0") == "Amazon"
    assert retailer_from_url("https://amzn.to/abc") == "Amazon"
    assert retailer_from_url("https://store.steampowered.com/app/1") == "Steam"
    assert retailer_from_url("https://www.nordstromrack.com/x") == "Nordstrom Rack"
    assert retailer_from_url("https://example.com") is None
    assert retailer_from_title("Ninja air fryer $49 at Walmart") == "Walmart"
    assert retailer_from_title("[SSD] Samsung 990 Pro (Amazon)") == "Amazon"
    assert retailer_from_title("Best Buy: OLED TV $999") == "Best Buy"
    assert detect_retailer("https://forum.example.com/t/1", "Thing $5 @ Target") == "Target"
    assert detect_retailer(None, "mystery") == "Unknown"
