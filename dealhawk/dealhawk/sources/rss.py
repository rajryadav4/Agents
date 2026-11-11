"""Generic RSS 2.0 / Atom source, with presets for the big deal communities.

Community deal sites are the best "any retailer" firehose: thousands of
hunters post Walmart/Target/Costco/Amazon price drops within minutes, including
price mistakes. We parse the price out of each post title and score it.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime
from urllib.parse import quote_plus

from ..models import Offer
from ..pricing import parse_prices
from ..retailers import detect_retailer, retailer_from_url
from .base import Source

PRESETS = {
    "slickdeals_frontpage": "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
    "slickdeals_popular": "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
    "slickdeals_search": "https://slickdeals.net/newsearch.php?q={query}&searcharea=deals&searchin=first&rss=1",
    "reddit_buildapcsales": "https://www.reddit.com/r/buildapcsales/new/.rss",
    "reddit_deals": "https://www.reddit.com/r/deals/new/.rss",
    "reddit_frugalmalefashion": "https://www.reddit.com/r/frugalmalefashion/new/.rss",
    "reddit_gamedeals": "https://www.reddit.com/r/GameDeals/new/.rss",
    "reddit_appledeals": "https://www.reddit.com/r/AppleDeals/new/.rss",
}

ATOM = "{http://www.w3.org/2005/Atom}"
_VOTES = re.compile(r"(?:thumb ?score|thumbs? up|votes?|score)\s*:?\s*\+?(\d+)", re.I)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def _parse_date(s: str) -> float | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_feed(xml_text: str) -> list[dict]:
    """Return [{title, link, summary, published, categories}] for RSS or Atom."""
    root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    items = []
    for it in root.iter("item"):
        items.append({
            "title": html.unescape(_text(it.find("title"))),
            "link": _text(it.find("link")),
            "summary": _text(it.find("description")) or _text(it.find("{http://purl.org/rss/1.0/modules/content/}encoded")),
            "published": _parse_date(_text(it.find("pubDate"))),
            "categories": [_text(c) for c in it.findall("category")],
        })
    for e in root.iter(f"{ATOM}entry"):
        link = e.find(f"{ATOM}link")
        items.append({
            "title": html.unescape(_text(e.find(f"{ATOM}title"))),
            "link": link.get("href", "") if link is not None else "",
            "summary": _text(e.find(f"{ATOM}content")) or _text(e.find(f"{ATOM}summary")),
            "published": _parse_date(_text(e.find(f"{ATOM}updated")) or _text(e.find(f"{ATOM}published"))),
            "categories": [c.get("term", "") for c in e.findall(f"{ATOM}category")],
        })
    return items


class RssSource(Source):
    type = "rss"
    default_interval = 120

    def __init__(self, name: str, url: str | None = None, preset: str | None = None,
                 query: str | None = None, **kw):
        super().__init__(name, **kw)
        if not url:
            if preset not in PRESETS:
                raise ValueError(f"source {name!r}: unknown preset {preset!r}; choose from {sorted(PRESETS)}")
            url = PRESETS[preset].format(query=quote_plus(query or ""))
        self.url = url

    def item_to_offer(self, item: dict) -> Offer:
        summary_text = _strip_html(item["summary"])
        # the post's own link may be the deal site; look for a retailer link in the body
        retailer_link = next((h for h in _HREF.findall(item["summary"]) if retailer_from_url(h)), None)
        price, ref = parse_prices(item["title"])
        votes = _VOTES.search(summary_text)
        tags = [c for c in item["categories"] if c]
        m = re.match(r"^\s*\[([^\]]{1,30})\]", item["title"])   # "[GPU] ..." reddit flair
        category = m.group(1).strip().lower() if m else (tags[0].lower() if tags else None)
        return Offer(
            source=self.name,
            retailer=detect_retailer(retailer_link or item["link"], item["title"] + " " + summary_text[:300]),
            title=item["title"],
            url=retailer_link or item["link"],
            price=price,
            reference_price=ref,
            reference_trust=0.7,
            category=category,
            community_score=int(votes.group(1)) if votes else None,
            posted_at=item["published"],
            tags=tags,
        )

    def fetch(self) -> list[Offer]:
        xml_text = self.http.get_text(self.url, {"Accept": "application/rss+xml, application/atom+xml, */*"})
        return [self.item_to_offer(i) for i in parse_feed(xml_text) if i["title"]]
