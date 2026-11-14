from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from yamo.config import Config, RetryConfig
from yamo.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 11, 15, 3, 17, tzinfo=dt.timezone.utc)


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


class FakeClock:
    """sleep で時間が進む擬似時計。実際には眠らない。"""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(fake_clock) -> HttpClient:
    return HttpClient(
        "yamo-test/0 (+https://example.invalid)",
        RetryConfig(max_attempts=3, base_sec=2, max_sec=60),
        sleep=fake_clock.sleep,
        clock=fake_clock.clock,
    )


def make_config(**overrides) -> Config:
    raw = {
        "user_agent": "yamo-test",
        "fetch": {"max_new_fetch": 10, "retry": {"max_attempts": 3}},
        "defaults": {"languages_any": ["japanese", "english"], "release_after": "2015-01-01"},
        "categories": {
            "horror": {
                "label": "ホラー",
                "steam": {
                    "require_any": ["Horror", "Psychological Horror"],
                    "exclude": ["Sexual Content"],
                    "bonus": {"Short": 5},
                },
                "filters": {
                    "max_price_jpy": 2000,
                    "min_positive_ratio": 0.8,
                    "reviews": {"min": 30, "max": 20000},
                },
                "weights": {"volume_sweet_spot": 500},
            }
        },
    }
    raw.update(overrides)
    return Config.model_validate(raw)


@pytest.fixture
def cfg() -> Config:
    return make_config()
