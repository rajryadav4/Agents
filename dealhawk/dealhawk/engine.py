"""The pipeline, once per poll of a source:

    offers ─► formula score ─► Claude (batched, cold-start/borderline) ─► learned model
           ─► match your watches ─► dedupe / quiet hours / caps ─► notify (with 👍/👎)
           └► crowd reactions become training labels
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .brain import Brain
from .config import Config, QuietHours
from .feedback import FeedbackConfig, links
from .matching import first_match
from .models import Deal, Offer, Score
from .notify import Notifier
from .scoring import score_offer
from .store import Store
from .verify import PriceVerifier

log = logging.getLogger("dealhawk.engine")
TIER_RANK = {"meh": 0, "good": 1, "great": 2, "steal": 3}


def in_quiet_hours(q: QuietHours, now: float) -> bool:
    local = datetime.fromtimestamp(now, ZoneInfo(q.timezone))
    hm = local.hour * 60 + local.minute
    sh, sm = map(int, q.start.split(":"))
    eh, em = map(int, q.end.split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    return start <= hm < end if start <= end else (hm >= start or hm < end)


@dataclass
class CycleReport:
    offers: int = 0
    matched: int = 0
    alerted: list[Deal] = field(default_factory=list)
    suppressed: list[tuple[Deal, str]] = field(default_factory=list)


class Engine:
    def __init__(self, cfg: Config, store: Store, notifiers: list[Notifier] | None = None,
                 brain: Brain | None = None, verifier: PriceVerifier | None = None, clock=time.time):
        self.cfg = cfg
        self.store = store
        self.notifiers = cfg.notifiers if notifiers is None else notifiers
        self.brain = brain or Brain(store, cfg.scoring, cfg.learning, cfg.llm)
        self.feedback: FeedbackConfig = cfg.feedback
        self.clock = clock
        self.verifier = verifier or PriceVerifier(cfg.verify, {src.name: src for src in cfg.sources}, clock=clock)
        self._lock = threading.Lock()    # the poller and dashboard "check now" may both call process()

    def _suppress_reason(self, deal: Deal, now: float) -> str | None:
        a, o, s = self.cfg.alerts, deal.offer, deal.score
        last = self.store.last_alert(o.key)
        if last is not None:
            deal.previous_alert_price = last["price"]
            dropped_more = o.price is not None and last["price"] and \
                o.price <= last["price"] * (1 - a.realert_drop_pct / 100)
            # the deal "came back": price went up after our alert and is now down again
            went_back_up = (now - last["ts"] > a.realert_after_hours * 3600
                            and (self.store.max_price_since(o.key, last["ts"]) or 0) > (last["price"] or 0) * 1.05)
            if not (dropped_more or went_back_up):
                return "already alerted"
        v = deal.verification
        if v is not None and v.live and self.cfg.verify.bypass_quiet_hours:
            return None                     # a confirmed live glitch goes out now, whatever the hour
        if a.quiet_hours and in_quiet_hours(a.quiet_hours, now) \
                and TIER_RANK[s.tier] < TIER_RANK.get(a.quiet_hours.except_tier, 3):
            return "quiet hours"
        if self.store.alerts_since(now - 3600) >= a.max_per_hour and s.tier != "steal":
            return "hourly alert cap"
        return None

    def notify(self, deal: Deal) -> bool:
        ok = False
        for n in self.notifiers:
            try:
                n.send(deal)
                ok = True
            except Exception as e:  # one broken channel must not stop the others
                log.error("notifier %s failed: %s", n.type, e)
        return ok

    def _verify(self, deal: Deal) -> str | None:
        """Re-check the live price of suspected glitches. Returns a suppress reason if it's gone."""
        o, s = deal.offer, deal.score
        if not self.verifier.wants(s.tier, s.glitch_suspect):
            return None
        v = deal.verification = self.verifier.verify(o)
        when = datetime.fromtimestamp(v.checked_at).strftime("%H:%M:%S")
        if v.live:
            if v.status == "lower":
                s.reasons.insert(0, f"price dropped further while checking: ${o.price:,.2f} → ${v.live_price:,.2f}")
                o.price = v.live_price
            s.value, s.tier = max(s.value, 95), "steal"
            note = " — check out fast; retailers can cancel pricing-error orders" if s.glitch_suspect else ""
            s.reasons.insert(0, f"✅ price verified live on {o.retailer} at {when} ({v.method}){note}")
            return None
        if v.status == "corrected":
            log.info("glitch already corrected: %s now $%.2f", o.title[:50], v.live_price)
            return f"price already corrected ({v.detail})"
        s.reasons.insert(0, f"⚠️ couldn't verify live price: {v.detail} — open it quickly")
        return None

    def _deliver(self, deal: Deal, now: float, report: CycleReport) -> None:
        reason = self._suppress_reason(deal, now)
        if reason is None:
            reason = self._verify(deal)
            if reason is None:
                reason = self._suppress_reason(deal, now)   # verification may lift quiet hours / caps
        if reason is None:
            deal.id = self.store.log_deal(deal, notified=False, ts=now)
            if self.feedback.active:
                deal.links = links(self.feedback, deal.id)
            if self.notify(deal):
                self.store.mark_notified(deal.id)
                self.store.save_alert(deal, now)
                report.alerted.append(deal)
                return
            reason = "all notifiers failed"
        elif not self.store.deal_logged_since(deal.offer.key, deal.offer.price, now - 86400):
            deal.id = self.store.log_deal(deal, notified=False, ts=now)
        report.suppressed.append((deal, reason))

    def _tracked(self, o: Offer, score: Score, features: dict, prior: list[float], now: float,
                 report: CycleReport) -> None:
        """Alert rules for products you track by link."""
        t = self.store.get_tracked(o.tracked_id)
        if not t or not t["active"] or o.price is None:
            return
        prev_low = min(prior) if prior else None
        self.store.update_tracked(t["id"], last_price=o.price, last_checked=now, status="ok", error=None,
                                  low_price=min(o.price, prev_low) if prev_low else o.price)
        if self.cfg.alerts.require_in_stock and o.in_stock is False:
            return
        why = None
        if t["mode"] == "target" and t["target_price"] and o.price <= t["target_price"]:
            why = f"🎯 at or below your target ${t['target_price']:,.2f}"
        elif t["mode"] == "smart":
            enough = len(prior) >= self.cfg.scoring.min_history_points
            if score.tier in ("steal", "great"):
                why = f"🤖 {score.tier} deal for this item"
            elif enough and prev_low and o.price <= prev_low * 0.97:
                why = f"🤖 lowest price since you started tracking (was ${prev_low:,.2f})"
        if score.glitch_suspect:
            why = why or "⚡ possible price glitch"
        if why is None:
            return
        score.reasons.insert(0, why)
        report.matched += 1
        before = len(report.alerted)
        self._deliver(Deal(o, score, f"Tracking #{t['id']}", features=features), now, report)
        if len(report.alerted) > before:
            self.store.update_tracked(t["id"], hits=(t["hits"] or 0) + 1)

    def process(self, offers: list[Offer]) -> CycleReport:
        with self._lock:
            return self._process(offers)

    def _process(self, offers: list[Offer]) -> CycleReport:
        report = CycleReport(offers=len(offers))
        now = self.clock()
        seen: set[str] = set()
        candidates: list[tuple[Offer, Score]] = []
        priors: dict[str, list[float]] = {}
        default_min = self.cfg.alerts.min_score

        # 1. formula score + record price history for everything we saw
        for o in offers:
            if o.key in seen:
                continue
            seen.add(o.key)
            history = self.store.history(o.key)       # prior prices, *before* recording this one
            priors[o.key] = history
            score = score_offer(o, history, self.cfg.scoring, now)
            self.store.record(o, now)
            candidates.append((o, score))

        # 2. ask Claude about offers that pass your filters (ignoring score) — batched, cached, capped
        relevant = [(o, s) for o, s in candidates
                    if o.in_stock is not False and (o.tracked_id is not None
                                                    or first_match(self.cfg.watches, o, None, default_min))]
        verdicts = self.brain.consult_claude(relevant, now)

        # 3. final score, learning, matching, delivery
        for o, score in candidates:
            score, features = self.brain.finalize(o, score, verdicts.get(o.key))
            if o.tracked_id is not None:
                self._tracked(o, score, features, priors[o.key], now, report)
                continue
            self.brain.observe_crowd(o, score, features, now)
            if self.cfg.alerts.require_in_stock and o.in_stock is False:
                continue
            watch = first_match(self.cfg.watches, o, score, default_min)
            explore = False
            if watch is None:
                watch = first_match(self.cfg.watches, o, None, default_min)
                if watch is None or not self.brain.should_explore(score, features, now):
                    continue
                explore = True
            report.matched += 1
            self._deliver(Deal(o, score, watch.name, explore=explore, features=features), now, report)
        return report
