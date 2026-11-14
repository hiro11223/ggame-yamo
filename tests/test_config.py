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
    assert "Sexual Content" in cfg.steam_tags()  # 除外タグも一覧取得対象
    assert cfg.currency.to_jpy["USD"] == 150


def test_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        make_config(fetch={"max_new_fetsh": 10})


def test_steamspy_interval_cannot_be_shortened():
    with pytest.raises(ValidationError):
        make_config(fetch={"steamspy_list_interval_sec": 10})


def test_reserved_category_name():
    with pytest.raises(ValidationError):
        make_config(categories={"all": {"label": "x", "steam": {"collect": ["Horror"]}}})


def test_require_any_defaults_to_collect():
    cfg = make_config(categories={"c": {"label": "x", "steam": {"collect": ["A", "B"]}}})
    assert cfg.categories["c"].steam.required() == ["A", "B"]
