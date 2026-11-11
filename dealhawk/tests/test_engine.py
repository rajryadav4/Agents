from dealhawk.brain import Brain, LearningConfig
from dealhawk.config import AlertConfig, Config, QuietHours
from dealhawk.engine import Engine, in_quiet_hours
from dealhawk.matching import Watch
from dealhawk.models import Offer
from dealhawk.store import Store


class Sink:
    type = "sink"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, deal):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(deal)


def make(clock, watches=None, alerts=None, learning=None, notifiers=None):
    cfg = Config(database=":memory:", watches=watches or [Watch("all")],
                 alerts=alerts or AlertConfig(min_score=70), learning=learning or LearningConfig(explore_per_day=0))
    store = Store(":memory:")
    sink = Sink()
    eng = Engine(cfg, store, notifiers=notifiers or [sink], clock=clock)
    return eng, store, sink


def item(price, pid="sku1", **kw):
    return Offer("shop", "Walmart", kw.pop("title", "Ninja Air Fryer"), "https://walmart.com/ip/1", price,
                 product_id=pid, **kw)


def build_history(eng, clock, prices):
    for p in prices:
        eng.process([item(p)])
        clock.advance(7 * 3600)     # > resample window so each one is recorded


def test_alerts_only_on_unusual_drop(clock):
    eng, store, sink = make(clock)
    # an item that regularly goes on sale for ~$60
    build_history(eng, clock, [100, 62, 100, 60, 100, 98, 100, 61, 100, 59, 100])
    assert sink.sent == []                                   # routine prices / usual sales: silent
    r = eng.process([item(20)])
    assert len(r.alerted) == 1 and sink.sent[0].score.tier == "steal"
    assert store.stats()["alerts_sent"] == 1


def test_no_duplicate_alert_but_realert_on_further_drop(clock):
    eng, store, sink = make(clock)
    build_history(eng, clock, [100] * 6)
    eng.process([item(30)])
    clock.advance(600)
    r = eng.process([item(30)])
    assert len(sink.sent) == 1 and r.suppressed[0][1] == "already alerted"
    clock.advance(600)
    eng.process([item(25)])                                   # ≥5% lower than the alert → alert again
    assert len(sink.sent) == 2 and sink.sent[1].previous_alert_price == 30


def test_quiet_hours_hold_non_steals(clock):
    q = QuietHours(start="00:00", end="23:59", timezone="UTC", except_tier="steal")
    eng, _, sink = make(clock, alerts=AlertConfig(min_score=40, quiet_hours=q))
    build_history(eng, clock, [100] * 6)
    r = eng.process([item(80)])                               # "good" tier
    assert sink.sent == [] and r.suppressed[0][1] == "quiet hours"
    eng.process([item(10)])
    assert len(sink.sent) == 1                                # steal breaks through


def test_in_quiet_hours_wraps_midnight():
    q = QuietHours(start="23:00", end="07:00", timezone="UTC")
    assert in_quiet_hours(q, 1_794_444_000)        # 2026-11-11 23:20 UTC
    assert not in_quiet_hours(q, 1_794_412_800)    # 10:00 UTC


def test_out_of_stock_never_alerts(clock):
    eng, _, sink = make(clock)
    build_history(eng, clock, [100] * 6)
    eng.process([item(10, in_stock=False)])
    assert sink.sent == []


def test_watch_filters_apply(clock):
    eng, _, sink = make(clock, watches=[Watch("mac", keywords=["macbook"])])
    build_history(eng, clock, [100] * 6)
    eng.process([item(10)])
    assert sink.sent == []


def test_failing_notifier_does_not_lose_others(clock):
    good = Sink()
    eng, store, _ = make(clock, notifiers=[Sink(fail=True), good])
    build_history(eng, clock, [100] * 6)
    eng.process([item(10)])
    assert len(good.sent) == 1


def test_all_notifiers_failing_is_reported(clock):
    eng, store, _ = make(clock, notifiers=[Sink(fail=True)])
    build_history(eng, clock, [100] * 6)
    r = eng.process([item(10)])
    assert r.suppressed[0][1] == "all notifiers failed" and store.stats()["alerts_sent"] == 0


def test_hourly_cap_lets_steals_through(clock):
    eng, _, sink = make(clock, alerts=AlertConfig(min_score=30, max_per_hour=1))
    for pid in ("x", "y", "z"):
        for p in [100] * 6:
            eng.process([item(p, pid=pid)])
            clock.advance(7 * 3600)
    eng.process([item(10, pid="x")])                   # steal → sent (cap now hit)
    r = eng.process([item(80, pid="y")])               # "good" tier → capped
    assert any(why == "hourly alert cap" for _, why in r.suppressed)
    eng.process([item(5, pid="z")])                    # steal → still sent
    assert len(sink.sent) == 2


def test_deals_logged_with_features_for_learning(clock):
    eng, store, sink = make(clock)
    build_history(eng, clock, [100] * 6)
    eng.process([item(10)])
    d = store.recent_deals()[0]
    assert d["notified"] == 1 and store.get_deal(d["id"])["features"]["rule"] > 0.8
    assert sink.sent[0].id == d["id"]
