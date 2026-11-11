"""Recognise which retailer an offer is from, from its URL or title."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# domain suffix -> display name. Order doesn't matter; longest suffix wins.
RETAILER_DOMAINS: dict[str, str] = {
    "amazon.com": "Amazon",
    "amzn.to": "Amazon",
    "woot.com": "Woot",
    "walmart.com": "Walmart",
    "target.com": "Target",
    "bestbuy.com": "Best Buy",
    "costco.com": "Costco",
    "samsclub.com": "Sam's Club",
    "newegg.com": "Newegg",
    "bhphotovideo.com": "B&H Photo",
    "adorama.com": "Adorama",
    "microcenter.com": "Micro Center",
    "homedepot.com": "Home Depot",
    "lowes.com": "Lowe's",
    "macys.com": "Macy's",
    "kohls.com": "Kohl's",
    "nordstrom.com": "Nordstrom",
    "nordstromrack.com": "Nordstrom Rack",
    "ebay.com": "eBay",
    "dell.com": "Dell",
    "lenovo.com": "Lenovo",
    "hp.com": "HP",
    "apple.com": "Apple",
    "samsung.com": "Samsung",
    "gamestop.com": "GameStop",
    "wayfair.com": "Wayfair",
    "ulta.com": "Ulta",
    "sephora.com": "Sephora",
    "nike.com": "Nike",
    "adidas.com": "Adidas",
    "rei.com": "REI",
    "dickssportinggoods.com": "Dick's",
    "staples.com": "Staples",
    "officedepot.com": "Office Depot",
    "kroger.com": "Kroger",
    "cvs.com": "CVS",
    "walgreens.com": "Walgreens",
    "zappos.com": "Zappos",
    "jcpenney.com": "JCPenney",
    "bjs.com": "BJ's",
    "aliexpress.com": "AliExpress",
    "steampowered.com": "Steam",
    "store.steampowered.com": "Steam",
    "gog.com": "GOG",
    "epicgames.com": "Epic Games",
}

_NAME_PATTERNS = sorted(set(RETAILER_DOMAINS.values()), key=len, reverse=True)


def retailer_from_url(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    best = None
    for domain, name in RETAILER_DOMAINS.items():
        if (host == domain or host.endswith("." + domain)) and (best is None or len(domain) > best[0]):
            best = (len(domain), name)
    return best[1] if best else None


def retailer_from_title(title: str) -> str | None:
    """Deal-site titles usually end with '... $X at Store' or '(Store)'."""
    for name in _NAME_PATTERNS:
        n = re.escape(name)
        if re.search(rf"(?:\bat|\bfrom|\bvia|@)\s+{n}\b|\(\s*{n}\s*\)|\[\s*{n}\s*\]|^{n}\s*[:\-]", title, re.I):
            return name
    return None


def detect_retailer(url: str | None, title: str, default: str = "Unknown") -> str:
    return retailer_from_url(url) or retailer_from_title(title) or default
