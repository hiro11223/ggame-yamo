"""Steam ストアAPI: appdetails / appreviews / タグ一覧（実在検証用）。"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ..http import HttpClient

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
TAGLIST_URL = "https://store.steampowered.com/tagdata/populartags/english"

RATE_KEY = "steam_store"


def fetch_tag_names(client: HttpClient, interval: float) -> set[str]:
    resp = client.get(TAGLIST_URL, rate_key=RATE_KEY, interval=interval)
    return {str(t["name"]) for t in resp.json() if isinstance(t, dict) and "name" in t}


# ---- appdetails ---------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def parse_languages(html: str | None) -> dict[str, bool]:
    """supported_languages（l=english で取得したHTML断片）から日本語/英語の対応を判定。"""
    if not html:
        return {"japanese": False, "english": False}
    text = _TAG_RE.sub(",", html)
    names = {p.strip(" *\t\r\n").lower() for p in re.split(r"[,\n]", text)}
    return {"japanese": "japanese" in names, "english": "english" in names}


_DATE_FORMATS = ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%B %Y")


def parse_release_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    if re.fullmatch(r"\d{4}", s):
        return f"{s}-01-01"
    return None


def _price_jpy(data: dict[str, Any], to_jpy: dict[str, float]) -> float | None:
    if data.get("is_free"):
        return 0.0
    po = data.get("price_overview")
    if not po:
        return None  # 価格未設定（未発売など）
    rate = to_jpy.get(str(po.get("currency", "")).upper())
    if rate is None or po.get("final") is None:
        return None
    # Steam の価格は通貨単位×100（JPY も同様）
    return round(po["final"] / 100 * rate, 2)


def parse_appdetails(appid: int, payload: dict[str, Any], to_jpy: dict[str, float]) -> dict[str, Any]:
    """appdetails のレスポンスを、キャッシュに保存する最小限のフィールドに変換する。"""
    entry = payload.get(str(appid)) or {}
    if not entry.get("success"):
        return {"unavailable": True}
    data = entry.get("data") or {}
    rd = data.get("release_date") or {}
    return {
        "unavailable": False,
        "type": data.get("type"),
        "name": data.get("name"),
        "is_free": bool(data.get("is_free")),
        "price_jpy": _price_jpy(data, to_jpy),
        "languages": parse_languages(data.get("supported_languages")),
        "has_demo": bool(data.get("demos")),
        "release_date": parse_release_date(rd.get("date")),
        "coming_soon": bool(rd.get("coming_soon")),
        "website": data.get("website") or None,
        "header_image": data.get("header_image"),
        "developers": list(data.get("developers") or []),
        "short_description": data.get("short_description") or "",
    }


def fetch_appdetails(
    client: HttpClient, appid: int, interval: float, to_jpy: dict[str, float]
) -> dict[str, Any]:
    resp = client.get(
        APPDETAILS_URL,
        params={"appids": appid, "cc": "jp", "l": "english"},
        rate_key=RATE_KEY,
        interval=interval,
    )
    return parse_appdetails(appid, resp.json() or {}, to_jpy)


# ---- appreviews ---------------------------------------------------------


def parse_appreviews(payload: dict[str, Any]) -> dict[str, int] | None:
    if not payload.get("success"):
        return None
    qs = payload.get("query_summary") or {}
    return {
        "positive": int(qs.get("total_positive", 0)),
        "negative": int(qs.get("total_negative", 0)),
    }


def fetch_appreviews(client: HttpClient, appid: int, interval: float) -> dict[str, int] | None:
    resp = client.get(
        APPREVIEWS_URL.format(appid=appid),
        params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
        rate_key=RATE_KEY,
        interval=interval,
    )
    return parse_appreviews(resp.json() or {})
