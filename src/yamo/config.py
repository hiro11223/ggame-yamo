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
    steam_interval_sec: float = Field(2.0, ge=1.0)  # Steam API（StoreQuery / GetItems / appdetails）
    query_page_size: int = Field(1000, ge=1, le=1000)  # StoreQuery 1回の件数（API上限 1000）
    query_max_items: int = Field(50000, ge=1)  # 1カテゴリで取得する最大件数（暴走防止）
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


FeedKind = Literal["newest", "popular", "top_rated"]


class ItchConfig(_Strict):
    retention_days: int = Field(120, ge=1)  # 最後に RSS で見かけてからこの日数で捨てる
    feeds: list[FeedKind] = ["newest", "popular", "top_rated"]
    pages: int = Field(1, ge=1, le=5)  # フィードごとの取得ページ数（1ページ36件）
    max_items: int = Field(100, ge=1)  # カテゴリごとに掲載する itch 作品の上限（Steam とは別枠）


class MergeConfig(_Strict):
    developer_similarity: float = Field(0.8, ge=0, le=1)


class StreamingConfig(_Strict):
    # 公式サイト巡回から除外するホスト（サブドメイン含む）
    excluded_hosts: list[str] = []
    # 公式サイトのトップに加えて見るパス
    paths: list[str] = ["/press", "/presskit", "/press-kit", "/streaming", "/faq"]
    max_page_bytes: int = Field(1_000_000, ge=10_000)
    page_timeout_sec: float = Field(10, gt=0)


class DefaultsConfig(_Strict):
    languages_any: list[str] = ["japanese", "english"]
    release_after: dt.date | None = None
    include_coming_soon: bool = False


class SteamTagsConfig(_Strict):
    # どれか1つを持つ作品を StoreQuery で収集する（OR）
    require_any: list[str] = Field(min_length=1)
    # 1つでも持つ作品を除外する（StoreQuery 側で除外）
    exclude: list[str] = []
    # 持っていれば加点（作品の上位20タグで判定）
    bonus: dict[str, float] = {}

    def all_tags(self) -> set[str]:
        return set(self.require_any) | set(self.exclude) | set(self.bonus)


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
    # スコア上位この件数までを掲載（公式サイトURL・配信可否の取得もこの範囲のみ）。null で無制限
    max_items: int | None = Field(300, ge=1)


class ItchScoringConfig(_Strict):
    recency: float = Field(0.5, ge=0)
    price: float = Field(0.2, ge=0)
    tag_match: float = Field(0.3, ge=0)
    recency_half_life_days: float = Field(180, gt=0)
    # 人気・高評価フィードに載っていれば加点（itch には評価数がないため、品質の目安として使う）
    feed_bonus: dict[FeedKind, float] = {"top_rated": 10, "popular": 5}

    @model_validator(mode="after")
    def _nonzero(self) -> "ItchScoringConfig":
        if self.recency + self.price + self.tag_match <= 0:
            raise ValueError("itch_scoring の recency/price/tag_match の合計が 0 です")
        return self


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

    def itch_tags(self) -> list[str]:
        tags: set[str] = set()
        for cat in self.categories.values():
            if cat.itch:
                tags |= set(cat.itch.tags)
        return sorted(tags)

    def steam_tags(self) -> set[str]:
        """config で使っている全 Steam タグ（実在検証の対象）。"""
        tags: set[str] = set()
        for cat in self.categories.values():
            if cat.steam:
                tags |= cat.steam.all_tags()
        return tags


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
