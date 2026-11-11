"""Reddit deal subreddits via the public JSON listing (falls back to RSS if blocked).

JSON gives us upvotes and post age, which feed the "community heat" signal:
a post racking up 100 upvotes in 20 minutes is usually a real steal.
"""

from __future__ import annotations

import html

from ..models import Offer
from ..pricing import parse_prices
from ..retailers import detect_retailer
from .base import FetchError, Source
from .rss import RssSource


class RedditSource(Source):
    type = "reddit"
    default_interval = 120

    def __init__(self, name: str, subreddits: list[str], sort: str = "new", limit: int = 50, **kw):
        super().__init__(name, **kw)
        if not subreddits:
            raise ValueError(f"source {name!r}: needs at least one subreddit")
        self.subreddits = subreddits
        self.sort = sort
        self.limit = limit

    def post_to_offer(self, d: dict, sub: str) -> Offer | None:
        if d.get("stickied") or d.get("over_18"):
            return None
        title = html.unescape(d.get("title", ""))
        flair = (d.get("link_flair_text") or "").strip()
        # keep expired posts: they never alert, but "sold out within hours" teaches the learner
        expired = flair.lower() in {"expired", "sold out", "oos", "dead"}
        link = d.get("url_overridden_by_dest") or d.get("url") or ""
        permalink = "https://www.reddit.com" + d.get("permalink", "")
        price, ref = parse_prices(title)
        category = None
        if title.startswith("[") and "]" in title:
            category = title[1:title.index("]")].strip().lower()
        return Offer(
            source=self.name,
            retailer=detect_retailer(link, title),
            title=title,
            url=link if link and "reddit.com" not in link else permalink,
            price=price,
            reference_price=ref,
            reference_trust=0.75,
            category=category,
            in_stock=False if expired else None,
            community_score=int(d.get("ups") or d.get("score") or 0) or None,
            posted_at=float(d["created_utc"]) if d.get("created_utc") else None,
            tags=[t for t in (flair, f"r/{sub}") if t],
        )

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        for sub in self.subreddits:
            url = f"https://www.reddit.com/r/{sub}/{self.sort}.json?limit={self.limit}&raw_json=1"
            try:
                data = self.http.get_json(url)
                for child in data.get("data", {}).get("children", []):
                    o = self.post_to_offer(child.get("data", {}), sub)
                    if o:
                        offers.append(o)
            except FetchError as e:
                if e.status not in (403, 429):
                    raise
                # Reddit often blocks anonymous JSON; the RSS feed is more tolerant.
                rss = RssSource(self.name, url=f"https://www.reddit.com/r/{sub}/{self.sort}/.rss", http=self.http)
                offers.extend(rss.fetch())
        return offers
