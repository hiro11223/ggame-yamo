"""スコア計算（Steam）。

好評率は Wilson 下限で「件数が少ないのに好評率100%」を割り引く。
レビュー数は log10 スケールの釣鐘型で、sweet_spot 付近が最大、多すぎ（実況され尽くし）も少なすぎも減点。
"""

from __future__ import annotations

import datetime as dt
import math

from .config import ItchScoringConfig, WeightsConfig


def wilson_lower(positive: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = positive / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


def volume_score(reviews: int, sweet_spot: float, sigma: float) -> float:
    if reviews <= 0:
        return 0.0
    d = math.log10(reviews) - math.log10(sweet_spot)
    return math.exp(-(d * d) / (2 * sigma * sigma))


def recency_score(release_date: str | None, today: dt.date, half_life_years: float) -> float:
    if not release_date:
        return 0.5
    try:
        released = dt.date.fromisoformat(release_date)
    except ValueError:
        return 0.5
    age_days = max(0, (today - released).days)
    return 0.5 ** (age_days / (365.25 * half_life_years))


def steam_score(
    *,
    positive: int,
    negative: int,
    release_date: str | None,
    tags: set[str],
    weights: WeightsConfig,
    bonus: dict[str, float],
    today: dt.date,
) -> float:
    total = positive + negative
    parts = (
        weights.rating * wilson_lower(positive, total)
        + weights.volume * volume_score(total, weights.volume_sweet_spot, weights.volume_sigma)
        + weights.recency * recency_score(release_date, today, weights.recency_half_life_years)
    )
    base = 100 * parts / (weights.rating + weights.volume + weights.recency)
    extra = sum(v for t, v in bonus.items() if t in tags)
    return round(base + extra, 1)


def itch_score(
    *,
    published: str | None,
    price_jpy: float | None,
    max_price_jpy: float | None,
    tags_matched: list[str],
    category_tags: list[str],
    feeds: list[str],
    weights: ItchScoringConfig,
    today: dt.date,
) -> float:
    """itch 用の別基準スコア: 新しさ・価格・タグ一致度（＋人気/高評価フィードの加点）。"""
    if published:
        age = max(0, (today - dt.date.fromisoformat(published[:10])).days)
        recency = 0.5 ** (age / weights.recency_half_life_days)
    else:
        recency = 0.5
    if price_jpy is None:
        price = 0.5
    elif price_jpy == 0:
        price = 1.0
    else:
        price = max(0.0, 1 - price_jpy / (max_price_jpy or 2000))
    wanted = set(category_tags)
    tag_match = len(wanted & set(tags_matched)) / len(wanted) if wanted else 0.0
    total = weights.recency + weights.price + weights.tag_match
    base = 100 * (weights.recency * recency + weights.price * price + weights.tag_match * tag_match) / total
    bonus = sum(v for k, v in weights.feed_bonus.items() if k in feeds)
    return round(base + bonus, 1)
