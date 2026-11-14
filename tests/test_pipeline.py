"""パイプライン結合テスト（外部APIはすべてモック）。"""

import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from yamo.pipeline import run
from yamo.sources import steam_store, steamspy
from yamo.state import StateStore

from .conftest import NOW, load_fixture

APPDETAILS = {
    1001: load_fixture("appdetails_1001.json"),
    1004: {"1004": {"success": True, "data": {
        "type": "game", "name": "Brand New Fright", "is_free": True,
        "supported_languages": "English", "header_image": "https://example.invalid/1004.jpg",
        "developers": ["Fresh Dev"], "release_date": {"coming_soon": False, "date": "1 Oct, 2026"},
    }}},
}


class FakeSteam:
    """SteamSpy / ストアAPIの振る舞いをまとめたモック。"""

    def __init__(self, spy_status=200):
        self.spy_status = spy_status
        self.appdetails_calls: list[int] = []
        self.spy_bodies = {
            "Horror": load_fixture("steamspy_tag_horror.json"),
            "Short": {"1001": {"appid": 1001, "name": "Quiet Hallway"}},
            "Sexual Content": {"1005": {"appid": 1005, "name": "Spicy Haunt"}},
        }

    def install(self, rsps):
        rsps.add_callback(responses.GET, steamspy.API_URL, callback=self.spy)
        rsps.add_callback(responses.GET, steam_store.APPDETAILS_URL, callback=self.details)
        rsps.add_callback(responses.GET, re.compile(r"https://store\.steampowered\.com/appreviews/\d+"),
                               callback=self.reviews)
        rsps.get(steam_store.TAGLIST_URL, json=[{"tagid": i, "name": n} for i, n in
                                                      enumerate(["Horror", "Short", "Sexual Content"])])

    def spy(self, req):
        if self.spy_status != 200:
            return self.spy_status, {}, ""
        return 200, {}, json.dumps(self.spy_bodies.get(req.params["tag"], []))

    def details(self, req):
        q = parse_qs(urlparse(req.url).query)
        assert q["l"] == ["english"] and q["cc"] == ["jp"]
        appid = int(q["appids"][0])
        self.appdetails_calls.append(appid)
        return 200, {}, json.dumps(APPDETAILS[appid])

    def reviews(self, req):
        return 200, {}, json.dumps({"success": 1, "query_summary": {"total_positive": 40, "total_negative": 2}})


@pytest.fixture
def fake_steam():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        fs = FakeSteam()
        fs.install(rsps)
        yield fs


def test_end_to_end(cfg, client, tmp_path, fake_steam):
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert result.exit_code == 0
    ids = [i["id"] for i in result.items]
    # 1002 レビュー過多 / 1003 低評価 / 1005 除外タグ で落ち、1001 と 1004 が残る
    assert sorted(ids) == ["steam:1001", "steam:1004"]

    data = json.loads((tmp_path / "site" / "data.json").read_text(encoding="utf-8"))
    item = next(i for i in data["items"] if i["id"] == "steam:1001")
    assert item["price_jpy"] == 520
    assert item["tags"] == ["Horror", "Short"]
    assert item["languages"] == ["english", "japanese"]
    assert item["has_demo"] is True
    assert set(item["categories"]) == {"horror"}
    new = next(i for i in data["items"] if i["id"] == "steam:1004")
    assert (new["positive"], new["negative"]) == (40, 2)  # appreviews で補完
    assert [s["status"] for s in data["sources"]] == ["ok", "ok"]
    assert data["warnings"] == []

    state = StateStore(tmp_path / "state")
    assert state.load("appdetails.json", {})["steam:1001"]["fetched_at"] == "2026-11-15T03:17:00Z"
    assert len(state.load("last_output.json", {})["items"]) == 2


def test_cache_is_reused_within_ttl(cfg, client, tmp_path, fake_steam):
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert sorted(fake_steam.appdetails_calls) == [1001, 1004]
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=3))
    assert len(fake_steam.appdetails_calls) == 2  # 7日以内は再取得しない
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=8))
    assert len(fake_steam.appdetails_calls) == 4


def test_max_new_limits_fetch_and_rest_is_deferred(cfg, client, tmp_path, fake_steam):
    r1 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=1)
    assert fake_steam.appdetails_calls == [1001]  # 仮スコアの高い順
    assert [i["id"] for i in r1.items] == ["steam:1001"]
    assert "未取得(次回以降) 1件" in r1.statuses[1].notes[0]
    r2 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=1)
    assert fake_steam.appdetails_calls == [1001, 1004]
    assert len(r2.items) == 2


def test_missing_tag_is_warned(client, tmp_path, fake_steam):
    from .conftest import make_config

    cfg = make_config(categories={"horror": {"label": "ホラー", "steam": {
        "collect": ["Horror"], "bonus": {"Shrot": 5}}}})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=0)
    assert any("'Shrot' は存在しません" in w for w in result.warnings)
    assert any("'Shrot' の一覧が0件" in w for w in result.warnings)


def test_appreviews_non_json_stops_calls(cfg, client, tmp_path, monkeypatch):
    """appreviews がトップページへリダイレクトされる等で JSON 以外を返したら、以降は呼ばない。"""
    calls = []

    def html(req):
        calls.append(req.url)
        return 200, {"Content-Type": "text/html"}, "<!DOCTYPE html><title>Welcome to Steam</title>"

    # レビュー数のない作品を2つにする（1004 と、その複製の 1006）
    monkeypatch.setitem(APPDETAILS, 1006, {"1006": APPDETAILS[1004]["1004"]})
    fs = FakeSteam()
    fs.spy_bodies["Horror"]["1006"] = dict(fs.spy_bodies["Horror"]["1004"], appid=1006)
    fs.reviews = html  # install より前に差し替える（install 時点の self.reviews が登録される）
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        fs.install(rsps)
        result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert len(calls) == 1  # 2作品とも必要だが、1回目で打ち切る
    assert result.statuses[1].status == "partial"
    assert [i["id"] for i in result.items] == ["steam:1001"]  # レビュー数不明の作品は落ちる


def test_steamspy_down_falls_back_to_previous(cfg, client, tmp_path, fake_steam):
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    fake_steam.spy_status = 503
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=3))
    assert result.exit_code == 1  # 全ソース失敗 → ジョブ失敗
    assert [s.status for s in result.statuses] == ["failed", "skipped"]
    assert all(i["stale"] for i in result.items) and len(result.items) == 2
    # 失敗時は last_output を上書きしない（次回の NEW 判定を壊さない）
    prev = StateStore(tmp_path / "state").load("last_output.json", {})
    assert prev["generated_at"] == "2026-11-15T03:17:00Z"
