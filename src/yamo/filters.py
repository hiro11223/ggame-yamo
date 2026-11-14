"""カテゴリ条件による絞り込み。

prefilter: SteamSpy の情報だけで判定できる条件（タグ・レビュー数・好評率）。appdetails 取得前に件数を絞る。
final_filter: appdetails 取得後の全条件。
どちらも (通過したか, 落ちた理由) を返す。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .config import CategoryConfig, DefaultsConfig


def tag_match(tags: set[str], cat: CategoryConfig) -> tuple[bool, str]:
    if not cat.steam:
        return False, "Steam対象外カテゴリ"
    if not tags & set(cat.steam.required()):
        return False, "必須タグなし"
    if tags & set(cat.steam.exclude):
        return False, "除外タグ"
    return True, ""


def review_match(positive: int, negative: int, cat: CategoryConfig) -> tuple[bool, str]:
    f = cat.filters
    total = positive + negative
    if f.reviews.min is not None and total < f.reviews.min:
        return False, "レビュー数不足"
    if f.reviews.max is not None and total > f.reviews.max:
        return False, "レビュー数過多"
    if f.min_positive_ratio is not None:
        if total == 0 or positive / total < f.min_positive_ratio:
            return False, "好評率不足"
    return True, ""


def prefilter(tags: set[str], positive: int, negative: int, cat: CategoryConfig) -> tuple[bool, str]:
    ok, why = tag_match(tags, cat)
    if not ok:
        return ok, why
    if positive + negative == 0:
        # SteamSpy にレビュー数がない作品は appreviews で後から確認するので通す
        return True, ""
    return review_match(positive, negative, cat)


def final_filter(
    details: dict[str, Any],
    positive: int,
    negative: int,
    cat: CategoryConfig,
    defaults: DefaultsConfig,
) -> tuple[bool, str]:
    f = cat.filters
    if details.get("unavailable"):
        return False, "ストア情報なし"
    if details.get("type") not in (None, "game"):
        return False, f"種別 {details.get('type')}"
    ok, why = review_match(positive, negative, cat)
    if not ok:
        return ok, why
    price = details.get("price_jpy")
    if f.max_price_jpy is not None:
        if price is None:
            return False, "価格不明"
        if price > f.max_price_jpy:
            return False, "価格上限超過"
    if f.require_demo and not details.get("has_demo"):
        return False, "デモなし"
    langs = f.languages_any if f.languages_any is not None else defaults.languages_any
    if langs:
        have = details.get("languages") or {}
        if not any(have.get(lang) for lang in langs):
            return False, "対応言語外"
    if details.get("coming_soon") and not defaults.include_coming_soon:
        return False, "未発売"
    after = f.release_after or defaults.release_after
    rd = details.get("release_date")
    if after and rd:
        try:
            if dt.date.fromisoformat(rd) < after:
                return False, "発売日が古い"
        except ValueError:
            pass
    return True, ""
