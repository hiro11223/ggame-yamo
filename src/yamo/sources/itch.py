"""itch.io: タグ別RSS（新着 / 人気 / 高評価）から作品を集める。HTMLスクレイピングはしない。

RSS の1ページは36件。`?page=N` で続きを取れる。存在しないタグは 200 で0件が返る。
作者名は RSS にないので、作品URL（https://<作者>.itch.io/<スラッグ>）のサブドメインを使う。
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from defusedxml import ElementTree as ET

from ..http import HttpClient, HttpStatusError, SourceUnavailable
from ..models import SourceStatus

log = logging.getLogger(__name__)

RATE_KEY = "itch"
FEED_PATHS = {
    "newest": "https://itch.io/games/newest/tag-{tag}.xml",
    "popular": "https://itch.io/games/tag-{tag}.xml",
    "top_rated": "https://itch.io/games/top-rated/tag-{tag}.xml",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class FeedItem:
    id: str
    url: str
    author: str
    title: str
    image: str | None
    price: float | None
    currency: str | None
    description: str
    published: str | None
    updated: str | None
    platforms: list[str]


def item_id(url: str) -> tuple[str, str] | None:
    """作品URL → (ID, 作者)。itch.io の作品URLでなければ None。"""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if not host.endswith(".itch.io") or host == "www.itch.io":
        return None
    author = host[: -len(".itch.io")]
    slug = u.path.strip("/").split("/")[0] if u.path.strip("/") else ""
    if not author or not slug:
        return None
    return f"itch:{author}/{slug}", author


def parse_price(text: str | None) -> float | None:
    """'$4.99' / '€1,50' / '¥500' / '1.234,50 €' などを数値に。"""
    if not text:
        return None
    s = re.sub(r"[^\d.,]", "", text)
    if not s:
        return None
    if "," in s and "." in s:
        # 後ろにある方を小数点とみなす
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(el, tag: str) -> str | None:
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else None


def clean_description(raw: str | None, limit: int = 300) -> str:
    text = html.unescape(_TAG_RE.sub(" ", raw or ""))
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def parse_feed(xml_text: str) -> list[FeedItem]:
    root = ET.fromstring(xml_text)
    items: list[FeedItem] = []
    for el in root.iter("item"):
        url = _text(el, "link") or _text(el, "guid") or ""
        ident = item_id(url)
        if not ident:
            continue
        platforms_el = el.find("platforms")
        platforms = sorted(
            c.tag for c in (platforms_el if platforms_el is not None else []) if (c.text or "").strip() == "yes"
        )
        items.append(FeedItem(
            id=ident[0],
            url=url,
            author=ident[1],
            title=_text(el, "plainTitle") or _text(el, "title") or "",
            image=_text(el, "imageurl"),
            price=parse_price(_text(el, "price")),
            currency=(_text(el, "currency") or "").upper() or None,
            description=clean_description(_text(el, "description")),
            published=_date(_text(el, "createDate") or _text(el, "pubDate")),
            updated=_date(_text(el, "updateDate")),
            platforms=platforms,
        ))
    return items


def fetch_feed(client: HttpClient, kind: str, tag: str, page: int, interval: float) -> list[FeedItem]:
    url = FEED_PATHS[kind].format(tag=tag)
    params = {"page": page} if page > 1 else None
    resp = client.get(url, params=params, rate_key=RATE_KEY, interval=interval)
    return parse_feed(resp.text)


def collect(
    client: HttpClient,
    tags: list[str],
    feeds: list[str],
    pages: int,
    interval: float,
) -> tuple[dict[str, tuple[FeedItem, set[str], set[str]]], SourceStatus, list[str]]:
    """全タグ×全フィード×ページを取得。

    戻り値: (ID→(作品, 見つかったタグ, 見つかったフィード), ステータス, 0件だったタグ)
    """
    status = SourceStatus("itch")
    found: dict[str, tuple[FeedItem, set[str], set[str]]] = {}
    tag_counts = {t: 0 for t in tags}
    ok = attempted = 0
    down = False
    for tag in tags:
        for kind in feeds:
            for page in range(1, pages + 1):
                if down:
                    break
                attempted += 1
                try:
                    items = fetch_feed(client, kind, tag, page, interval)
                except SourceUnavailable as e:
                    status.partial(f"{tag}/{kind}: {e}")
                    down = True  # itch 自体が落ちている。残りは試さない
                    break
                except (HttpStatusError, ET.ParseError, ValueError) as e:
                    status.partial(f"{tag}/{kind}/p{page}: {e}")
                    continue
                ok += 1
                tag_counts[tag] += len(items)
                for it in items:
                    prev = found.get(it.id)
                    if prev:
                        prev[1].add(tag)
                        prev[2].add(kind)
                    else:
                        found[it.id] = (it, {tag}, {kind})
                if len(items) == 0:
                    break  # 続きのページもない
    if attempted and ok == 0:
        status.status = "failed"
    empty = [t for t, n in tag_counts.items() if n == 0] if ok else []
    log.info("itch: %d件（%d/%d フィード取得）", len(found), ok, attempted)
    return found, status, empty


def to_record(item: FeedItem, tags: set[str], feeds: set[str], to_jpy: dict[str, float]) -> dict[str, Any]:
    rate = to_jpy.get(item.currency or "")
    price_jpy = None
    if item.price is not None and rate is not None:
        price_jpy = round(item.price * rate, 2)
    return {
        "title": item.title,
        "url": item.url,
        "author": item.author,
        "image": item.image,
        "price": item.price,
        "currency": item.currency,
        "price_jpy": price_jpy,
        "description": item.description,
        "published": item.published,
        "updated": item.updated,
        "platforms": item.platforms,
        "tags_matched": sorted(tags),
        "feeds": sorted(feeds),
    }
