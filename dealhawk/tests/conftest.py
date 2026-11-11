import json
from pathlib import Path

import pytest

from dealhawk.sources.base import Http

FIX = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIX / name).read_text()


class FakeFetcher:
    """Maps URL substrings → (status, body). Records every request."""

    def __init__(self, routes: dict[str, tuple[int, bytes | str]] | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict, bytes | None]] = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        for frag, (status, payload) in self.routes.items():
            if frag in url:
                return status, payload.encode() if isinstance(payload, str) else payload
        return 404, b"not found"


@pytest.fixture
def fake_http():
    def make(routes):
        f = FakeFetcher(routes)
        return Http(fetcher=f, sleep=lambda s: None), f
    return make


class Clock:
    def __init__(self, t=1_794_412_800.0):   # 2026-11-11 10:00 UTC
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


def jload(name):
    return json.loads(fixture_text(name))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never touch the network: any real fetch fails loudly as a FetchError."""
    from dealhawk.sources import base

    def blocked(url, headers, body=None, timeout=20):
        raise base.FetchError(f"network disabled in tests: {url}")
    monkeypatch.setattr(base, "urllib_fetch", blocked)
