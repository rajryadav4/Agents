import json
from types import SimpleNamespace

from dealhawk.brain import Brain, LearningConfig
from dealhawk.config import AlertConfig, Config
from dealhawk.engine import Engine
from dealhawk.llm import VERDICT_SCHEMA, ClaudeJudge, LLMConfig, combine
from dealhawk.matching import Watch
from dealhawk.models import Offer, Score
from dealhawk.scoring import ScoringConfig
from dealhawk.store import Store

from .test_engine import Sink


class FakeMessages:
    def __init__(self, responder):
        self.responder, self.calls = responder, []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responder(kw)


def fake_client(responder):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(responder)))


def reply(verdicts, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop,
                           content=[SimpleNamespace(type="thinking", thinking=""),
                                    SimpleNamespace(type="text", text=json.dumps({"verdicts": verdicts}))])


def v(i, q=50, match=True, typical=0, error=False):
    return {"id": i, "product_match": match, "deal_quality": q, "estimated_typical_price": typical,
            "likely_price_error": error, "reason": "r"}


def test_request_shape_uses_structured_output_caching_and_fallbacks():
    client = fake_client(lambda kw: reply([v(0, 90)]))
    j = ClaudeJudge(LLMConfig(enabled=True, profile="student who loves Macs"), client=client)
    out = j.judge([(Offer("s", "Amazon", "MacBook Pro M4", "u", 1299.0), Score(30, "meh", "signals"))])
    assert out[0]["deal_quality"] == 90
    kw = client.beta.messages.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": VERDICT_SCHEMA}
    assert kw["output_config"]["effort"] == "low"
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert "student who loves Macs" in kw["system"]
    assert "thinking" not in kw and "temperature" not in kw
    assert '"price": 1299.0' in kw["messages"][0]["content"]


def test_refusal_and_garbage_are_safe():
    for resp in (reply([], stop="refusal"), reply([], stop="max_tokens"),
                 SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="nope")])):
        j = ClaudeJudge(LLMConfig(), client=fake_client(lambda kw, r=resp: r))
        assert j.judge([(Offer("s", "A", "t", "u", 1.0), Score(1, "meh", "signals"))]) == [None]


def test_combine_cold_start_mismatch_and_glitch():
    cfg = LLMConfig()
    cold = combine(Score(20, "meh", "reference"), v(0, 92, typical=1999), cfg)
    assert cold.value >= 82 and cold.typical_price == 1999 and cold.reasons[0].startswith("Claude (92/100)")
    case = combine(Score(90, "steal", "history"), v(0, 95, match=False), cfg)
    assert case.value == 27 and "not the real product" in case.reasons[0]
    glitch = combine(Score(40, "good", "signals"), v(0, 70, error=True), cfg)
    assert glitch.glitch_suspect and glitch.value >= 85
    nudged = combine(Score(60, "great", "history"), v(0, 90), cfg)
    assert 60 < nudged.value < 90


def test_engine_uses_claude_for_cold_start_and_respects_budget_and_cache(clock):
    def responder(kw):
        items = json.loads(kw["messages"][0]["content"].split("\n", 1)[1])
        return reply([v(it["id"], 92 if "MacBook" in it["title"] else 30,
                        match="Case" not in it["title"]) for it in items])
    client = fake_client(responder)
    llm = LLMConfig(enabled=True, batch_size=2, max_calls_per_hour=2)
    cfg = Config(database=":memory:", watches=[Watch("mac", keywords=["macbook"])],
                 alerts=AlertConfig(min_score=70), learning=LearningConfig(explore_per_day=0), llm=llm)
    store, sink = Store(":memory:"), Sink()
    brain = Brain(store, ScoringConfig(), cfg.learning, llm, judge=ClaudeJudge(llm, client=client))
    eng = Engine(cfg, store, notifiers=[sink], brain=brain, clock=clock)
    offers = [
        Offer("sd", "Amazon", 'MacBook Pro 14" M4 $1,299 at Amazon', "u1", 1299.0),       # no history at all
        Offer("sd", "Amazon", "MacBook Air Case $19", "u2", 19.0),
        Offer("sd", "Amazon", "Toaster $9", "u3", 9.0),                                   # not watched: not sent
        Offer("sd", "Amazon", "MacBook Air M1 $899", "u4", 899.0),
        Offer("sd", "Amazon", "MacBook stand $30", "u5", 30.0),
        Offer("sd", "Amazon", "MacBook sleeve $12", "u6", 12.0),
    ]
    r = eng.process(offers)
    calls = client.beta.messages.calls
    assert len(calls) == 2                                     # 5 candidates, batch 2, budget 2 calls/hour
    assert "Toaster" not in "".join(c["messages"][0]["content"] for c in calls)
    titles = [d.offer.title for d in sink.sent]
    assert any("M4" in t for t in titles)                      # the formula alone would have missed this
    assert not any("Case" in t for t in titles)
    eng.process(offers)                                        # same prices → cached, no new calls
    assert len(calls) == 2
