from dealhawk.matching import Watch, first_match, matches
from dealhawk.models import Offer, Score, normalize_title, title_fingerprint


def o(title, price=100.0, retailer="Amazon", **kw):
    return Offer("t", retailer, title, "u", price, **kw)


S = Score(80, "steal", "history", discount=0.5)


def test_keywords_phrase_any_order():
    w = Watch("mb", keywords=["macbook air"])
    assert matches(w, o('Apple 13" MacBook Air M3'), S, 70)
    assert matches(w, o("Air MacBook"), S, 70)
    assert not matches(w, o("MacBook Pro"), S, 70)


def test_exclude_and_regex():
    w = Watch("gpu", keywords=[r"re:\b(rtx|rx)\s?\d{4}"], exclude=["laptop"])
    assert matches(w, o("[GPU] MSI RTX 4070 Super"), S, 70)
    assert not matches(w, o("Gaming laptop RTX 4060"), S, 70)
    w2 = Watch("mb", keywords=["macbook"], exclude=["case"])
    assert not matches(w2, o("Hard shell case for MacBook Air"), S, 70)
    assert matches(w2, o("MacBook Air (Showcase edition)"), S, 70)   # whole-word only


def test_price_retailer_category_and_score():
    w = Watch("x", retailers=["Walmart"], max_price=50, min_score=90, categories=["gpu"])
    assert not matches(w, o("thing", 40, "Amazon", category="gpu"), S, 70)
    assert not matches(w, o("thing", 60, "Walmart", category="gpu"), S, 70)
    assert not matches(w, o("thing", 40, "Walmart", category="gpu"), S, 70)           # score 80 < 90
    assert matches(w, o("thing", 40, "Walmart", category="gpu"), Score(95, "steal", "history"), 70)
    assert matches(w, o("thing", 40, "walmart", category="GPU"), None, 70)            # score ignored


def test_min_discount_and_default_threshold():
    w = Watch("x", min_discount=0.6)
    assert not matches(w, o("t"), S, 70)
    assert not matches(Watch("any"), o("t"), Score(60, "great", "history"), 70)


def test_first_match_order():
    ws = [Watch("a", keywords=["ipad"]), Watch("everything")]
    assert first_match(ws, o("iPad mini"), S, 70).name == "a"
    assert first_match(ws, o("toaster"), S, 70).name == "everything"


def test_fingerprint_stable_across_price_and_order():
    assert title_fingerprint("Sony WH-1000XM5 $248 at Amazon") == title_fingerprint("Amazon: Sony WH-1000XM5 $199")
    assert "free" not in normalize_title("Free shipping on Sony")
