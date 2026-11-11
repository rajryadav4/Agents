"""Pull a current price and a claimed "was" price out of free-text deal titles.

Deal-site titles are messy:
    "Apple MacBook Air 13" M3 16GB/256GB $749 at Amazon"
    "[GPU] MSI RTX 4070 Super - $499 ($599 - $100)"
    "Sony WH-1000XM5 $248 (Reg. $399.99) + Free Shipping"
    "Instant Pot Duo - Save $40 - now $59.99, was $99.99"
"""

from __future__ import annotations

import re

_AMOUNT = r"\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
_AMOUNT_RE = re.compile(_AMOUNT)
# reddit-style "($599 - $100)": list price minus saving
_REF_MINUS_SAVING = re.compile(rf"\(\s*({_AMOUNT})\s*-\s*({_AMOUNT})\s*\)")

# words right before an amount that mark it as the *reference* price
_REF_BEFORE = re.compile(
    r"(?:\bwas|\breg\.?|\bregular(?:ly)?|\blist(?: price)?|\bmsrp|\bretail|\borig\.?|\boriginally|compare at|\bvalue)\s*:?\s*\(?\s*$",
    re.I,
)
# words right before an amount that mark it as *not a price* (savings, thresholds, rewards)
_SKIP_BEFORE = re.compile(
    r"(?:\bsave|\bsaving|\bsavings|\bextra|\bover|orders? of|\bspend|\bminus|\bless|\bget|\bearn|credit|reward|cash ?back|gift card|\bfor)\s*:?\s*$",
    re.I,
)
_SKIP_AFTER = re.compile(
    r"^\s*(?:off\b|discount|savings|credit|gift card|back\b|cash ?back|reward|/\s*mo|per month|a month|/\s*month|min(?:imum)?\b|\+ orders?)",
    re.I,
)
_FREE = re.compile(r"\bfree\b(?!\s+(?:shipping|ship|returns?|pickup|delivery|trial|w/|with|on|\$|gift))", re.I)


def _money(s: str) -> float:
    return float(s.replace("$", "").replace(",", "").strip())


def parse_prices(text: str) -> tuple[float | None, float | None]:
    """Return (current_price, reference_price) found in free text."""
    reference: float | None = None
    masked = text
    m = _REF_MINUS_SAVING.search(text)
    if m:
        reference = _money(m.group(1))
        masked = text[: m.start()] + " " * (m.end() - m.start()) + text[m.end():]

    current: float | None = None
    for m in _AMOUNT_RE.finditer(masked):
        before = masked[max(0, m.start() - 25): m.start()]
        after = masked[m.end(): m.end() + 20]
        value = _money(m.group(0))
        if _SKIP_AFTER.search(after) or _SKIP_BEFORE.search(before):
            continue
        if _REF_BEFORE.search(before):
            reference = reference if reference is not None else value
            continue
        if current is None:
            current = value
        elif reference is None and value > current:
            reference = value   # "now $59.99 $99.99": a second, larger amount is the old price
    if current is None and _FREE.search(text):
        current = 0.0
    if reference is not None and current is not None and reference <= current:
        reference = None
    return current, reference


def parse_percent_off(text: str) -> float | None:
    m = re.search(r"(\d{1,2})\s*%\s*off", text, re.I)
    return int(m.group(1)) / 100 if m else None
