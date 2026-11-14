"""配信可否チェックの実行: 対象選定 → ページ取得 → 判定 → state 更新。

対象は掲載作品のうち、未判定または判定から cache_ttl_days.streaming 日以上たったもの。
スコア順に max_streaming_checks 件まで。残りは次回以降。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Any

from ..config import Config, RetryConfig
from ..http import HttpClient, HttpStatusError, SourceUnavailable
from ..models import SourceStatus
from ..sources import steam_store
from ..state import StateStore, is_expired, iso
from .classify import classify
from .fetch import PageFetcher, candidate_urls, host_excluded, html_to_text

log = logging.getLogger(__name__)

CACHE = "streaming.json"
HISTORY = "verdict_history.jsonl"
ITCH_DESCRIPTION_CLASS = "formatted_description"
# 外部サイトは再試行を控えめに（1サイトの不調で時間を使い切らない）
WEB_RETRY = RetryConfig(max_attempts=2, base_sec=2, max_sec=10)
MAX_CONSECUTIVE_FAILURES = 5


def _steam_appid(item: dict[str, Any]) -> int | None:
    if item["id"].startswith("steam:"):
        return int(item["id"].split(":", 1)[1])
    return None


def gather_pages(
    item: dict[str, Any], cfg: Config, client: HttpClient, fetcher: PageFetcher,
) -> list[tuple[str, str]]:
    """探索範囲のページを集める: Steam 説明文、公式サイト＋決め打ちパス、itch のゲームページ本文。"""
    pages: list[tuple[str, str]] = []
    website = item.get("website")
    appid = _steam_appid(item)
    if appid is not None:
        site, html = steam_store.fetch_store_texts(client, appid, cfg.fetch.steam_interval_sec)
        website = site or website
        if html:
            pages.append((item["url"], html_to_text(html)))
    if website and not host_excluded(website, cfg.streaming.excluded_hosts + ["steampowered.com", "itch.io"]):
        for url in candidate_urls(website, cfg.streaming.paths):
            text = fetcher.get_text(url)
            if text:
                pages.append((url, text))
    itch_url = item["url"] if item["source"] == "itch" else item.get("itch_url")
    if itch_url:
        text = fetcher.get_text(itch_url, only_class=ITCH_DESCRIPTION_CLASS)
        if text:
            pages.append((itch_url, text))
    return pages


def check_item(
    item: dict[str, Any], cfg: Config, client: HttpClient, fetcher: PageFetcher, now: dt.datetime,
) -> dict[str, Any]:
    pages = gather_pages(item, cfg, client, fetcher)
    result = classify(pages)
    return {
        "verdict": result.verdict,
        "method": "auto",
        "checked_at": iso(now),
        "checked_urls": [url for url, _ in pages],
        "evidence": [
            {"url": e.url, "excerpt": e.excerpt, "matched": e.matched} for e in result.evidence
        ],
    }


def run_checks(
    cfg: Config, client: HttpClient, store: StateStore, items: list[dict[str, Any]], now: dt.datetime,
) -> SourceStatus:
    """掲載作品の配信可否を更新し、各 item["streaming"] に反映する。"""
    status = SourceStatus("streaming")
    cache: dict[str, Any] = store.load(CACHE, {})
    ttl = cfg.cache_ttl_days.streaming
    targets = [
        i for i in sorted(items, key=lambda i: (-i["score"], i["id"]))
        if not i.get("stale") and is_expired(cache.get(i["id"], {}).get("checked_at"), ttl, now)
    ]
    web = client.derive(WEB_RETRY, cfg.streaming.page_timeout_sec)
    fetcher = PageFetcher(
        web, interval=cfg.fetch.web_interval_sec,
        excluded_hosts=cfg.streaming.excluded_hosts, max_bytes=cfg.streaming.max_page_bytes,
    )
    checked = failures = 0
    history: list[dict[str, Any]] = []
    for item in targets[: cfg.fetch.max_streaming_checks]:
        try:
            entry = check_item(item, cfg, client, fetcher, now)
        except (SourceUnavailable, HttpStatusError, ValueError) as e:
            failures += 1
            status.partial(f"{item['id']}: {e}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                status.partial(f"連続 {failures}件失敗したため、今回の配信可否チェックを中止")
                break
            continue
        failures = 0
        prev = cache.get(item["id"], {}).get("verdict")
        if prev != entry["verdict"]:
            history.append({"id": item["id"], "at": entry["checked_at"], "from": prev,
                            "to": entry["verdict"], "method": "auto"})
        cache[item["id"]] = entry
        checked += 1

    store.save(CACHE, cache)
    store.append_jsonl(HISTORY, history)
    for item in items:
        if item["id"] in cache:
            item["streaming"] = cache[item["id"]]

    counts = Counter(i["streaming"]["verdict"] for i in items)
    pending = max(0, len(targets) - checked)
    status.count = checked
    status.notes.append(
        f"今回判定 {checked}件 / 未判定・期限切れで次回以降 {pending}件 / "
        f"掲載中: OK {counts['ok']}・条件付き {counts['conditional']}・NG {counts['ng']}・不明 {counts['unknown']}"
    )
    log.info("配信可否: %s", status.notes[-1])
    return status
