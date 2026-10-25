import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from warden.demo.world import build_world  # noqa: E402
from warden.gateway import Warden  # noqa: E402
from warden.policy.engine import Policy  # noqa: E402


@pytest.fixture
def world():
    w = build_world()
    yield w
    w.warden.ledger.close()


@pytest.fixture
def warden(world):
    return world.warden


@pytest.fixture
def empty_warden():
    w = Warden(policy=Policy(), db_path=":memory:")
    yield w
    w.ledger.close()


@pytest.fixture
def tokens(world):
    return world.tokens
