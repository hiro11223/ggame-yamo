"""Steam ストアAPI。

- IStoreBrowseService/GetItems（キー不要・最大100件程度をまとめて取得）:
  名前、価格、対応言語、デモ、発売日、レビュー、開発者、説明文、ヘッダー画像
- appdetails（1件ずつ）: 公式サイトURL のみ（GetItems の links は SNS しか含まないため）
- タグ一覧（実在検証用）

appreviews は 2026-10 頃からストアトップへ 302 されるようになったため廃止。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..http import HttpClient

GETITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
TAGLIST_URL = "https://store.steampowered.com/tagdata/populartags/english"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"

RATE_KEY = "steam_store"

# Steam の ELanguage（GetItems の supported_languages[].elanguage）
ELANGUAGE = {"english": 0, "japanese": 10}
# GetItems の type（実データで確認済みのもの）
ITEM_TYPES = {0: "game", 1: "demo", 11: "music"}


def fetch_tag_names(client: HttpClient, interval: float) -> dict[str, int]:
    """タグ名→tagid。Steam のデータには末尾に空白の付いた名前（例 'Parody '）があるので strip する。"""
    resp = client.get(TAGLIST_URL, rate_key=RATE_KEY, interval=interval)
    return {
        str(t["name"]).strip(): int(t["tagid"])
        for t in resp.json()
        if isinstance(t, dict) and "name" in t and "tagid" in t
    }


# ---- GetItems -----------------------------------------------------------

DATA_REQUEST = {
    "include_basic_info": True,
    "include_release": True,
    "include_reviews": True,
    "include_supported_languages": True,
    "include_assets": True,
}


def _release(rel: dict[str, Any], now: dt.datetime) -> tuple[str | None, bool]:
    ts = rel.get("steam_release_date") or rel.get("original_release_date")
    if rel.get("is_coming_soon"):
        return (_ts_date(ts) if ts else None), True
    if not ts:
        return None, True  # 発売日がない＝未発売とみなす
    return _ts_date(ts), dt.datetime.fromtimestamp(int(ts), dt.timezone.utc) > now


def _ts_date(ts: int | str) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()


def reviews_from_summary(summary: dict[str, Any] | None) -> tuple[int, int]:
    """summary_filtered → (好評数, 不評数)。好評数は percent_positive×review_count から推定（丸め誤差は許容）。

    percent_positive=0 かつ review_count=0 はレビューなし。
    """
    if not summary:
        return 0, 0
    count = int(summary.get("review_count") or 0)
    pct = int(summary.get("percent_positive") or 0)
    if count <= 0:
        return 0, 0
    pos = round(count * pct / 100)
    return pos, count - pos


def parse_store_item(item: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    if item.get("success") != 1:
        return {"unavailable": True}
    langs = {
        lang["elanguage"]
        for lang in item.get("supported_languages") or []
        if lang.get("supported")
    }
    bpo = item.get("best_purchase_option") or {}
    if item.get("is_free"):
        price: float | None = 0.0
    elif bpo.get("final_price_in_cents") is not None:
        # country_code=JP なので円。単位は 1/100
        price = int(bpo["final_price_in_cents"]) / 100
    else:
        price = None
    release_date, coming_soon = _release(item.get("release") or {}, now)
    assets = item.get("assets") or {}
    header = None
    if assets.get("asset_url_format") and assets.get("header"):
        header = ASSET_BASE + assets["asset_url_format"].replace("${FILENAME}", assets["header"])
    basic = item.get("basic_info") or {}
    pos, neg = reviews_from_summary((item.get("reviews") or {}).get("summary_filtered"))
    return {
        "unavailable": False,
        "type": ITEM_TYPES.get(item.get("type"), f"type{item.get('type')}"),
        "name": item.get("name"),
        "is_free": bool(item.get("is_free")),
        "price_jpy": price,
        "languages": {name: code in langs for name, code in ELANGUAGE.items()},
        "has_demo": bool((item.get("related_items") or {}).get("demo_appid")),
        "release_date": release_date,
        "coming_soon": coming_soon,
        "early_access": bool(item.get("is_early_access")),
        "header_image": header,
        "developers": [d["name"] for d in basic.get("developers") or [] if d.get("name")],
        "short_description": basic.get("short_description") or "",
        "positive": pos,
        "negative": neg,
    }


def fetch_items(
    client: HttpClient, appids: list[int], interval: float, now: dt.datetime
) -> dict[int, dict[str, Any]]:
    """GetItems で複数作品をまとめて取得する。返らなかった appid は unavailable 扱い。"""
    payload = {
        "ids": [{"appid": a} for a in appids],
        "context": {"language": "english", "country_code": "JP"},
        "data_request": DATA_REQUEST,
    }
    resp = client.get(
        GETITEMS_URL,
        params={"input_json": json.dumps(payload, separators=(",", ":"))},
        rate_key=RATE_KEY,
        interval=interval,
    )
    items = (resp.json().get("response") or {}).get("store_items") or []
    out: dict[int, dict[str, Any]] = {}
    for item in items:
        # 存在しない appid は appid=0 で返るので、id を使う
        appid = int(item.get("id") or item.get("appid") or 0)
        if appid:
            out[appid] = parse_store_item(item, now)
    for a in appids:
        out.setdefault(a, {"unavailable": True})
    return out


# ---- appdetails（公式サイトURL のみ） -----------------------------------


def parse_website(appid: int, payload: dict[str, Any]) -> str | None:
    entry = payload.get(str(appid)) or {}
    if not entry.get("success"):
        return None
    return (entry.get("data") or {}).get("website") or None


def fetch_website(client: HttpClient, appid: int, interval: float) -> str | None:
    resp = client.get(
        APPDETAILS_URL,
        params={"appids": appid, "cc": "jp", "l": "english"},
        rate_key=RATE_KEY,
        interval=interval,
    )
    return parse_website(appid, resp.json() or {})
