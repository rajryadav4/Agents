import pytest

from dealhawk.sources import FetchError, Http
from dealhawk.sources.harvest import harvest_products
from dealhawk.sources.pagewatch import extract_product
from dealhawk.sources.retail import RETAILERS, RetailSource

from .conftest import fixture_text


def by_name(products):
    return {p["name"]: p for p in products}


def test_walmart_next_data_product():
    ps = harvest_products(fixture_text("walmart_product.html"), "https://www.walmart.com/ip/x")
    tv = by_name(ps)['LG 65" Class OLED evo C4 4K Smart TV']
    assert (tv["price"], tv["was"], tv["id"], tv["in_stock"]) == (296.99, 1796.99, "5129348702", True)
    assert tv["url"] == "https://www.walmart.com/ip/LG-65-OLED/5129348702"
    assert "Customer reviews" not in by_name(ps)                       # no price → not a product
    assert extract_product(fixture_text("walmart_product.html"))["price"] == 296.99   # verifier can read it


def test_walmart_deals_listing():
    ps = by_name(harvest_products(fixture_text("walmart_deals.html"), "https://www.walmart.com/shop/deals"))
    assert set(ps) == {"Apple AirPods Pro 2", "Ninja AF101 Air Fryer"}
    assert ps["Apple AirPods Pro 2"]["was"] == 249.0


def test_target_embedded_state():
    ps = by_name(harvest_products(fixture_text("target_clearance.html"), "https://www.target.com/c/clearance"))
    sony = ps["Sony WH-1000XM5 Wireless Headphones"]
    assert (sony["price"], sony["was"], sony["id"]) == (178.0, 399.99, "89123456")


def test_amazon_html_fallback():
    (p,) = harvest_products(fixture_text("amazon_product.html"))
    assert (p["name"], p["price"], p["was"], p["id"]) == ("Apple 2024 MacBook Pro 14-inch M4", 1299.0, 1599.0,
                                                          "B0DLHBK7XX")


def test_jsonld_itemlist_and_aggregate_offer():
    ps = by_name(harvest_products(fixture_text("jsonld_itemlist.html"), "https://www.homedepot.com/b/deals"))
    assert ps["DeWalt 20V Drill Kit"]["price"] == 79.0 and ps["DeWalt 20V Drill Kit"]["url"].endswith("/p/dw1")
    assert ps["Milwaukee M18 Impact"]["price"] == 99.0


def test_retail_source_end_to_end(fake_http):
    http, f = fake_http({"walmart.com/shop/deals": (200, fixture_text("walmart_deals.html")),
                         "walmart.com/search": (200, fixture_text("walmart_product.html"))})
    src = RetailSource("wm", retailer="walmart", pages=["deals"], search=["oled tv"], min_gap_s=0, http=http)
    offers = src.fetch()
    assert {o.retailer for o in offers} == {"Walmart"}
    assert any(o.title.startswith("LG 65") and o.reference_price == 1796.99 for o in offers)
    assert "search?q=oled+tv" in f.calls[1][0]


def test_retail_source_blocked_raises_for_backoff(fake_http):
    http, _ = fake_http({"walmart.com": (403, "blocked")})
    with pytest.raises(FetchError) as e:
        RetailSource("wm", retailer="walmart", pages=["deals"], min_gap_s=0, http=http).fetch()
    assert e.value.status == 403
    http, _ = fake_http({"walmart.com": (200, "<html>Please verify you are a human (captcha)</html>")})
    with pytest.raises(FetchError):
        RetailSource("wm", retailer="walmart", pages=["deals"], min_gap_s=0, http=http).fetch()


def test_retail_source_validation_and_presets():
    with pytest.raises(ValueError, match="unknown retailer"):
        RetailSource("x", retailer="nope")
    with pytest.raises(ValueError, match="no page"):
        RetailSource("x", retailer="walmart", pages=["nope"])
    with pytest.raises(ValueError, match="nothing to scan"):
        RetailSource("x", retailer="other")
    assert RetailSource("t", retailer="target").render is not None          # JS-heavy → browser by default
    assert RetailSource("t", retailer="target", browser=False).render is None
    assert len(RETAILERS) - 1 >= 18                                          # 18 named retailers + "other"
    for key, spec in RETAILERS.items():
        assert all(u.startswith("/") for u in spec["pages"].values()), key


def test_live_price_by_id(fake_http):
    http, _ = fake_http({"walmart.com/ip": (200, fixture_text("walmart_product.html"))})
    src = RetailSource("wm", retailer="walmart", pages=["deals"], min_gap_s=0, http=http)
    from dealhawk.models import Offer
    o = Offer("wm", "Walmart", "LG", "https://www.walmart.com/ip/LG-65-OLED/5129348702", 296.99,
              product_id="5129348702")
    assert src.live_price(o) == 296.99


def test_retail_source_all_pages_unreachable_is_a_failure():
    def down(url, headers, body):
        raise FetchError("network unreachable")
    src = RetailSource("wm", retailer="walmart", pages=["deals", "clearance"], min_gap_s=0,
                       http=Http(fetcher=down, retries=0, sleep=lambda s: None))
    with pytest.raises(FetchError, match="every page failed"):
        src.fetch()

