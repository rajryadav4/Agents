from dealhawk.brain import Brain, LearningConfig
from dealhawk.config import AlertConfig, Config
from dealhawk.engine import Engine
from dealhawk.learner import Learner, featurize
from dealhawk.matching import Watch
from dealhawk.models import Offer, Score
from dealhawk.scoring import ScoringConfig
from dealhawk.store import Store

from .test_engine import Sink


def deal_features(title, retailer, rule=55):
    o = Offer("t", retailer, title, "u", 100.0, category="laptops")
    return featurize(o, Score(rule, "good", "reference", discount=0.3))


def test_prior_mimics_formula():
    L = Learner()
    assert L.proba({"bias": 1, "rule": 0.9}) > 0.9
    assert L.proba({"bias": 1, "rule": 0.2}) < 0.05
    assert L.confidence == 0
    assert L.blend(55, {"bias": 1, "rule": 0.55}) [0] == 55      # no labels → formula unchanged


def test_learns_user_taste():
    L = Learner(warmup_labels=10)
    likes = deal_features("Lenovo ThinkPad T14 open-box", "Best Buy")
    dislikes = deal_features("Women's puffer jacket", "Kohl's")
    for _ in range(15):
        L.update(likes, 1)
        L.update(dislikes, 0)
    assert L.proba(likes) > 0.8 and L.proba(dislikes) < 0.2
    new_thinkpad = deal_features("ThinkPad X1 Carbon", "Best Buy")
    new_jacket = deal_features("Men's jacket", "Kohl's")
    assert L.proba(new_thinkpad) > L.proba(new_jacket)         # generalises via words / retailer
    assert L.blend(55, new_thinkpad)[0] > 55 > L.blend(55, new_jacket)[0]
    top = dict(L.top_weights(40))
    assert top["w=thinkpad"] > 0 and top["retailer=kohl's"] < 0


def test_persistence_roundtrip():
    L = Learner()
    L.update({"bias": 1, "w=x": 1}, 1)
    L2 = Learner.loads(L.dumps())
    assert L2.w == L.w and L2.n_labels == L.n_labels


def _engine(clock, explore=0):
    cfg = Config(database=":memory:", watches=[Watch("laptops", keywords=["thinkpad"])],
                 alerts=AlertConfig(min_score=70),
                 learning=LearningConfig(explore_per_day=explore, warmup_labels=6))
    store = Store(":memory:")
    sink = Sink()
    return Engine(cfg, store, notifiers=[sink], clock=clock), store, sink


def test_feedback_promotes_deals_formula_would_skip(clock):
    eng, store, sink = _engine(clock)
    # cold-start ThinkPads with a ~45% claimed discount score ~50: below the 70 bar
    offer = lambda i: Offer("sd", "Best Buy", f"Lenovo ThinkPad T14 Gen {i} open-box $549 (Reg. $999)",
                            f"https://bestbuy.com/{i}", 549.0, reference_price=999.0, reference_trust=0.95,
                            product_id=str(i))
    eng.process([offer(1)])
    assert sink.sent == []
    # the user 👍s ten similar ThinkPad deals (via buttons, WhatsApp replies or `dealhawk review`)
    for i in range(2, 12):
        feats = featurize(offer(i), Score(50, "good", "reference", discount=0.45, rule_value=50))
        eng.brain.learn(f"best-buy:{i}", 1, "user", feats, now=clock())
    r = eng.process([offer(100)])
    assert len(sink.sent) == 1, r.suppressed
    assert any("learned from your feedback" in x for x in sink.sent[0].score.reasons)
    # and the model survives a restart
    brain2 = Brain(store, ScoringConfig(), LearningConfig(warmup_labels=6), judge=False)
    assert brain2.learner.n_labels == eng.brain.learner.n_labels


def test_exploration_asks_about_borderline_deals(clock):
    eng, store, sink = _engine(clock, explore=2)
    mk = lambda i: Offer("sd", "Best Buy", f"ThinkPad E{i} $499 (Reg. $999)", "u", 499.0, reference_price=999.0,
                         reference_trust=0.95, product_id=f"e{i}")
    for i in range(5):
        eng.process([mk(i)])
    assert len(sink.sent) == 2 and all(d.explore for d in sink.sent)       # capped per day
    assert sink.sent[0].headline.startswith("🧪")


def test_crowd_labels_from_vote_velocity(clock):
    eng, store, _ = _engine(clock)
    now = clock()
    hot = Offer("r", "Newegg", "[GPU] RTX 4070 $429", "u", 429.0, community_score=300, posted_at=now - 2 * 3600,
                product_id="hot")
    cold = Offer("r", "Newegg", "[Case] some case $89", "u", 89.0, community_score=3, posted_at=now - 8 * 3600,
                 product_id="cold")
    gone = Offer("r", "Amazon", "[SSD] 990 Pro $99", "u", 99.0, in_stock=False, posted_at=now - 3600,
                 tags=["Expired"], product_id="gone")
    eng.process([hot, cold, gone])
    labels = {r["key"]: r["label"] for r in store.all_feedback()}
    assert labels == {"newegg:hot": 1, "newegg:cold": 0, "amazon:gone": 1}
    eng.process([hot])                                   # never double-counted
    assert store.stats()["labels_crowd"] == 3


def test_retrain_replays_labels(clock):
    eng, store, _ = _engine(clock)
    f = deal_features("ThinkPad", "Best Buy")
    eng.brain.learn("k1", 1, "user", f, deal_id=1)
    eng.brain.learn("k1", 0, "user", f, deal_id=1)       # user changed their mind: replaces
    assert [r["label"] for r in store.all_feedback()] == [0]
    assert eng.brain.retrain() == 1
