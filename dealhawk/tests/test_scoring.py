from dealhawk.models import Offer
from dealhawk.scoring import ScoringConfig, percentile, score_offer

ITEM_100 = [100, 100, 95, 100, 60, 100, 98, 62, 100, 99]          # $100 item; sales bottom ~$60
MACBOOK = [1999, 1999, 1899, 1999, 1799, 1949, 1999, 1699, 1999, 1899, 1999]


def offer(price, **kw):
    return Offer("t", "Shop", kw.pop("title", "thing"), "https://x", price, **kw)


def test_readme_example_100_to_20_is_a_steal_and_glitch():
    s = score_offer(offer(20), ITEM_100)
    assert s.value == 100 and s.tier == "steal"
    assert s.glitch_suspect and s.record_low and s.basis == "history"


def test_ordinary_20_percent_off_does_not_score():
    s = score_offer(offer(80), ITEM_100)
    assert s.value < 40 and s.tier == "meh"


def test_usual_sale_price_is_only_good():
    s = score_offer(offer(60), ITEM_100)
    assert s.tier in ("good", "great") and s.value < 80


def test_macbook_1300_flash_sale_is_a_steal():
    s = score_offer(offer(1300, title="MacBook Pro"), MACBOOK)
    assert s.tier == "steal" and s.record_low and not s.glitch_suspect


def test_full_price_scores_zero():
    assert score_offer(offer(1999), MACBOOK).value == 0


def test_score_monotonic_in_price():
    values = [score_offer(offer(p), ITEM_100).value for p in (90, 75, 60, 45, 30, 20)]
    assert values == sorted(values)


def test_cold_start_uses_reference_but_discounts_trust():
    s = score_offer(offer(25, reference_price=100, reference_trust=0.5), [])
    assert s.basis == "reference"
    assert s.value <= 50   # untrusted "was" price can't make a steal on its own
    trusted = score_offer(offer(25, reference_price=100, reference_trust=0.95), [])
    assert trusted.value > s.value


def test_source_stats_basis():
    s = score_offer(offer(90, typical_price=220, low_price=170), [])
    assert s.basis == "source-stats" and s.tier == "steal"


def test_signals_only_capped_but_price_mistake_flags_glitch():
    s = score_offer(offer(None, title="Hot thread"), [])
    assert s.basis == "signals"
    pm = score_offer(offer(296.99, title="PRICE MISTAKE: LG 65\" OLED $296.99"), [])
    assert pm.glitch_suspect and pm.tier == "steal"


def test_community_heat_bonus():
    now = 1_000_000.0
    cold = score_offer(offer(50, reference_price=100), [], now=now)
    hot = score_offer(offer(50, reference_price=100, community_score=200, posted_at=now - 3600), [], now=now)
    assert hot.value > cold.value
    assert any("community heat" in r for r in hot.reasons)


def test_min_history_points_respected():
    cfg = ScoringConfig(min_history_points=4)
    assert score_offer(offer(20), [100, 100, 100], cfg).basis != "history"


def test_percentile():
    assert percentile([5], 0.1) == 5
    assert percentile([0, 10], 0.5) == 5
