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
    for tag in sorted(wanted - known):
        warnings.append(f"Steamタグ {tag!r} は存在しません（config.yaml を確認してください）")


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
    to_jpy = cfg.currency.to_jpy
    interval = cfg.fetch.appdetails_interval_sec
    ttl = cfg.cache_ttl_days.appdetails

    # 1) SteamSpy の情報だけで事前フィルタし、仮スコアで優先度をつける
    candidates: dict[int, tuple[list[str], float]] = {}
    pre_reasons: dict[str, Counter] = {}
    for key, cat in cfg.categories.items():
        if not cat.steam:
            continue
        reasons = pre_reasons.setdefault(key, Counter())
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
    for key, reasons in pre_reasons.items():
        log.info("事前フィルタ[%s]: 除外 %s", key, dict(reasons))
    log.info("事前フィルタ通過: %d件", len(candidates))

    # 2) appdetails: 新規・期限切れのみ、仮スコア順に上限まで
    details_cache: dict[str, Any] = store.load("appdetails.json", {})
    reviews_cache: dict[str, Any] = store.load("reviews.json", {})
    order = sorted(candidates, key=lambda a: (-candidates[a][1], a))
    need = [a for a in order if is_expired(details_cache.get(steam_id(a), {}).get("fetched_at"), ttl, now)]
    budget = max_new
    fetched = 0
    source_down = False
    for appid in need:
        if budget <= 0:
            break
        try:
            d = steam_store.fetch_appdetails(client, appid, interval, to_jpy)
        except SourceUnavailable as e:
            store_status.partial(f"appdetails: {e}")
            source_down = True
            break
        except (HttpStatusError, ValueError) as e:
            store_status.partial(f"appdetails {appid}: {e}")
            budget -= 1
            continue
        d["fetched_at"] = iso(now)
        details_cache[steam_id(appid)] = d
        fetched += 1
        budget -= 1
    pending = sum(1 for a in need if steam_id(a) not in details_cache)
    store_status.notes.append(f"appdetails 取得 {fetched}件 / 未取得(次回以降) {pending}件")
    log.info("appdetails: 取得 %d件, 次回以降 %d件", fetched, pending)

    # 3) appreviews: SteamSpy にレビュー数がない作品だけ。
    #    appdetails とは別枠の上限にする（共有すると、詳細だけ取れて
    #    レビュー数0のまま本フィルタで落ちる作品が出るため）
    budget = max_new
    if not source_down:
        for appid in order:
            if budget <= 0:
                break
            app = apps[appid]
            sid = steam_id(appid)
            if app.reviews > 0 or sid not in details_cache:
                continue
            if not is_expired(reviews_cache.get(sid, {}).get("fetched_at"), ttl, now):
                continue
            try:
                r = steam_store.fetch_appreviews(client, appid, interval)
            except SourceUnavailable as e:
                store_status.partial(f"appreviews: {e}")
                break
            except ValueError as e:
                # JSON 以外（トップページへのリダイレクト等）＝ API 自体が使えない。以降は呼ばない
                store_status.partial(f"appreviews: JSON以外の応答のため今回は中止 ({e})")
                break
            except HttpStatusError as e:
                store_status.partial(f"appreviews {appid}: {e}")
                budget -= 1
                continue
            budget -= 1
            if r is not None:
                reviews_cache[sid] = {**r, "fetched_at": iso(now)}

    store.save("appdetails.json", details_cache)
    store.save("reviews.json", reviews_cache)

    # 4) 本フィルタとスコア
    items: list[dict[str, Any]] = []
    final_reasons: Counter = Counter()
    for appid in order:
        sid = steam_id(appid)
        d = details_cache.get(sid)
        if not d:
            continue
        app = apps[appid]
        pos, neg = app.positive, app.negative
        if pos + neg == 0 and sid in reviews_cache:
            pos, neg = reviews_cache[sid]["positive"], reviews_cache[sid]["negative"]
        scores: dict[str, float] = {}
        for key in candidates[appid][0]:
            cat = cfg.categories[key]
            ok, why = filters.final_filter(d, pos, neg, cat, cfg.defaults)
            if not ok:
                final_reasons[why] += 1
                continue
            scores[key] = steam_score(
                positive=pos, negative=neg, release_date=d.get("release_date"),
                tags=app.tags, weights=cat.weights, bonus=cat.steam.bonus, today=today,
            )
        if not scores:
            continue
        total = pos + neg
        items.append({
            "id": sid,
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
            "languages": sorted(k for k, v in (d.get("languages") or {}).items() if v),
            "release_date": d.get("release_date"),
            "developers": d.get("developers") or ([app.developer] if app.developer else []),
            "website": d.get("website"),
            "description": d.get("short_description") or "",
            "categories": scores,
            "score": max(scores.values()),
            "stale": False,
        })
    log.info("本フィルタ: 除外 %s / 掲載 %d件", dict(final_reasons), len(items))
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
