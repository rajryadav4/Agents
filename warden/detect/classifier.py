"""A small, dependency-free prompt-injection classifier.

Hashed word n-grams + character n-grams → L2-regularised logistic regression
trained with SGD.  It is tiny (a few MB of weights), trains in seconds on a
laptop, runs in well under a millisecond per message, and every prediction can
be explained by the top contributing n-grams.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import random
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

DIM = 1 << 18
_WORD = re.compile(r"[a-z0-9_@./:-]+|[{}<>\[\]|#=]+")


def _h(s: str) -> int:
    return zlib.crc32(s.encode()) & (DIM - 1)


def features(text: str) -> Counter:
    text = text.lower()
    words = _WORD.findall(text)
    feats: Counter = Counter()
    for w in words:
        feats["w:" + w] += 1
    for a, b in zip(words, words[1:]):
        feats["b:" + a + " " + b] += 1
    for a, b, c in zip(words, words[1:], words[2:]):
        feats["t:" + a + " " + b + " " + c] += 1
    squashed = " " + " ".join(words) + " "
    for n in (3, 4, 5):
        for i in range(len(squashed) - n + 1):
            feats[f"c{n}:" + squashed[i:i + n]] += 1
    return feats


def vectorize(text: str) -> tuple[dict[int, float], dict[int, str]]:
    raw = features(text)
    vec: dict[int, float] = {}
    names: dict[int, str] = {}
    for k, v in raw.items():
        idx = _h(k)
        vec[idx] = vec.get(idx, 0.0) + 1.0 + math.log(v)
        names.setdefault(idx, k)
    norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
    return {i: x / norm for i, x in vec.items()}, names


@dataclass
class InjectionClassifier:
    weights: dict[int, float] = field(default_factory=dict)
    bias: float = 0.0
    meta: dict = field(default_factory=dict)

    def predict_proba(self, text: str) -> float:
        if not text.strip():
            return 0.0
        vec, _ = vectorize(text)
        z = self.bias + sum(self.weights.get(i, 0.0) * x for i, x in vec.items())
        return 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, z))))

    def explain(self, text: str, top: int = 5) -> list[tuple[str, float]]:
        vec, names = vectorize(text)
        contrib = [(names[i], self.weights.get(i, 0.0) * x) for i, x in vec.items()]
        contrib.sort(key=lambda kv: -kv[1])
        return [(n.split(":", 1)[1].strip(), round(c, 3)) for n, c in contrib[:top] if c > 0]

    def fit(self, data: Iterable[tuple[str, int]], epochs: int = 6, lr: float = 0.5, l2: float = 1e-6, seed: int = 3) -> "InjectionClassifier":
        rows = [(vectorize(t)[0], y) for t, y in data]
        rng = random.Random(seed)
        w = self.weights
        b = self.bias
        for epoch in range(epochs):
            rng.shuffle(rows)
            rate = lr / (1 + epoch)
            for vec, y in rows:
                z = b + sum(w.get(i, 0.0) * x for i, x in vec.items())
                p = 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, z))))
                g = p - y
                for i, x in vec.items():
                    w[i] = w.get(i, 0.0) * (1 - rate * l2) - rate * g * x
                b -= rate * g
        self.weights = {i: v for i, v in w.items() if abs(v) > 1e-4}
        self.bias = b
        return self

    def evaluate(self, data: Iterable[tuple[str, int]], threshold: float = 0.5) -> dict[str, float]:
        tp = fp = tn = fn = 0
        for t, y in data:
            p = self.predict_proba(t) >= threshold
            if p and y:
                tp += 1
            elif p and not y:
                fp += 1
            elif not p and y:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        acc = (tp + tn) / max(1, tp + tn + fp + fn)
        return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
                "accuracy": round(acc, 4), "tp": tp, "fp": fp, "tn": tn, "fn": fn}

    # ----------------------------------------------------------- persistence
    def save(self, path: str) -> None:
        payload = {"bias": self.bias, "meta": self.meta,
                   "weights": {str(k): round(v, 5) for k, v in self.weights.items()}}
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))

    @classmethod
    def load(cls, path: str) -> "InjectionClassifier":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        return cls(weights={int(k): v for k, v in payload["weights"].items()}, bias=payload["bias"],
                   meta=payload.get("meta", {}))


DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "injection_model.json.gz")


def train_default(n: int = 6000, seed: int = 7, extra: list[tuple[str, int]] | None = None) -> tuple[InjectionClassifier, dict]:
    from warden.detect import dataset
    from warden.detect.normalize import canonical

    data = dataset.generate(n=n, seed=seed)
    train, test = dataset.iter_split(data)
    train = [(canonical(t), y) for t, y in train] + [(canonical(t), y) for t, y in (extra or [])]
    test = [(canonical(t), y) for t, y in test]
    clf = InjectionClassifier().fit(train)
    metrics = clf.evaluate(test)
    clf.meta = {"trained_on": len(train), "held_out": len(test), "metrics": metrics, "seed": seed}
    return clf, metrics


_cached: InjectionClassifier | None = None


def default_classifier() -> InjectionClassifier:
    global _cached
    if _cached is None:
        if os.path.exists(DEFAULT_MODEL_PATH):
            _cached = InjectionClassifier.load(DEFAULT_MODEL_PATH)
        else:
            _cached, _ = train_default()
            try:
                os.makedirs(os.path.dirname(DEFAULT_MODEL_PATH), exist_ok=True)
                _cached.save(DEFAULT_MODEL_PATH)
            except OSError:
                pass
    return _cached


def set_default_classifier(clf: InjectionClassifier) -> None:
    global _cached
    _cached = clf
