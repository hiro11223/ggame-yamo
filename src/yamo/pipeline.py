"""1回の実行: 収集 → 絞り込み → スコア → 出力。"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import filters, merge, render
from .config import CategoryConfig, Config
from .http import HttpClient, HttpStatusError, SourceUnavailable
from .models import SourceStatus
from .scoring import itch_score, steam_score
from .sources import itch, steam_store
from .state import StateStore, is_expired, iso, utcnow
from .streaming import runner as streaming

log = logging.getLogger(__name__)

ALL_SOURCES = ("steam", "itch")
WEBSITE_CACHE = "steam_websites.json"
ITCH_CACHE = "itch_items.json"

FETCH_ERRORS = (SourceUnavailable, HttpStatusError, ValueError)


@dataclass
class RunResult:
    exit_code: int
    items: list[dict[str, Any]]
    statuses: list[SourceStatus]
    warnings: list[str] = field(default_factory=list)


def steam_id(appid: int) -> str:
    return f"steam:{appid}"


def fetch_steam_tag_ids(client: HttpClient, cfg: Config, warnings: list[str]) -> dict[str, int]:
    """タグ名→ID を取得し、config のタグの実在を検証する。取得失敗時は例外をそのまま上げる。"""
    tag_ids = steam_store.fetch_tag_names(client, cfg.fetch.steam_interval_sec)
    for tag in sorted(cfg.steam_tags() - set(tag_ids)):
        warnings.append(f"Steamタグ {tag!r} は存在しません（config.yaml を確認してください）")
    return tag_ids


def _query_category(
    cfg: Config, client: HttpClient, key: str, cat: CategoryConfig,
    tag_ids: dict[str, int], now: dt.datetime, status: SourceStatus,
) -> dict[int, dict[str, Any]] | None:
    """1カテゴリ分を StoreQuery で全件取得する。1ページも取れなければ None。"""
    assert cat.steam
    any_ids = [tag_ids[t] for t in cat.steam.require_any if t in tag_ids]
    if not any_ids:
        status.partial(f"{key}: 実在する必須タグがないため収集できません")
        return None
    exclude_ids = [tag_ids[t] for t in cat.steam.exclude if t in tag_ids]
    found: dict[int, dict[str, Any]] = {}
    start, total, pages = 0, None, 0
    limit = cfg.fetch.query_max_items
    while (total is None or start < total) and start < limit:
        try:
            total, items = steam_store.query_page(
                client, tagids_any=any_ids, exclude=exclude_ids,
                released_only=not cfg.defaults.include_coming_soon,
                start=start, count=min(cfg.fetch.query_page_size, limit - start),
                interval=cfg.fetch.steam_interval_sec, now=now,
            )
        except FETCH_ERRORS as e:
            status.partial(f"{key}: StoreQuery {start}件目以降を取得できませんでした: {e}")
            break
        pages += 1
        if not items:
            break
        found.update(items)
        start += cfg.fetch.query_page_size
    if total is not None and total > limit:
        status.notes.append(f"{key}: 総件数 {total} のうち上限 {limit} 件まで取得")
    log.info("StoreQuery[%s]: %d件取得（総件数 %s、%dページ）", key, len(found), total, pages)
    return found if pages else None


def _keep_top(
    cfg: Config, passed: list[tuple[int, list[str], dict[str, float]]], status: SourceStatus,
) -> list[tuple[int, list[str], dict[str, float]]]:
    """カテゴリごとにスコア上位 max_items 件だけ残す。

    複数カテゴリに入る作品は、上位に入ったカテゴリだけを残す（どれにも入らなければ掲載しない）。
    """
    keep: dict[str, set[int]] = {}
    for key, cat in cfg.categories.items():
        ranked = sorted((-s[key], a) for a, _, s in passed if key in s)
        limit = cat.max_items if cat.max_items is not None else len(ranked)
        keep[key] = {a for _, a in ranked[:limit]}
        status.notes.append(f"{key}: 条件通過 {len(ranked)}件 → 掲載 {len(keep[key])}件")
    out = []
    for appid, tags, scores in passed:
        kept = {k: v for k, v in scores.items() if appid in keep.get(k, set())}
        if kept:
            out.append((appid, tags, kept))
    return out


def _fetch_websites(
    cfg: Config, client: HttpClient, store: StateStore, appids: list[int],
    max_new: int, now: dt.datetime, status: SourceStatus,
) -> dict[str, Any]:
    """掲載作品の公式サイトURLを appdetails で取得（StoreItem には含まれない）。スコア順に上限まで。"""
    cache: dict[str, Any] = store.load(WEBSITE_CACHE, {})
    ttl = cfg.cache_ttl_days.appdetails
    budget, fetched = max_new, 0
    for appid in appids:
        sid = steam_id(appid)
        if not is_expired(cache.get(sid, {}).get("fetched_at"), ttl, now):
            continue
        if budget <= 0:
            break
        budget -= 1
        try:
            website = steam_store.fetch_website(client, appid, cfg.fetch.steam_interval_sec)
        except SourceUnavailable as e:
            status.partial(f"appdetails: {e}")
            break
        except ValueError as e:
            status.partial(f"appdetails: JSON以外の応答のため今回は中止 ({e})")
            break
        except HttpStatusError as e:
            status.partial(f"appdetails {appid}: {e}")
            continue
        cache[sid] = {"website": website, "fetched_at": iso(now)}
        fetched += 1
    pending = sum(1 for a in appids if steam_id(a) not in cache)
    status.notes.append(f"公式サイトURL 取得 {fetched}件 / 未取得(次回以降) {pending}件")
    store.save(WEBSITE_CACHE, cache)
    return cache


def _fetch_descriptions_ja(
    cfg: Config, client: HttpClient, appids: list[int], status: SourceStatus,
) -> dict[int, str]:
    """掲載作品の日本語説明文を GetItems でまとめて取得（失敗しても英語で表示できるので致命的ではない）。"""
    out: dict[int, str] = {}
    size = cfg.fetch.getitems_batch_size
    for i in range(0, len(appids), size):
        try:
            out.update(steam_store.fetch_descriptions(client, appids[i:i + size], cfg.fetch.steam_interval_sec))
        except FETCH_ERRORS as e:
            status.partial(f"日本語説明文: {e}")
            break
    return out


def _build_item(
    appid: int, d: dict[str, Any], tags: list[str], scores: dict[str, float], website: str | None,
    description_ja: str | None = None,
) -> dict[str, Any]:
    pos, neg = d["positive"], d["negative"]
    total = pos + neg
    return {
        "id": steam_id(appid),
        "source": "steam",
        "title": d.get("name") or "",
        "url": f"https://store.steampowered.com/app/{appid}/",
        "thumbnail": d.get("header_image"),
        "price_jpy": d.get("price_jpy"),
        "positive": pos,
        "negative": neg,
        "reviews": total,
        "positive_ratio": round(pos / total, 4) if total else None,
        "tags": tags,
        "has_demo": d.get("has_demo", False),
        "early_access": d.get("early_access", False),
        "languages": sorted(k for k, v in (d.get("languages") or {}).items() if v),
        "release_date": d.get("release_date"),
        "developers": d.get("developers") or [],
        "website": website,
        "description": d.get("short_description") or "",
        "description_ja": description_ja,
        "categories": scores,
        "score": max(scores.values()),
        "score_basis": "steam",
        "streaming": unknown_streaming(),
        "is_new": False,
        "stale": False,
    }


def unknown_streaming() -> dict[str, Any]:
    # 配信可否は段階5で判定する。それまでは全件「不明（未判定）」
    return {"verdict": "unknown", "method": None, "checked_at": None, "evidence": []}


def _run_steam(
    cfg: Config,
    client: HttpClient,
    store: StateStore,
    now: dt.datetime,
    max_new: int,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[SourceStatus]]:
    status = SourceStatus("steam")
    try:
        tag_ids = fetch_steam_tag_ids(client, cfg, warnings)
    except FETCH_ERRORS as e:
        status.fail(f"タグ一覧を取得できませんでした: {e}")
        return [], [status]
    tag_names = {v: k for k, v in tag_ids.items()}

    # 1) StoreQuery でカテゴリごとに全件取得（詳細込み）
    details: dict[int, dict[str, Any]] = {}
    cats_of: dict[int, list[str]] = {}
    ok_categories = 0
    for key, cat in cfg.categories.items():
        if not cat.steam:
            continue
        found = _query_category(cfg, client, key, cat, tag_ids, now, status)
        if found is None:
            continue
        ok_categories += 1
        for appid, d in found.items():
            details[appid] = d
            cats_of.setdefault(appid, []).append(key)
    if ok_categories == 0:
        status.status = "failed"
        return [], [status]

    # 2) 本フィルタとスコア
    today = now.date()
    passed: list[tuple[int, list[str], dict[str, float]]] = []
    reasons: Counter = Counter()
    for appid, d in details.items():
        tags = [tag_names.get(t, str(t)) for t in d.get("tagids", [])]
        scores: dict[str, float] = {}
        for key in cats_of[appid]:
            cat = cfg.categories[key]
            ok, why = filters.final_filter(d, d.get("positive", 0), d.get("negative", 0), cat, cfg.defaults)
            if not ok:
                reasons[why] += 1
                continue
            scores[key] = steam_score(
                positive=d["positive"], negative=d["negative"], release_date=d.get("release_date"),
                tags=set(tags), weights=cat.weights, bonus=cat.steam.bonus, today=today,
            )
        if scores:
            passed.append((appid, tags, scores))
    log.info("本フィルタ: 除外 %s / 通過 %d件", dict(reasons), len(passed))
    passed = _keep_top(cfg, passed, status)
    passed.sort(key=lambda p: (-max(p[2].values()), p[0]))

    # 3) 公式サイトURL（掲載作品のみ、スコア順）
    listed = [a for a, _, _ in passed]
    websites = _fetch_websites(cfg, client, store, listed, max_new, now, status)
    descriptions_ja = _fetch_descriptions_ja(cfg, client, listed, status)

    items = [
        _build_item(a, details[a], tags, scores, websites.get(steam_id(a), {}).get("website"),
                    descriptions_ja.get(a))
        for a, tags, scores in passed
    ]
    status.count = len(items)
    return items, [status]


def _update_itch_cache(
    cfg: Config, store: StateStore, found: dict[str, Any], now: dt.datetime, warnings: list[str],
) -> dict[str, Any]:
    """RSS で見つかった作品を蓄積キャッシュに反映し、保持期間を過ぎたものを捨てる。"""
    cache: dict[str, Any] = store.load(ITCH_CACHE, {})
    unknown_currencies: set[str] = set()
    for item_id, (item, tags, feeds) in found.items():
        rec = itch.to_record(item, tags, feeds, cfg.currency.to_jpy)
        if item.currency and item.currency not in cfg.currency.to_jpy:
            unknown_currencies.add(item.currency)
        prev = cache.get(item_id, {})
        rec["tags_matched"] = sorted(set(rec["tags_matched"]) | set(prev.get("tags_matched", [])))
        rec["feeds"] = sorted(set(rec["feeds"]) | set(prev.get("feeds", [])))
        rec["first_seen"] = prev.get("first_seen") or iso(now)
        rec["last_seen"] = iso(now)
        cache[item_id] = rec
    for cur in sorted(unknown_currencies):
        warnings.append(f"itch: 通貨 {cur} の換算レートが config.yaml の currency.to_jpy にありません（価格不明として扱います）")
    expired = [k for k, v in cache.items() if is_expired(v.get("last_seen"), cfg.itch.retention_days, now)]
    for k in expired:
        del cache[k]
    if expired:
        log.info("itch: 保持期間切れ %d件を削除", len(expired))
    store.save(ITCH_CACHE, cache)
    return cache


def _build_itch_item(item_id: str, rec: dict[str, Any], scores: dict[str, float]) -> dict[str, Any]:
    return {
        "id": item_id,
        "source": "itch",
        "title": rec["title"],
        "url": rec["url"],
        "thumbnail": rec.get("image"),
        "price_jpy": rec.get("price_jpy"),
        "positive": None,
        "negative": None,
        "reviews": None,
        "positive_ratio": None,
        "tags": rec.get("tags_matched", []),
        "has_demo": False,
        "early_access": False,
        "languages": [],  # RSS からは分からない
        "release_date": (rec.get("published") or "")[:10] or None,
        "developers": [rec["author"]],
        "author": rec["author"],
        "website": None,  # itch のページ自体が公式ページ
        "description": rec.get("description") or "",
        "description_ja": None,
        "platforms": rec.get("platforms", []),
        "feeds": rec.get("feeds", []),
        "categories": scores,
        "score": max(scores.values()),
        "score_basis": "itch",
        "streaming": unknown_streaming(),
        "is_new": False,
        "stale": False,
    }


def _run_itch(
    cfg: Config, client: HttpClient, store: StateStore, now: dt.datetime, warnings: list[str],
) -> tuple[list[dict[str, Any]], list[SourceStatus]]:
    tags = cfg.itch_tags()
    if not tags:
        return [], []
    found, status, empty = itch.collect(client, tags, list(cfg.itch.feeds), cfg.itch.pages, cfg.fetch.web_interval_sec)
    for tag in empty:
        warnings.append(f"itchタグ {tag!r} の RSS が0件でした（タグ名を確認してください）")
    # 取得に失敗しても、蓄積済みの作品で一覧を作る
    cache = _update_itch_cache(cfg, store, found, now, warnings)
    status.notes.append(f"今回 RSS で取得 {len(found)}件 / 蓄積 {len(cache)}件")

    today = now.date()
    per_cat: dict[str, list[tuple[float, str]]] = {}
    reasons: Counter = Counter()
    for key, cat in cfg.categories.items():
        if not cat.itch or not cat.itch.tags:
            continue
        ranked = []
        for item_id, rec in cache.items():
            if not set(rec.get("tags_matched", [])) & set(cat.itch.tags):
                continue
            ok, why = filters.itch_filter(rec, cat, cfg.defaults)
            if not ok:
                reasons[why] += 1
                continue
            score = itch_score(
                published=rec.get("published"), price_jpy=rec.get("price_jpy"),
                max_price_jpy=cat.filters.max_price_jpy, tags_matched=rec.get("tags_matched", []),
                category_tags=cat.itch.tags, feeds=rec.get("feeds", []), weights=cfg.itch_scoring, today=today,
            )
            ranked.append((score, item_id))
        ranked.sort(key=lambda r: (-r[0], r[1]))
        per_cat[key] = ranked[: cfg.itch.max_items]
        status.notes.append(f"{key}: 条件通過 {len(ranked)}件 → 掲載 {len(per_cat[key])}件")
    log.info("itch 本フィルタ: 除外 %s", dict(reasons))

    scores_of: dict[str, dict[str, float]] = {}
    for key, ranked in per_cat.items():
        for score, item_id in ranked:
            scores_of.setdefault(item_id, {})[key] = score
    items = [_build_itch_item(i, cache[i], s) for i, s in scores_of.items()]
    status.count = len(items)
    return items, [status]


def mark_new(items: list[dict[str, Any]], last_output: dict[str, Any]) -> None:
    """前回の出力に存在しなかった作品に NEW を付ける。

    前回の出力がない（初回）場合は全件が新規になってしまうので、何も付けない。
    前回データで代替した作品（stale）は新規扱いしない。
    """
    if not last_output.get("generated_at"):
        return
    prev = {i["id"] for i in last_output.get("items", [])}
    for item in items:
        item["is_new"] = not item.get("stale") and item["id"] not in prev


def run(
    cfg: Config,
    client: HttpClient,
    state_dir: str | Path,
    out_dir: str | Path,
    *,
    now: dt.datetime | None = None,
    max_new: int | None = None,
    sources: set[str] | None = None,
    check_streaming: bool = True,
) -> RunResult:
    now = now or utcnow()
    sources = sources or set(ALL_SOURCES)
    store = StateStore(state_dir)
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

    if "itch" in sources:
        itch_items, st = _run_itch(cfg, client, store, now, warnings)
        statuses += st
        items, merged = merge.merge(items, itch_items, cfg.merge.developer_similarity)
        if st:
            st[0].notes.append(f"Steam と同一作品として統合 {merged}件")

    items.sort(key=lambda i: (-i["score"], i["id"]))
    if items and check_streaming:
        statuses.append(streaming.run_checks(cfg, client, store, items, now))
    mark_new(items, last_output)
    for w in warnings:
        log.warning(w)

    # 配信可否チェックはデータソースではないので、ジョブの成否には含めない
    active = [s for s in statuses if s.status != "skipped" and s.name in ALL_SOURCES]
    exit_code = 1 if active and all(s.status == "failed" for s in active) else 0

    payload = {
        "generated_at": iso(now),
        "categories": {k: c.label for k, c in cfg.categories.items()},
        "sources": [s.to_dict() for s in statuses],
        "warnings": warnings,
        "items": items,
    }
    render.write_site(payload, out_dir)
    if exit_code == 0:
        store.save("last_output.json", {"generated_at": iso(now), "items": items})
    store.save("run_status.json", {
        "generated_at": iso(now),
        "exit_code": exit_code,
        "sources": payload["sources"],
        "warnings": warnings,
    })
    return RunResult(exit_code, items, statuses, warnings)
