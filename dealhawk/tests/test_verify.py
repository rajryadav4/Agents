from dealhawk.brain import LearningConfig
from dealhawk.config import AlertConfig, Config, QuietHours
from dealhawk.engine import Engine
from dealhawk.matching import Watch
from dealhawk.models import Offer
from dealhawk.sources.base import Http
from dealhawk.sources.bestbuy import BestBuySource
from dealhawk.sources.shopify import ShopifySource
from dealhawk.store import Store
from dealhawk.verify import PriceVerifier, VerifyConfig

from .conftest import FakeFetcher
from .test_engine import Sink

PAGE = '<script type="application/ld+json">{"@type":"Product","name":"TV","offers":{"price":"%s",' \
       '"availability":"https://schema.org/InStock"}}</script>'


def verifier(routes, sources=None, **cfg):
    f = FakeFetcher(routes)
    return PriceVerifier(VerifyConfig(**cfg), sources, http=Http(fetcher=f, retries=0, sleep=lambda s: None)), f


def tv(price, url="https://www.target.com/p/tv/-/A-1", title='PRICE MISTAKE LG 65" OLED'):
    return Offer("sd", "Target", title, url, price, product_id="A-1")


def test_statuses():
    v, _ = verifier({"target.com": (200, PAGE % "296.99")})
    assert v.verify(tv(296.99)).status == "confirmed"
    assert v.verify(tv(350.00)).status == "lower"
    r = v.verify(tv(199.00))
    assert r.status == "corrected" and r.live_price == 296.99
    blocked, _ = verifier({"target.com": (403, "captcha")})
    r = blocked.verify(tv(296.99))
    assert r.status == "unverifiable" and "403" in r.detail
    nomarkup, _ = verifier({"target.com": (200, "<html>js app</html>")})
    assert "machine-readable" in nomarkup.verify(tv(296.99)).detail


def test_budget():
    v, f = verifier({"target.com": (200, PAGE % "1")}, max_per_hour=2)
    [v.verify(tv(1)) for _ in range(3)]
    assert len(f.calls) == 2


def test_wants():
    v, _ = verifier({}, tiers=["steal"])
    assert v.wants("great", True) and v.wants("steal", False) and not v.wants("great", False)
    assert not verifier({}, enabled=False)[0].wants("steal", True)


def test_bestbuy_api_verification_preferred():
    f = FakeFetcher({"products/6565837.json": (200, '{"salePrice": 749.0, "onlineAvailability": true}')})
    bb = BestBuySource("bb", api_key="K", http=Http(fetcher=f, sleep=lambda s: None))
    v, page_fetch = verifier({}, sources={"bb": bb})
    r = v.verify(Offer("bb", "Best Buy", "MacBook Air", "https://bestbuy.com/x", 749.0, product_id="6565837"))
    assert (r.status, r.method) == ("confirmed", "api") and page_fetch.calls == []


def test_shopify_live_price():
    f = FakeFetcher({"/products/wool-runner.json": (200, '{"product":{"variants":[{"id":1001,"price":"20.00",'
                                                         '"available":true}]}}')})
    s = ShopifySource("s", stores=["x.com"], http=Http(fetcher=f, sleep=lambda s: None))
    assert s.live_price(Offer("s", "x", "t", "https://x.com/products/wool-runner?variant=1001", 20.0)) == 20.0


def engine_with(clock, routes, quiet=False):
    q = QuietHours(start="00:00", end="23:59", timezone="UTC") if quiet else None
    cfg = Config(database=":memory:", watches=[Watch("all")], alerts=AlertConfig(min_score=70, quiet_hours=q),
                 learning=LearningConfig(explore_per_day=0))
    store, sink = Store(":memory:"), Sink()
    v, _ = verifier(routes)
    eng = Engine(cfg, store, notifiers=[sink], verifier=v, clock=clock)
    for _ in range(6):
        eng.process([tv(1999.0, title='LG 65" C4 OLED TV')])
        clock.advance(7 * 3600)
    return eng, sink


def test_confirmed_glitch_is_steal_and_breaks_quiet_hours(clock):
    eng, sink = engine_with(clock, {"target.com": (200, PAGE % "296.99")}, quiet=True)
    eng.process([tv(296.99)])
    d = sink.sent[0]
    assert d.headline.startswith("⚡✅ VERIFIED PRICE GLITCH") and d.score.tier == "steal"
    assert d.score.reasons[0].startswith("✅ price verified live on Target")


def test_corrected_glitch_is_not_sent(clock):
    eng, sink = engine_with(clock, {"target.com": (200, PAGE % "1999.00")})
    r = eng.process([tv(296.99)])
    assert sink.sent == [] and r.suppressed[0][1].startswith("price already corrected")


def test_unverifiable_glitch_still_sent_with_warning(clock):
    eng, sink = engine_with(clock, {"target.com": (403, "")})
    eng.process([tv(296.99)])
    assert sink.sent[0].headline.startswith("⚡ POSSIBLE PRICE GLITCH")
    assert "couldn't verify" in sink.sent[0].score.reasons[0]


def community_post(price, url="https://www.target.com/p/tv/-/A-1"):
    return Offer("reddit", "Target", 'LG 65" C4 OLED TV', url, price, product_id="A-1", trust="community")


def community_engine(clock, routes):
    cfg = Config(database=":memory:", watches=[Watch("all")], alerts=AlertConfig(min_score=40),
                 learning=LearningConfig(explore_per_day=0, crowd_labels=False))
    store, sink = Store(":memory:"), Sink()
    v, f = verifier(routes)
    eng = Engine(cfg, store, notifiers=[sink], verifier=v, clock=clock)
    for _ in range(6):                                   # store's own price history (first-party)
        eng.process([tv(1999.0, title='LG 65" C4 OLED TV')])
        clock.advance(7 * 3600)
    return eng, store, sink, f


def test_community_post_confirmed_on_store_page_is_shown(clock):
    eng, store, sink, _ = community_engine(clock, {"target.com": (200, PAGE % "1199.00")})
    eng.process([community_post(1199.0)])
    assert len(sink.sent) == 1
    d = sink.sent[0]
    assert d.score.reasons[0].startswith("✅ confirmed on Target's own page")
    row = store.recent_deals()[0]
    assert row["trust"] == "community" and row["verified"] == "confirmed"


def test_unconfirmed_community_posts_never_reach_the_dashboard(clock):
    for routes, why in [({"target.com": (200, PAGE % "1999.00")}, "store's page shows $1,999.00"),
                        ({"target.com": (403, "captcha")}, "403")]:
        eng, store, sink, _ = community_engine(clock, routes)
        before = store.stats()["price_points"]
        r = eng.process([community_post(299.0)])
        assert sink.sent == [] and store.recent_deals() == []                # not shown, not logged
        assert r.suppressed[0][1].startswith("unconfirmed community post") and why in r.suppressed[0][1]
        assert store.stats()["price_points"] == before                        # claimed price never stored


def test_confirmation_happens_even_when_glitch_verify_is_off(clock):
    cfg = Config(database=":memory:", watches=[Watch("all")], alerts=AlertConfig(min_score=0),
                 learning=LearningConfig(explore_per_day=0, crowd_labels=False))
    from dealhawk.verify import VerifyConfig as VC
    f = FakeFetcher({"target.com": (403, "")})
    v = PriceVerifier(VC(enabled=False), http=Http(fetcher=f, retries=0, sleep=lambda s: None))
    sink = Sink()
    eng = Engine(cfg, Store(":memory:"), notifiers=[sink], verifier=v, clock=clock)
    eng.process([community_post(10.0, url="https://www.target.com/p/x")])
    assert sink.sent == [] and len(f.calls) == 1

