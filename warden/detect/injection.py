"""Prompt-injection detector = obfuscation-aware heuristics + ML classifier.

Every view produced by :mod:`warden.detect.normalize` is scored; the final
score is the maximum, so hiding an attack in base64 or zero-width characters
does not lower it. Obfuscation itself adds a small penalty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from warden.detect import normalize
from warden.detect.classifier import InjectionClassifier, default_classifier

# (pattern, weight, label, strong). Applied to every normalised view.
# strong heuristics fire on their own; weak ones only count with corroboration
# (ML elevated, or another heuristic also fired) to keep false positives low on
# benign "instructions to an agent" text.
HEURISTICS: list[tuple[re.Pattern, float, str, bool]] = [
    (re.compile(r"\b(ignore|disregard|forget|override|bypass|set aside|discard|abandon)\b[^.\n]{0,40}\b(previous|prior|above|earlier|preceding|original|the developer'?s?|system)\b[^.\n]{0,25}\b(instruction|instructions|directions?|rules?|prompt|guidelines|context|constraints|programming|messages?)\b", re.I), 0.95, "instruction override", True),
    (re.compile(r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,20}\b(all|any|your)\b[^.\n]{0,20}\b(instruction|instructions|rules?|prompt|guidelines|constraints|programming)\b", re.I), 0.95, "instruction override", True),
    (re.compile(r"\b(you are now|you're now|you are no longer)\s+(an? )?(ai|assistant|system|admin|administrator|unrestricted|evil|different|dan|root|superuser|jailbroken|in (?:developer|god|maintenance) mode|free|without)|from now on,? you (?:are|must|will) (?:act|behave|respond|ignore|obey|comply|send|do)|pretend (?:to be|you are) (?:an? )?(?:ai|assistant|system|admin|administrator|unrestricted|evil|different|dan|root|superuser|jailbroken)|act as (?:an?|the) (?:ai|assistant|system|admin|unrestricted|evil)|roleplay as (?:an?|the)|you have been jailbroken|enter developer mode|in developer mode,? you (?:are|must|will|can)|enable god mode|no (?:content )?(?:policy|restrictions|limits|filters) (?:apply|now|anymore)", re.I), 0.85, "role hijack", True),
    (re.compile(r"(<\|im_start\|>|\[inst\]|\[\[assistant|<system>|###\s*system|\bsystem override\b|note to (?:the )?ai\b|ai (?:assistant|agent)s? (?:directive|instruction)|instruction for (?:the )?llm|attention ai\b|priority instructions)", re.I | re.M), 0.8, "fake authority / system message", True),
    (re.compile(r"^\s*\[?system\]?\s*:|admin notice\s*:|important note to ai", re.I | re.M), 0.6, "fake authority marker", False),
    (re.compile(r"\b(send|forward|email|e-mail|upload|post|transfer|share|leak|exfiltrate|copy|mail|dump|attach)\b[^.\n]{0,60}\b(customer list|customers|all users|every user|entire|full|private|internal|api key|api keys|secret|secrets|password|passwords|credentials?|keys?|tokens?|database|all records|history|conversation|system prompt|\.env|/etc/passwd|spreadsheet|contacts)\b[^.\n]{0,60}(\bto\b|\bat\b|\binto\b)\s*[^\s]*(@|https?://|\bwww\.)", re.I), 0.9, "data exfiltration instruction", True),
    (re.compile(r"!\[[^\]]*\]\(\s*https?://[^)\s]*\?[^)\s]*(\{\{|\[|%7b|secret|data=|q=|key=|cookie|email|conversation)", re.I), 0.9, "markdown image exfiltration", True),
    (re.compile(r"(\"tool\"\s*:\s*\"|<tool_call>|function_call\s*:|\bcall the (?:function|tool) \w+ (?:with|to)\b|\buse the \w+ tool to (?:send|upload|post|delete|transfer|forward|wire)\b|\binvoke \w+\([^)]*(secret|key|customer|password|all))", re.I), 0.7, "tool-call smuggling", False),
    (re.compile(r"\b(do not|don't|never)\s+(tell|inform|mention|reveal|log)\b[^.\n]{0,30}\b(user|the user|anyone|human|this (?:step|action|instruction)|these instructions)", re.I), 0.55, "concealment request", False),
    (re.compile(r"\b(curl|wget)\b[^\n|]{0,80}\|\s*(ba|z)?sh\b|\brm\s+-rf\s+/(?:\s|$)|\bchmod\s+777\s+/|\bmkfs\b|:\(\)\s*\{", re.I), 0.85, "destructive command", True),
    (re.compile(r"\b(ask|collect|request|obtain)\b[^.\n]{0,30}\b(user'?s? )?(password|credit card|cvv|one-time pass(?:code|word)|otp|2fa code|ssn|social security)", re.I), 0.7, "credential phishing", False),
    (re.compile(r"\b(the|an?)\s+(assistant|ai|agent|model|llm)\s+(must|should|will|needs to|has to)\b[^.\n]{0,80}\b(send|transfer|delete|grant|disable|execute|wire|forward|upload)\b[^.\n]{0,40}(secret|key|customer|password|all|database|everyone|outside|external|@|https?://)", re.I), 0.7, "imperative aimed at the AI", False),
    (re.compile(r"\b(new|updated|real|true|secret|hidden)\s+(instructions?|directive|objective|task|mission)\s*[:\-]\s*\S", re.I), 0.6, "replacement objective", False),
    (re.compile(r"\bdecode (?:and|then) (?:follow|execute|run|obey)", re.I), 0.8, "encoded payload", True),
]

# Benign phrases that look scary; reduce false positives on documentation-like text.
DAMPENERS = [
    re.compile(r"\b(prompt injection is|researchers (?:found|describe)|example of (?:a )?prompt injection|defen[cs]es? (?:against|include))\b", re.I),
]


@dataclass
class InjectionResult:
    score: float
    is_injection: bool
    reasons: list[str] = field(default_factory=list)
    obfuscation: list[str] = field(default_factory=list)
    ml_score: float = 0.0
    heuristic_score: float = 0.0
    top_ngrams: list[tuple[str, float]] = field(default_factory=list)
    view: str = "canonical"

    def to_dict(self) -> dict:
        return {"score": round(self.score, 4), "is_injection": self.is_injection, "reasons": self.reasons,
                "obfuscation": self.obfuscation, "ml_score": round(self.ml_score, 4),
                "heuristic_score": round(self.heuristic_score, 4), "top_ngrams": self.top_ngrams, "view": self.view}


class InjectionDetector:
    def __init__(self, classifier: InjectionClassifier | None = None, threshold: float = 0.6) -> None:
        self._clf = classifier
        self.threshold = threshold

    @property
    def classifier(self) -> InjectionClassifier:
        if self._clf is None:
            self._clf = default_classifier()
        return self._clf

    @classifier.setter
    def classifier(self, clf: InjectionClassifier) -> None:
        self._clf = clf

    def scan(self, text: str, explain: bool = False) -> InjectionResult:
        if not text or not text.strip():
            return InjectionResult(0.0, False)
        vs = normalize.views(text)
        obf = normalize.obfuscation_signals(text)
        best_ml, best_view = 0.0, "canonical"
        strong_h, weak_h = 0.0, 0.0
        strong_reasons: list[str] = []
        weak_reasons: list[str] = []
        weak_count = 0
        for name, view in vs.items():
            # Long documents: score sentence windows so one injected line is not diluted.
            chunks = _chunks(view)
            ml = max(self.classifier.predict_proba(c) for c in chunks)
            for pat, weight, label, strong in HEURISTICS:
                if pat.search(view):
                    tag = label if name == "canonical" else f"{label} (in {name} text)"
                    if strong:
                        strong_h = max(strong_h, weight)
                        if tag not in strong_reasons:
                            strong_reasons.append(tag)
                    else:
                        weak_h = max(weak_h, weight)
                        weak_count += 1
                        if tag not in weak_reasons:
                            weak_reasons.append(tag)
            if max(ml, strong_h) > max(best_ml, strong_h) or ml > best_ml:
                best_view = name
            best_ml = max(best_ml, ml)
        damp = any(d.search(vs["canonical"]) for d in DAMPENERS)
        # weak heuristics only count with corroboration (ML elevated or >=2 weak signals)
        weak_active = weak_h if (best_ml >= 0.45 or weak_count >= 2) else 0.0
        best_h = max(strong_h, weak_active)
        reasons = strong_reasons + (weak_reasons if weak_active else [])
        score = max(best_h, best_ml * (0.9 if best_h else 0.85))
        if best_h and best_ml > 0.5:
            score = min(1.0, score + 0.05)
        if obf and score > 0.3:
            score = min(1.0, score + 0.1)
        if damp and best_h < 0.9:
            score *= 0.5
        if best_ml >= 0.5 and not reasons:
            reasons.append("ML classifier: resembles known injection attacks")
        res = InjectionResult(score=score, is_injection=score >= self.threshold, reasons=reasons, obfuscation=obf,
                              ml_score=best_ml, heuristic_score=best_h, view=best_view)
        if explain:
            res.top_ngrams = self.classifier.explain(vs.get(best_view, vs["canonical"]))
        return res


def _chunks(text: str, size: int = 400) -> list[str]:
    if len(text) <= size:
        return [text]
    parts = re.split(r"(?<=[.!?\n])\s+", text)
    out, cur = [], ""
    for p in parts:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return out or [text[:size]]
