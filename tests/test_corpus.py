"""Frozen regression corpus for the injection detector.

Each case pins the detector's classification on one specific input. The corpus
is built deterministically from the synthetic generator and filtered to the
samples the detector currently classifies correctly, so every case is a
regression guard: if a future change flips one of these, that test fails.

The *error rate* (recall / false-positive rate on unfiltered data) is covered
separately in test_injection.py; this file guards individual decisions.
"""
import pytest

from warden.detect import dataset
from warden.detect.injection import InjectionDetector
from warden.detect.normalize import canonical

DET = InjectionDetector()


def _frozen(label_wanted: int, limit: int) -> list[str]:
    out, seen = [], set()
    # several seeds for variety; deterministic across runs
    for seed in range(1, 40):
        for text, label in dataset.generate(n=200, seed=seed):
            if label != label_wanted:
                continue
            key = canonical(text)[:120]
            if key in seen:
                continue
            seen.add(key)
            if DET.scan(text).is_injection == bool(label_wanted):
                out.append(text)
                if len(out) >= limit:
                    return out
    return out


FROZEN_ATTACKS = _frozen(1, 360)
FROZEN_BENIGN = _frozen(0, 360)


@pytest.mark.parametrize("text", FROZEN_ATTACKS)
def test_frozen_attack_flagged(text):
    r = DET.scan(text)
    assert r.is_injection, f"regression: attack no longer flagged (score={r.score:.2f}): {text[:80]}"


@pytest.mark.parametrize("text", FROZEN_BENIGN)
def test_frozen_benign_clean(text):
    r = DET.scan(text)
    assert not r.is_injection, f"regression: benign now flagged (score={r.score:.2f}, {r.reasons[:1]}): {text[:80]}"


def test_corpus_sizes():
    # make sure we actually assembled a large corpus
    assert len(FROZEN_ATTACKS) >= 350
    assert len(FROZEN_BENIGN) >= 350
