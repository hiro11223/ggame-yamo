import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import responses

from yamo.sources import steam_store, steamspy

from .conftest import NOW, load_fixture


# ---- SteamSpy -----------------------------------------------------------

def test_parse_steamspy_tag():
    apps = steamspy.parse_tag_response(json.dumps(load_fixture("steamspy_tag_horror.json")))
    assert apps[1001].positive == 950 and apps[1001].reviews == 1000
    assert apps[1001].price_usd_cents == 499
    assert apps[1001].developer == "Tiny Ghost"


def test_parse_steamspy_unknown_tag_returns_empty():
    assert steamspy.parse_tag_response("[]") == {}


@responses.activate
def test_collect_merges_tag_membership(client, fake_clock):
    def cb(req):
        tag = req.params["tag"]
        body = {"Horror": load_fixture("steamspy_tag_horror.json"),
                "Short": {"1001": {"appid": 1001, "name": "Quiet Hallway"}},
                "Nope": []}[tag]
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, steamspy.API_URL, callback=cb)
    apps, status, empty = steamspy.collect(client, ["Horror", "Nope", "Short"], 60)
    assert status.status == "ok"
    assert apps[1001].tags == {"Horror", "Short"}
    assert apps[1002].tags == {"Horror"}
    assert empty == ["Nope"]
    assert fake_clock.sleeps == [60, 60]  # 一覧系は 1req/分


@responses.activate
def test_collect_partial_when_one_tag_fails(client):
    def cb(req):
        if req.params["tag"] == "Bad":
            return 404, {}, ""
        return 200, {}, json.dumps(load_fixture("steamspy_tag_horror.json"))

    responses.add_callback(responses.GET, steamspy.API_URL, callback=cb)
    apps, status, _ = steamspy.collect(client, ["Bad", "Horror"], 60)
    assert status.status == "partial"
    assert len(apps) == 5


@responses.activate
def test_collect_failed_when_source_down(client):
    responses.get(steamspy.API_URL, status=503)
    apps, status, _ = steamspy.collect(client, ["Horror", "Short"], 60)
    assert status.status == "failed"
    assert apps == {}
    assert len(responses.calls) == 3  # 1タグ目で上限 → 残りのタグは試さない


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
