from pathlib import Path

import pytest
from pydantic import ValidationError

from yamo.config import load_config

from .conftest import make_config

ROOT = Path(__file__).parent.parent


def test_repository_config_is_valid():
    cfg = load_config(ROOT / "config.yaml")
    assert set(cfg.categories) == {"horror", "bakage"}
    assert "Psychological Horror" in cfg.steam_tags()
    assert "Sexual Content" in cfg.steam_tags()  # 除外・加点タグも実在検証の対象
    assert "Destruction" in cfg.steam_tags()
    assert cfg.currency.to_jpy["USD"] == 150


def test_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        make_config(fetch={"max_new_fetsh": 10})


def test_steam_interval_and_page_size_limits():
    with pytest.raises(ValidationError):
        make_config(fetch={"steam_interval_sec": 0.5})
    with pytest.raises(ValidationError):
        make_config(fetch={"query_page_size": 2000})  # StoreQuery の上限は 1000


def test_reserved_category_name():
    with pytest.raises(ValidationError):
        make_config(categories={"all": {"label": "x", "steam": {"require_any": ["Horror"]}}})


def test_require_any_must_not_be_empty():
    with pytest.raises(ValidationError):
        make_config(categories={"c": {"label": "x", "steam": {"require_any": []}}})
