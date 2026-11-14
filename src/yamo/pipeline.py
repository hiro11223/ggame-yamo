"""1回の実行: 収集 → 絞り込み → スコア → 出力。"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import filters
from .config import Config
from .http import HttpClient, HttpStatusError, SourceUnavailable
from .models import SourceStatus
from .scoring import steam_score
from .sources import steam_store, steamspy
from .state import StateStore, is_expired, iso, utcnow

log = logging.getLogger(__name__)

ALL_SOURCES = ("steam",)
STEAM_CACHE = "steam_items.json"


@dataclass
class RunResult:
    exit_code: int
    items: list[dict[str, Any]]
    statuses: list[SourceStatus]
    warnings: list[str] = field(default_factory=list)


def steam_id(appid: int) -> str:
    return f"steam:{appid}"


def _validate_steam_tags(client: HttpClient, cfg: Config, warnings: list[str]) -> None:
    wanted = cfg.steam_tags()
    try:
        known = steam_store.fetch_tag_names(client, cfg.fetch.appdetails_interval_sec)
    except (SourceUnavailable, HttpStatusError, ValueError) as e:
        warnings.append(f"Steamタグの実在検証ができませんでした: {e}")
        return
    for tag in sorted(wanted - set(known)):
        warnings.append(f"Steamタグ {tag!r} は存在しません（config.yaml を確認してください）")


def _prefilter(cfg: Config, apps: dict[int, steamspy.SpyApp], today: dt.date) -> dict[int, tuple[list[str], float]]:
    """SteamSpy の情報だけで事前フィルタし、仮スコア（取得の優先度）をつける。"""
    candidates: dict[int, tuple[list[str], float]] = {}
    for key, cat in cfg.categories.items():
        if not cat.steam:
            continue
        reasons: Counter = Counter()
        for app in apps.values():
            ok, why = filters.prefilter(app.tags, app.positive, app.negative, cat)
            if not ok:
                reasons[why] += 1
                continue
            pre = steam_score(
                positive=app.positive, negative=app.negative, release_date=None,
                tags=app.tags, weights=cat.weights, bonus=cat.steam.bonus, today=today,
            )
            cats, best = candidates.get(app.appid, ([], 0.0))
            candidates[app.appid] = (cats + [key], max(best, pre))
        log.info("事前フィルタ[%s]: 除外 %s", key, dict(reasons))
    log.info("事前フィルタ通過: %d件", len(candidates))
    return candidates


def _fetch_store_items(
    cfg: Config, client: HttpClient, cache: dict[str, Any], need: list[int],
    max_new: int, now: dt.datetime, status: SourceStatus,
) -> None:
    """GetItems でバッチ取得してキャッシュに書き込む。"""
    batch_size = cfg.fetch.getitems_batch_size
    targets = need[:max_new]
    fetched = 0
    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        try:
            result = steam_store.fetch_items(client, batch, cfg.fetch.appdetails_interval_sec, now)
        except SourceUnavailable as e:
            status.partial(f"GetItems: {e}")
            break
        except ValueError as e:
            status.partial(f"GetItems: JSON以外の応答のため今回は中止 ({e})")
            break
        except HttpStatusError as e:
            status.partial(f"GetItems: {e}")
            continue
        for appid, d in result.items():
            prev = cache.get(steam_id(appid), {})
            # 公式サイトURL（appdetails で別取得）は引き継ぐ
            for k in ("website", "website_fetched_at"):
                if k in prev:
                    d[k] = prev[k]
            d["fetched_at"] = iso(now)
            cache[steam_id(appid)] = d
        fetched += len(batch)
    pending = sum(1 for a in need if steam_id(a) not in cache)
    status.notes.append(f"GetItems 取得 {fetched}件 / 未取得(次回以降) {pending}件")
    log.info("GetItems: 取得 %d件, 次回以降 %d件", fetched, pending)


def _fetch_websites(
    cfg: Config, client: HttpClient, cache: dict[str, Any], appids: list[int],
    max_new: int, now: dt.datetime, status: SourceStatus,
) -> None:
    """掲載作品の公式サイトURLを appdetails で取得（GetItems では取れない）。"""
    ttl = cfg.cache_ttl_days.appdetails
    budget = max_new
    for appid in appids:
        entry = cache[steam_id(appid)]
        if not is_expired(entry.get("website_fetched_at"), ttl, now):
            continue
        if budget <= 0:
            break
        budget -= 1
        try:
            entry["website"] = steam_store.fetch_website(client, appid, cfg.fetch.appdetails_interval_sec)
        except SourceUnavailable as e:
            status.partial(f"appdetails: {e}")
            break
        except ValueError as e:
            status.partial(f"appdetails: JSON以外の応答のため今回は中止 ({e})")
            break
        except HttpStatusError as e:
            status.partial(f"appdetails {appid}: {e}")
            continue
        entry["website_fetched_at"] = iso(now)


def _build_item(appid: int, d: dict[str, Any], app: steamspy.SpyApp, scores: dict[str, float]) -> dict[str, Any]:
    pos, neg = d["positive"], d["negative"]
    total = pos + neg
    return {
        "id": steam_id(appid),
        "source": "steam",
        "title": d.get("name") or app.name,
        "url": f"https://store.steampowered.com/app/{appid}/",
        "thumbnail": d.get("header_image"),
        "price_jpy": d.get("price_jpy"),
        "positive": pos,
        "negative": neg,
        "reviews": total,
        "positive_ratio": round(pos / total, 4) if total else None,
        "tags": sorted(app.tags),
        "has_demo": d.get("has_demo", False),
        "early_access": d.get("early_access", False),
        "languages": sorted(k for k, v in (d.get("languages") or {}).items() if v),
        "release_date": d.get("release_date"),
        "developers": d.get("developers") or ([app.developer] if app.developer else []),
        "website": d.get("website"),
        "description": d.get("short_description") or "",
        "categories": scores,
        "score": max(scores.values()),
        "stale": False,
    }


def _run_steam(
    cfg: Config,
    client: HttpClient,
    store: StateStore,
    now: dt.datetime,
    max_new: int,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[SourceStatus]]:
    _validate_steam_tags(client, cfg, warnings)

    apps, spy_status, empty = steamspy.collect(
        client, sorted(cfg.steam_tags()), cfg.fetch.steamspy_list_interval_sec
    )
    for tag in empty:
        warnings.append(f"SteamSpy: タグ {tag!r} の一覧が0件でした")
    store_status = SourceStatus("steam_store")
    if spy_status.status == "failed":
        store_status.status = "skipped"
        store_status.notes.append("SteamSpy 失敗のため未実行")
        return [], [spy_status, store_status]

    today = now.date()
    candidates = _prefilter(cfg, apps, today)

    # GetItems: 新規・期限切れのみ、仮スコア順に上限まで
    cache: dict[str, Any] = store.load(STEAM_CACHE, {})
    order = sorted(candidates, key=lambda a: (-candidates[a][1], a))
    ttl = cfg.cache_ttl_days.appdetails
    need = [a for a in order if is_expired(cache.get(steam_id(a), {}).get("fetched_at"), ttl, now)]
    _fetch_store_items(cfg, client, cache, need, max_new, now, store_status)

    # 本フィルタとスコア（レビュー数は GetItems の値を使う）
    passed: list[tuple[int, dict[str, float]]] = []
    reasons: Counter = Counter()
    for appid in order:
        d = cache.get(steam_id(appid))
        if not d:
            continue
        scores: dict[str, float] = {}
        for key in candidates[appid][0]:
            cat = cfg.categories[key]
            ok, why = filters.final_filter(d, d.get("positive", 0), d.get("negative", 0), cat, cfg.defaults)
            if not ok:
                reasons[why] += 1
                continue
            scores[key] = steam_score(
                positive=d["positive"], negative=d["negative"], release_date=d.get("release_date"),
                tags=apps[appid].tags, weights=cat.weights, bonus=cat.steam.bonus, today=today,
            )
        if scores:
            passed.append((appid, scores))
    log.info("本フィルタ: 除外 %s / 掲載 %d件", dict(reasons), len(passed))

    _fetch_websites(cfg, client, cache, [a for a, _ in passed], max_new, now, store_status)
    store.save(STEAM_CACHE, cache)

    items = [_build_item(a, cache[steam_id(a)], apps[a], s) for a, s in passed]
    store_status.count = len(items)
    return items, [spy_status, store_status]


def run(
    cfg: Config,
    client: HttpClient,
    state_dir: str | Path,
    out_dir: str | Path,
    *,
    now: dt.datetime | None = None,
    max_new: int | None = None,
    sources: set[str] | None = None,
) -> RunResult:
    now = now or utcnow()
    sources = sources or set(ALL_SOURCES)
    store = StateStore(state_dir)
    out = StateStore(out_dir)
    last_output = store.load("last_output.json", {"items": []})
    warnings: list[str] = []
    statuses: list[SourceStatus] = []
    items: list[dict[str, Any]] = []

    if "steam" in sources:
        steam_items, st = _run_steam(
            cfg, client, store, now,
            cfg.fetch.max_new_fetch if max_new is None else max_new, warnings,
        )
        statuses += st
        if st[0].status == "failed":
            # 一覧が取れなかった: 前回の Steam 作品を「前回データ」として残す
            prev = [dict(i, stale=True) for i in last_output.get("items", []) if i.get("source") == "steam"]
            warnings.append(f"Steam の取得に失敗したため、前回データ {len(prev)}件を表示しています")
            items += prev
        else:
            items += steam_items

    items.sort(key=lambda i: (-i["score"], i["id"]))
    for w in warnings:
        log.warning(w)

    active = [s for s in statuses if s.status != "skipped"]
    exit_code = 1 if active and all(s.status == "failed" for s in active) else 0

    payload = {
        "generated_at": iso(now),
        "categories": {k: c.label for k, c in cfg.categories.items()},
        "sources": [s.to_dict() for s in statuses],
        "warnings": warnings,
        "items": items,
    }
    out.save("data.json", payload)
    if exit_code == 0:
        store.save("last_output.json", {"generated_at": iso(now), "items": items})
    store.save("run_status.json", {
        "generated_at": iso(now),
        "exit_code": exit_code,
        "sources": payload["sources"],
        "warnings": warnings,
    })
    return RunResult(exit_code, items, statuses, warnings)
