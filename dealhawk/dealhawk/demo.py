"""Sample data for `dealhawk serve --demo` — to preview the dashboard only.

Everything here is invented: titles start with "Sample:", there are no links,
and the page shows a DEMO banner. Real deals only ever come from live sources.
"""

from __future__ import annotations

import random
import time

from .models import Deal, Offer
from .scoring import score_offer
from .store import Store

SAMPLES = [
    # retailer, title, normal price history, today's price, category
    ("Walmart", 'Sample: 65" OLED 4K TV', [1799, 1799, 1499, 1799, 1599, 1799], 296.99, "tv"),
    ("Amazon", 'Sample: 14" laptop, M-series, 16GB/512GB', [1999, 1999, 1899, 1999, 1799, 1699, 1999], 1299.0, "laptops"),
    ("Target", "Sample: noise-cancelling headphones", [399, 399, 349, 399, 329, 399], 299.0, "audio"),
    ("Best Buy", "Sample: 27\" 1440p 240Hz monitor", [449, 449, 399, 449, 379, 449], 349.0, "monitors"),
    ("Costco", "Sample: cordless stick vacuum", [749, 749, 649, 749, 699], 399.99, "home"),
    ("Newegg", "Sample: 2TB NVMe SSD", [189, 179, 189, 169, 189, 175], 139.0, "storage"),
    ("Home Depot", "Sample: 20V drill + impact driver kit", [229, 229, 199, 229, 179], 99.0, "tools"),
    ("Amazon", "Sample: wireless earbuds", [249, 249, 199, 249, 189, 249], 179.0, "audio"),
    ("Walmart", "Sample: 6-qt air fryer", [129, 99, 129, 89, 129], 84.0, "kitchen"),
    ("B&H Photo", "Sample: mirrorless camera body", [2499, 2499, 2299, 2499, 2199], 1698.0, "cameras"),
]


def _add(store: Store, i: int, now: float, retailer: str, title: str, hist: list[float], price: float,
         cat: str, explore: bool = False) -> None:
    pid = f"sample-{i}"
    for j, h in enumerate(hist):
        store.record(Offer("demo", retailer, title, "", h, product_id=pid), ts=now - (len(hist) - j) * 86400 * 3)
    o = Offer("demo", retailer, title, "", price, product_id=pid, category=cat)
    s = score_offer(o, store.history(o.key), now=now)
    store.record(o, ts=now)
    d = Deal(o, s, "Demo watch", explore=explore or s.tier in ("good", "meh"), features={"bias": 1.0})
    if s.glitch_suspect:
        from .verify import VerifyResult
        d.verification = VerifyResult("confirmed", price, "page", now)
    store.log_deal(d, notified=s.tier == "steal", ts=now)


def seed(store: Store, order: tuple = (3, 2, 5, 8, 6, 1, 0)) -> None:
    now = time.time()
    for n, i in enumerate(order):
        _add(store, i, now - (len(order) - n) * 420, *SAMPLES[i])


def seed_tracked(store: Store) -> None:
    """Two sample tracked products (no real links) so the tracking panel can be previewed."""
    now = time.time()
    for title, retailer, mode, target, prices in [
        ("Sample: 65\" OLED TV you're watching", "Best Buy", "target", 1299.0, [1799, 1799, 1699, 1799, 1499, 1599, 1449]),
        ("Sample: laptop on AI watch", "Amazon", "smart", None, [1999, 1999, 1899, 1949, 1999, 1849, 1899]),
    ]:
        tid = store.add_tracked("https://example.com/sample", mode, target, title=title, retailer=retailer,
                                ts=now - 20 * 86400)
        for j, pr in enumerate(prices):
            store.record(Offer("demo", retailer, title, "", pr, product_id=f"track-{tid}"),
                         ts=now - (len(prices) - j) * 3 * 86400)
        store.update_tracked(tid, last_price=prices[-1], low_price=min(prices), status="ok", last_checked=now - 300)


def trickle(store: Store, every: float = 12.0) -> None:
    """Add another sample deal now and then, so the live updates can be seen."""
    i = 1000
    while True:
        time.sleep(every)
        retailer, title, hist, price, cat = random.choice(SAMPLES)
        _add(store, i, time.time(), retailer, title, hist, round(price * random.uniform(0.85, 1.05), 2), cat)
        i += 1
