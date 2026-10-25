"""Text normalisation that strips the tricks attackers use to hide instructions.

Produces several *views* of the input; detectors scan all of them:

* canonical   NFKC, zero-width characters removed, homoglyphs folded, lower-case
* deleet      canonical with leetspeak undone (1gn0r3 → ignore)
* decoded     base64 / hex / url-encoded / rot13 payloads decoded
* despaced    letters joined when an attacker writes "i g n o r e"
* hidden      text hidden in HTML comments, alt-text, or zero-size/white styled elements
"""
from __future__ import annotations

import base64
import binascii
import codecs
import html
import re
import unicodedata
from urllib.parse import unquote

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠⁡⁢⁣⁤﻿­᠎"), None)

HOMOGLYPHS = str.maketrans({
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "һ": "h", "ӏ": "l", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "ь": "b", "ɡ": "g",
    "А": "a", "Е": "e", "О": "o", "Р": "p", "С": "c", "Х": "x", "У": "y", "І": "i", "К": "k", "М": "m",
    "Т": "t", "В": "b", "Н": "h",
    # Greek
    "α": "a", "ο": "o", "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t", "υ": "u", "χ": "x",
    "Α": "a", "Β": "b", "Ε": "e", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Χ": "x",
    # misc
    "ℓ": "l", "ı": "i", "ѵ": "v", "ԝ": "w", "ɑ": "a", "ǀ": "l",
})

LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "l"})

_B64 = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{16,}={0,2})(?![A-Za-z0-9+/=])")
_HEX = re.compile(r"\b((?:[0-9a-fA-F]{2}){10,})\b")
_URLENC = re.compile(r"(?:%[0-9a-fA-F]{2}){4,}")
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_HIDDEN_STYLE = re.compile(
    r"<(\w+)[^>]*style\s*=\s*[\"'][^\"']*(?:display\s*:\s*none|font-size\s*:\s*0|color\s*:\s*(?:#fff(?:fff)?|white)|opacity\s*:\s*0|visibility\s*:\s*hidden)[^\"']*[\"'][^>]*>(.*?)</\1>",
    re.S | re.I,
)
_ALT = re.compile(r"(?:alt|title|aria-label)\s*=\s*[\"']([^\"']{12,})[\"']", re.I)
_SPACED = re.compile(r"\b(?:[a-z][\s.\-_*]){4,}[a-z]\b", re.I)
_TAGS = re.compile(r"<[^>]+>")


def canonical(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(ZERO_WIDTH)
    t = t.translate(HOMOGLYPHS)
    t = html.unescape(t)
    return t.lower()


def _printable_ratio(s: str) -> float:
    if not s:
        return 0.0
    ok = sum(1 for ch in s if ch.isprintable() or ch in "\n\t")
    return ok / len(s)


def _decode_b64(text: str) -> list[str]:
    out = []
    for m in _B64.finditer(text):
        blob = m.group(1)
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
            s = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        letters = sum(c.isalpha() for c in s)
        if _printable_ratio(s) > 0.95 and letters >= max(4, len(s) * 0.5) and " " in s:
            out.append(s)
    return out


def _decode_hex(text: str) -> list[str]:
    out = []
    for m in _HEX.finditer(text):
        try:
            s = bytes.fromhex(m.group(1)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if _printable_ratio(s) > 0.95 and " " in s:
            out.append(s)
    return out


def hidden_segments(text: str) -> list[str]:
    segs = [m.group(1).strip() for m in _HTML_COMMENT.finditer(text)]
    segs += [_TAGS.sub(" ", m.group(2)).strip() for m in _HIDDEN_STYLE.finditer(text)]
    segs += [m.group(1).strip() for m in _ALT.finditer(text)]
    return [s for s in segs if s]


def views(text: str) -> dict[str, str]:
    if not text:
        return {"canonical": ""}
    can = canonical(text)
    out = {"canonical": can}
    de = can.translate(LEET)
    if de != can:
        out["deleet"] = de
    decoded: list[str] = []
    decoded += _decode_b64(text)
    decoded += _decode_hex(text)
    if _URLENC.search(text):
        decoded.append(unquote(text))
    if "rot13" in can or "rot-13" in can:
        decoded.append(codecs.decode(text, "rot13"))
    if decoded:
        out["decoded"] = canonical("\n".join(decoded))
    spaced = _SPACED.findall(can)
    if spaced:
        out["despaced"] = " ".join(re.sub(r"[\s.\-_*]", "", s) for s in spaced)
    hidden = hidden_segments(text)
    if hidden:
        out["hidden"] = canonical("\n".join(hidden))
    return out


def obfuscation_signals(text: str) -> list[str]:
    """Report *how* a text tried to hide itself (useful evidence on its own)."""
    signals = []
    if any(ord(c) in ZERO_WIDTH for c in text):
        signals.append("zero-width characters")
    if text.translate(HOMOGLYPHS) != text and any(ord(c) > 0x2FF for c in text):
        signals.append("homoglyph characters")
    if _decode_b64(text):
        signals.append("base64-encoded text")
    if _decode_hex(text):
        signals.append("hex-encoded text")
    if hidden_segments(text):
        signals.append("hidden HTML content")
    if _SPACED.search(canonical(text)):
        signals.append("letter-spaced words")
    return signals
