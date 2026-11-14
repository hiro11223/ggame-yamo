import json

import responses

from yamo.sources import steam_store, steamspy

from .conftest import load_fixture

TO_JPY = {"JPY": 1.0, "USD": 150.0}


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


def test_parse_appdetails():
    d = steam_store.parse_appdetails(1001, load_fixture("appdetails_1001.json"), TO_JPY)
    assert d["name"] == "Quiet Hallway"
    assert d["price_jpy"] == 520
    assert d["languages"] == {"japanese": True, "english": True}
    assert d["has_demo"] is True
    assert d["release_date"] == "2024-03-12"
    assert d["coming_soon"] is False
    assert d["developers"] == ["Tiny Ghost"]


def test_parse_appdetails_unsuccessful():
    assert steam_store.parse_appdetails(5, {"5": {"success": False}}, TO_JPY) == {"unavailable": True}


def test_parse_appdetails_free_and_usd():
    free = {"9": {"success": True, "data": {"is_free": True}}}
    assert steam_store.parse_appdetails(9, free, TO_JPY)["price_jpy"] == 0
    usd = {"9": {"success": True, "data": {"price_overview": {"currency": "USD", "final": 499}}}}
    assert steam_store.parse_appdetails(9, usd, TO_JPY)["price_jpy"] == 748.5


def test_parse_languages_variants():
    assert steam_store.parse_languages("English") == {"japanese": False, "english": True}
    assert steam_store.parse_languages("Japanese<strong>*</strong>") == {"japanese": True, "english": False}
    # "Simplified Chinese" のような部分一致で誤判定しない
    assert steam_store.parse_languages("Simplified Chinese, Korean") == {"japanese": False, "english": False}
    assert steam_store.parse_languages(None) == {"japanese": False, "english": False}


def test_parse_release_date_formats():
    assert steam_store.parse_release_date("12 Mar, 2024") == "2024-03-12"
    assert steam_store.parse_release_date("Mar 12, 2024") == "2024-03-12"
    assert steam_store.parse_release_date("Q3 2026") is None
    assert steam_store.parse_release_date("Coming soon") is None


def test_parse_appreviews():
    payload = {"success": 1, "query_summary": {"total_positive": 40, "total_negative": 2, "total_reviews": 42}}
    assert steam_store.parse_appreviews(payload) == {"positive": 40, "negative": 2}
    assert steam_store.parse_appreviews({"success": 0}) is None
