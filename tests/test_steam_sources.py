import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import responses

from yamo.sources import steam_store

from .conftest import NOW, load_fixture


# ---- タグ一覧 -----------------------------------------------------------

@responses.activate
def test_tag_names_are_stripped(client):
    # 実データには 'Parody ' のように末尾空白つきの名前がある
    responses.get(steam_store.TAGLIST_URL, json=[{"tagid": 4878, "name": "Parody "}, {"tagid": 1667, "name": "Horror"}])
    assert steam_store.fetch_tag_names(client, 0) == {"Parody": 4878, "Horror": 1667}


# ---- GetItems -----------------------------------------------------------

def _items():
    """実APIの応答（2026-11-15取得、項目を間引いたもの）。"""
    return {i["id"]: i for i in load_fixture("getitems_sample.json")["response"]["store_items"]}


def test_parse_store_item_paid_game():
    d = steam_store.parse_store_item(_items()[1150690], NOW)  # OMORI
    assert d["name"] == "OMORI" and d["type"] == "game"
    assert d["price_jpy"] == 1980
    assert d["languages"] == {"english": True, "japanese": True}
    assert d["has_demo"] is False
    assert d["release_date"] == "2020-12-25" and d["coming_soon"] is False
    assert d["developers"] == ["OMOCAT, LLC"]
    assert d["header_image"] == ("https://shared.akamai.steamstatic.com/store_item_assets/"
                                 "steam/apps/1150690/header.jpg?t=1671584768")
    # 80516件 × 97% → 好評 78101 / 不評 2415（推定）
    assert (d["positive"], d["negative"]) == (78101, 2415)


def test_parse_store_item_free_and_english_only():
    d = steam_store.parse_store_item(_items()[698780], NOW)  # Doki Doki Literature Club!
    assert d["is_free"] is True and d["price_jpy"] == 0
    assert d["languages"] == {"english": True, "japanese": False}


def test_parse_store_item_demo_and_early_access():
    items = _items()
    assert steam_store.parse_store_item(items[1092790], NOW)["has_demo"] is True  # Inscryption
    assert steam_store.parse_store_item(items[1966720], NOW)["early_access"] is True  # Lethal Company


def test_parse_store_item_not_found():
    assert steam_store.parse_store_item(_items()[999999999], NOW) == {"unavailable": True}


def test_parse_store_item_coming_soon():
    future = int((NOW + dt.timedelta(days=30)).timestamp())
    base = {"id": 5, "success": 1, "type": 0, "name": "X"}
    assert steam_store.parse_store_item({**base, "release": {"steam_release_date": future}}, NOW)["coming_soon"] is True
    assert steam_store.parse_store_item({**base, "release": {}}, NOW)["coming_soon"] is True
    assert steam_store.parse_store_item({**base, "release": {"is_coming_soon": True}}, NOW)["coming_soon"] is True


def test_reviews_from_summary():
    assert steam_store.reviews_from_summary({"review_count": 0, "percent_positive": 0}) == (0, 0)
    assert steam_store.reviews_from_summary(None) == (0, 0)
    assert steam_store.reviews_from_summary({"review_count": 3, "percent_positive": 66}) == (2, 1)


@responses.activate
def test_fetch_items_batches_and_marks_missing(client):
    def cb(req):
        payload = json.loads(parse_qs(urlparse(req.url).query)["input_json"][0])
        assert payload["context"] == {"language": "english", "country_code": "JP"}
        assert payload["data_request"]["include_reviews"] is True
        assert [i["appid"] for i in payload["ids"]] == [1150690, 698780, 42]
        items = _items()
        return 200, {}, json.dumps({"response": {"store_items": [items[1150690], items[698780]]}})

    responses.add_callback(responses.GET, steam_store.GETITEMS_URL, callback=cb)
    out = steam_store.fetch_items(client, [1150690, 698780, 42], 0, NOW)
    assert out[1150690]["name"] == "OMORI"
    assert out[42] == {"unavailable": True}  # 応答に含まれなかったもの


def test_parse_website():
    payload = load_fixture("appdetails_1001.json")
    assert steam_store.parse_website(1001, payload) == "https://tinyghost.example/"
    assert steam_store.parse_website(5, {"5": {"success": False}}) is None


# ---- StoreQuery ---------------------------------------------------------

@responses.activate
def test_query_page_builds_filters_and_parses(client):
    seen = {}

    def cb(req):
        seen.update(json.loads(parse_qs(urlparse(req.url).query)["input_json"][0]))
        items = _items()
        omori = dict(items[1150690], tags=[{"tagid": 1667, "weight": 900}, {"tagid": 4234, "weight": 500}])
        body = {"response": {"metadata": {"total_matching_records": 2500, "start": 0, "count": 2},
                             "store_items": [omori, items[698780]]}}
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, steam_store.QUERY_URL, callback=cb)
    total, items = steam_store.query_page(
        client, tagids_any=[1667, 1721], exclude=[12095], released_only=True,
        start=1000, count=1000, interval=0, now=NOW,
    )
    q = seen["query"]
    assert (q["start"], q["count"], q["sort"]) == (1000, 1000, 2)  # sort 省略はランダム順になるので固定
    assert q["filters"]["tagids_must_match"] == [{"tagids": [1667, 1721]}]  # 1グループ = OR
    assert q["filters"]["tagids_exclude"] == [12095]
    assert q["filters"]["released_only"] is True
    assert q["filters"]["type_filters"] == {"include_games": True}
    assert seen["context"] == {"language": "english", "country_code": "JP"}
    assert seen["data_request"]["include_tag_count"] == 20
    assert total == 2500
    assert set(items) == {1150690, 698780}
    assert items[1150690]["tagids"] == [1667, 4234]
    assert items[1150690]["price_jpy"] == 1980


@responses.activate
def test_query_page_without_exclude_and_unreleased(client):
    seen = {}

    def cb(req):
        seen.update(json.loads(parse_qs(urlparse(req.url).query)["input_json"][0]))
        return 200, {}, json.dumps({"response": {"metadata": {"total_matching_records": 0}}})

    responses.add_callback(responses.GET, steam_store.QUERY_URL, callback=cb)
    total, items = steam_store.query_page(
        client, tagids_any=[1], exclude=[], released_only=False, start=0, count=10, interval=0, now=NOW,
    )
    assert (total, items) == (0, {})
    assert "tagids_exclude" not in seen["query"]["filters"]
    assert "released_only" not in seen["query"]["filters"]
