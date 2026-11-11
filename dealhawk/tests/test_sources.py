import json
from urllib.parse import unquote

import pytest

from dealhawk.sources import FetchError, Http
from dealhawk.sources.bestbuy import BestBuySource
from dealhawk.sources.keepa import KeepaSource
from dealhawk.sources.pagewatch import PageWatchSource, extract_product
from dealhawk.sources.reddit import RedditSource
from dealhawk.sources.rss import PRESETS, RssSource, parse_feed
from dealhawk.sources.shopify import ShopifySource

from .conftest import fixture_text


def test_slickdeals_rss(fake_http):
    http, _ = fake_http({"slickdeals.net": (200, fixture_text("slickdeals.rss"))})
    offers = RssSource("sd", preset="slickdeals_frontpage", http=http).fetch()
    assert len(offers) == 4
    mac, fryer, tv, coupon = offers
    assert mac.price == 1299 and mac.retailer == "Amazon"
    assert mac.url.startswith("https://www.amazon.com/dp/")       # straight to the store, not the forum
    assert mac.community_score == 212 and mac.posted_at
    assert (fryer.price, fryer.reference_price, fryer.retailer) == (49.99, 129.99, "Walmart")
    assert tv.retailer == "Target" and tv.price == 296.99
    assert coupon.price is None


def test_rss_search_preset_quotes_query():
    s = RssSource("q", preset="slickdeals_search", query="macbook pro m4")
    assert "q=macbook+pro+m4" in s.url
    with pytest.raises(ValueError):
        RssSource("bad", preset="nope")


def test_atom_feed_reddit_rss():
    items = parse_feed(fixture_text("reddit_deals.atom"))
    assert items[0]["link"].startswith("https://www.reddit.com/")
    o = RssSource("r", url="https://x/.rss").item_to_offer(items[0])
    assert (o.retailer, o.price, o.reference_price) == ("Costco", 399.99, 749.99)
    assert o.url.startswith("https://www.costco.com/")


def test_reddit_json(fake_http):
    http, _ = fake_http({"buildapcsales/new.json": (200, fixture_text("reddit_buildapcsales.json"))})
    offers = RedditSource("r", subreddits=["buildapcsales"], http=http).fetch()
    assert len(offers) == 3                       # sticky dropped, expired kept (for crowd learning)
    gpu, monitor, ssd = offers
    assert (gpu.price, gpu.reference_price, gpu.retailer, gpu.category) == (429, 599, "Newegg", "gpu")
    assert gpu.community_score == 140
    assert monitor.in_stock is False and "Expired" in monitor.tags
    assert ssd.retailer == "Amazon" and "reddit.com" in ssd.url   # self-post: permalink


def test_reddit_falls_back_to_rss_when_blocked(fake_http):
    http, f = fake_http({".json": (403, "blocked"), "/new/.rss": (200, fixture_text("reddit_deals.atom"))})
    offers = RedditSource("r", subreddits=["deals"], http=http).fetch()
    assert len(offers) == 1 and offers[0].retailer == "Costco"
    assert any(".rss" in c[0] for c in f.calls)


def test_shopify(fake_http):
    http, f = fake_http({"products.json": (200, fixture_text("shopify_products.json"))})
    offers = ShopifySource("s", stores=["www.allbirds.com"], http=http).fetch()
    assert len(offers) == 1                        # out-of-stock variant + $0 gift card skipped
    o = offers[0]
    assert o.retailer == "allbirds.com" and o.price == 98.0
    assert o.product_id == "111-1001" and o.title == "Wool Runner — 8"
    assert "clearance" in o.tags and o.url.endswith("?variant=1001")
    assert f.calls[0][0].startswith("https://www.allbirds.com/products.json")


def test_bestbuy(fake_http):
    http, f = fake_http({"api.bestbuy.com": (200, fixture_text("bestbuy_products.json"))})
    src = BestBuySource("bb", api_key="K", searches=["macbook air"], http=http)
    offers = src.fetch()
    assert len(offers) == 1
    o = offers[0]
    assert (o.price, o.reference_price, o.product_id, o.category) == (749.0, 1099.0, "6565837", "macbooks")
    url = unquote(f.calls[0][0])
    assert "search=macbook&search=air&onSale=true&percentSavings>=30" in url and "apiKey=K" in url
    with pytest.raises(ValueError):
        BestBuySource("bb", api_key="")


def test_keepa(fake_http):
    http, f = fake_http({"api.keepa.com/deal": (200, fixture_text("keepa_deals.json"))})
    offers = KeepaSource("k", api_key="K", http=http).fetch()
    assert len(offers) == 1
    o = offers[0]
    assert (o.price, o.typical_price, o.low_price) == (89.99, 219.99, 189.99)
    assert o.url == "https://www.amazon.com/dp/B0D1XD1ZV3"
    assert o.image == "https://m.media-amazon.com/images/I/ABC.jpg"
    sel = json.loads(unquote(f.calls[0][0].split("selection=")[1]))
    assert sel["priceTypes"] == [1] and sel["deltaPercentRange"] == [40, 100]


def test_jsonld_extraction():
    p = extract_product(fixture_text("product_page.html"))
    assert p == {"name": "Sony Alpha a7 IV Mirrorless Camera", "price": 1698.0, "sku": "SOA7IV",
                 "in_stock": True, "image": "https://img/a7iv.jpg"}
    meta = extract_product(fixture_text("product_page_meta.html"))
    assert meta["price"] == 19.99 and meta["name"] == "Cheap Widget"
    assert extract_product("<html>nothing</html>") is None


def test_pagewatch(fake_http):
    http, _ = fake_http({"bhphotovideo": (200, fixture_text("product_page.html")), "blocked": (403, "")})
    src = PageWatchSource("w", urls=["https://www.bhphotovideo.com/c/product/1.html", "https://blocked.example/x"],
                          http=http)
    offers = src.fetch()                      # blocked page is skipped, not fatal
    assert len(offers) == 1 and offers[0].retailer == "B&H Photo" and offers[0].product_id == "SOA7IV"


def test_http_retries_then_raises():
    calls = []

    def fetch(url, h, b):
        calls.append(url)
        return 503, b""
    http = Http(fetcher=fetch, retries=2, sleep=lambda s: None)
    with pytest.raises(FetchError) as e:
        http.get_text("https://x")
    assert e.value.status == 503 and len(calls) == 3


def test_http_accepts_2xx():
    http = Http(fetcher=lambda u, h, b: (204, b""), sleep=lambda s: None)
    assert http.request("https://x") == b""


def test_presets_are_urls():
    assert all(u.startswith("https://") for u in PRESETS.values())
