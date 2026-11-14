import datetime as dt
from pathlib import Path

import pytest
import responses

from yamo import filters
from yamo.config import ItchScoringConfig
from yamo.scoring import itch_score
from yamo.sources import itch

from .conftest import FIXTURES, make_config

FEED = (FIXTURES / "itch_horror.xml").read_text(encoding="utf-8")
EMPTY_FEED = '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel><title>x</title></channel></rss>'
TODAY = dt.date(2026, 11, 15)


def test_parse_feed_real_sample():
    items = {i.id: i for i in itch.parse_feed(FEED)}
    # itch.io 以外の URL の項目は捨てる
    assert "itch:deeplookgames/dont-fix-her-pipes" in items and len(items) == 5
    pipes = items["itch:deeplookgames/dont-fix-her-pipes"]
    assert pipes.title == "Don't Fix Her Pipes"  # plainTitle（[Free] 等の付かない方）
    assert pipes.author == "deeplookgames"
    assert (pipes.price, pipes.currency) == (0.0, "USD")
    assert pipes.published == "2025-10-07T13:14:25Z"
    assert pipes.platforms == ["android", "windows"]
    assert "<img" not in pipes.description
    fog = items["itch:euro-dev/castle-fog"]
    assert (fog.price, fog.currency) == (1.5, "EUR")
    assert fog.description == "Fog & dread in a castle."  # タグ除去・実体参照の復元


@pytest.mark.parametrize("text,expected", [
    ("$4.99", 4.99), ("$0.00", 0.0), ("€1,50", 1.5), ("¥500", 500.0), ("$1,299.00", 1299.0),
    ("1.234,50 €", 1234.5), ("₩5,000", 5000.0), ("", None), (None, None), ("Free", None),
])
def test_parse_price(text, expected):
    assert itch.parse_price(text) == expected


def test_item_id():
    assert itch.item_id("https://Some-Dev.itch.io/my-game?x=1") == ("itch:some-dev/my-game", "some-dev")
    assert itch.item_id("https://itch.io/games/tag-horror") is None
    assert itch.item_id("https://some-dev.itch.io/") is None
    assert itch.item_id("https://example.com/game") is None


def test_xml_attacks_are_rejected():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            '<rss><channel><item><title>&lol2;</title></item></channel></rss>')
    with pytest.raises(ValueError):  # defusedxml の例外は ValueError のサブクラス
        itch.parse_feed(bomb)


@responses.activate
def test_collect_fetches_feeds_and_pages(client, fake_clock):
    calls = []

    def cb(req):
        calls.append(req.url)
        page = req.params.get("page")
        if "tag-funny" in req.url or page == "2":
            return 200, {}, EMPTY_FEED
        return 200, {}, FEED

    for url in itch.FEED_PATHS.values():
        responses.add_callback(responses.GET, url.format(tag="horror"), callback=cb)
        responses.add_callback(responses.GET, url.format(tag="funny"), callback=cb)
    found, status, empty = itch.collect(client, ["funny", "horror"], ["newest", "top_rated"], pages=3, interval=2)
    assert status.status == "ok"
    assert empty == ["funny"]
    # horror は 1ページ目あり → 2ページ目が空なので3ページ目は取らない。funny は1ページ目で終わり
    assert len(calls) == 2 + 2 * 2
    item, tags, feeds = found["itch:euro-dev/castle-fog"]
    assert tags == {"horror"} and feeds == {"newest", "top_rated"}
    assert all(s == 2 for s in fake_clock.sleeps)  # 2秒間隔


@responses.activate
def test_collect_partial_and_failed(client):
    responses.add(responses.GET, itch.FEED_PATHS["newest"].format(tag="horror"), status=404)
    responses.add(responses.GET, itch.FEED_PATHS["popular"].format(tag="horror"), body=FEED)
    found, status, _ = itch.collect(client, ["horror"], ["newest", "popular"], pages=1, interval=0)
    assert status.status == "partial" and len(found) == 5


@responses.activate
def test_collect_stops_when_itch_is_down(client):
    for url in itch.FEED_PATHS.values():
        responses.add(responses.GET, url.format(tag="horror"), status=503)
    found, status, empty = itch.collect(client, ["horror"], ["newest", "popular", "top_rated"], pages=1, interval=0)
    assert status.status == "failed" and found == {}
    assert len(responses.calls) == 3  # 1フィード目で再試行上限 → 残りは試さない
    assert empty == []  # 取得できていないので「0件のタグ」とは言えない


def test_to_record_converts_currency():
    items = {i.id: i for i in itch.parse_feed(FEED)}
    rec = itch.to_record(items["itch:euro-dev/castle-fog"], {"horror"}, {"newest"}, {"EUR": 165, "USD": 150})
    assert rec["price_jpy"] == 247.5
    rec = itch.to_record(items["itch:kr-dev/won-game"], {"horror"}, {"newest"}, {"USD": 150})
    assert rec["price_jpy"] is None  # 換算レートなし


# ---- スコア・フィルタ -----------------------------------------------------

def _score(**kw):
    args = dict(published="2026-11-01T00:00:00Z", price_jpy=0, max_price_jpy=2000, tags_matched=["horror"],
                category_tags=["horror"], feeds=[], weights=ItchScoringConfig(), today=TODAY)
    args.update(kw)
    return itch_score(**args)


def test_itch_score_components():
    assert _score(published="2026-11-15T00:00:00Z") > _score(published="2024-01-01T00:00:00Z")  # 新しいほど高い
    assert _score(price_jpy=0) > _score(price_jpy=1000) > _score(price_jpy=2000)
    assert _score(category_tags=["funny", "comedy"], tags_matched=["funny", "comedy"]) > \
        _score(category_tags=["funny", "comedy"], tags_matched=["funny"])
    assert _score(feeds=["top_rated"]) - _score() == 10
    assert _score(feeds=["popular", "top_rated", "newest"]) - _score() == 15


def test_itch_filter():
    cfg = make_config()
    cat = cfg.categories["horror"]
    rec = {"price_jpy": 0, "published": "2026-01-01T00:00:00Z"}
    assert filters.itch_filter(rec, cat, cfg.defaults) == (True, "")
    assert filters.itch_filter({**rec, "price_jpy": 3000}, cat, cfg.defaults)[1] == "価格上限超過"
    assert filters.itch_filter({**rec, "price_jpy": None}, cat, cfg.defaults)[1] == "価格不明"
    assert filters.itch_filter({**rec, "published": "2010-01-01T00:00:00Z"}, cat, cfg.defaults)[1] == "発売日が古い"
