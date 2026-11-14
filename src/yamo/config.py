"""config.yaml の読み込みと検証。

typo を早期に検出するため、全モデルで未知のキーを禁止している。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryConfig(_Strict):
    max_attempts: int = Field(5, ge=1)
    base_sec: float = Field(2.0, ge=0)
    max_sec: float = Field(120.0, ge=0)


class FetchConfig(_Strict):
    steamspy_interval_sec: float = Field(1.0, ge=1.0)  # SteamSpy の規約: 1req/秒
    steamspy_list_interval_sec: float = Field(60.0, ge=60.0)  # 一覧系: 1req/分
    appdetails_interval_sec: float = Field(2.0, ge=0)  # Steam ストアAPI（GetItems / appdetails）
    getitems_batch_size: int = Field(100, ge=1)
    web_interval_sec: float = Field(2.0, ge=0)
    timeout_sec: float = Field(30.0, gt=0)
    max_new_fetch: int = Field(300, ge=0)
    max_streaming_checks: int = Field(150, ge=0)
    retry: RetryConfig = RetryConfig()


class CacheTtlConfig(_Strict):
    appdetails: int = Field(7, ge=0)
    streaming: int = Field(30, ge=0)


class CurrencyConfig(_Strict):
    to_jpy: dict[str, float] = {"JPY": 1.0}

    @field_validator("to_jpy")
    @classmethod
    def _upper(cls, v: dict[str, float]) -> dict[str, float]:
        out = {k.upper(): float(r) for k, r in v.items()}
        out.setdefault("JPY", 1.0)
        return out


class ItchConfig(_Strict):
    retention_days: int = Field(120, ge=1)
    feeds: list[Literal["newest", "popular", "top_rated"]] = ["newest", "popular", "top_rated"]


class MergeConfig(_Strict):
    developer_similarity: float = Field(0.8, ge=0, le=1)


class StreamingConfig(_Strict):
    excluded_hosts: list[str] = []


class DefaultsConfig(_Strict):
    languages_any: list[str] = ["japanese", "english"]
    release_after: dt.date | None = None
    include_coming_soon: bool = False


class SteamTagsConfig(_Strict):
    collect: list[str] = Field(min_length=1)
    require_any: list[str] = []
    exclude: list[str] = []
    bonus: dict[str, float] = {}

    def all_tags(self) -> set[str]:
        return set(self.collect) | set(self.require_any) | set(self.exclude) | set(self.bonus)

    def required(self) -> list[str]:
        """どれか1つは持っていなければならないタグ。未指定なら collect と同じ。"""
        return self.require_any or self.collect


class ItchTagsConfig(_Strict):
    tags: list[str] = []


class ReviewRange(_Strict):
    min: int | None = Field(None, ge=0)
    max: int | None = Field(None, ge=0)


class FiltersConfig(_Strict):
    max_price_jpy: float | None = Field(None, ge=0)
    min_positive_ratio: float | None = Field(None, ge=0, le=1)
    reviews: ReviewRange = ReviewRange()
    require_demo: bool = False
    # 未指定なら defaults の値を使う
    languages_any: list[str] | None = None
    release_after: dt.date | None = None


class WeightsConfig(_Strict):
    rating: float = Field(0.5, ge=0)
    volume: float = Field(0.3, ge=0)
    recency: float = Field(0.2, ge=0)
    volume_sweet_spot: float = Field(500, gt=0)
    volume_sigma: float = Field(0.5, gt=0)  # log10 スケールでの山の幅（0.5 = ±半桁）
    recency_half_life_years: float = Field(2.0, gt=0)

    @model_validator(mode="after")
    def _nonzero(self) -> "WeightsConfig":
        if self.rating + self.volume + self.recency <= 0:
            raise ValueError("weights の rating/volume/recency の合計が 0 です")
        return self


class CategoryConfig(_Strict):
    label: str
    steam: SteamTagsConfig | None = None
    itch: ItchTagsConfig | None = None
    filters: FiltersConfig = FiltersConfig()
    weights: WeightsConfig = WeightsConfig()


class ItchScoringConfig(_Strict):
    recency: float = Field(0.5, ge=0)
    price: float = Field(0.2, ge=0)
    tag_match: float = Field(0.3, ge=0)


class Config(_Strict):
    user_agent: str
    fetch: FetchConfig = FetchConfig()
    cache_ttl_days: CacheTtlConfig = CacheTtlConfig()
    currency: CurrencyConfig = CurrencyConfig()
    itch: ItchConfig = ItchConfig()
    merge: MergeConfig = MergeConfig()
    streaming: StreamingConfig = StreamingConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    categories: dict[str, CategoryConfig] = Field(min_length=1)
    itch_scoring: ItchScoringConfig = ItchScoringConfig()

    @field_validator("categories")
    @classmethod
    def _slug(cls, v: dict[str, CategoryConfig]) -> dict[str, CategoryConfig]:
        import re

        for key in v:
            if not re.fullmatch(r"[a-z0-9_-]+", key):
                raise ValueError(f"カテゴリ名は半角英小文字・数字・_・- のみ: {key!r}")
            if key == "all":
                raise ValueError("カテゴリ名 'all' は「全部」タブ用に予約されています")
        return v

    def steam_tags(self) -> set[str]:
        """SteamSpy から一覧を取得する必要のある全タグ。"""
        tags: set[str] = set()
        for cat in self.categories.values():
            if cat.steam:
                tags |= cat.steam.all_tags()
        return tags


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
