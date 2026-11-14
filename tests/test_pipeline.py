"""パイプライン結合テスト（外部APIはすべてモック）。"""

import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from yamo.pipeline import STEAM_CACHE, run
from yamo.sources import steam_store, steamspy
from yamo.state import StateStore

from .conftest import NOW, load_fixture, make_config


def _ts(date: str) -> int:
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp())


def store_item(appid, name, *, count, pct, price=None, free=False, langs=(0,), release="2024-03-12", demo=False):
    item = {
        "id": appid, "appid": appid, "success": 1, "visible": True, "type": 0, "name": name,
        "reviews": {"summary_filtered": {"review_count": count, "percent_positive": pct}},
        "release": {"steam_release_date": _ts(release)},
        "supported_languages": [{"elanguage": c, "supported": True} for c in langs],
        "basic_info": {"short_description": f"{name} desc", "developers": [{"name": "Dev"}]},
        "assets": {"asset_url_format": f"steam/apps/{appid}/${{FILENAME}}", "header": "header.jpg"},
    }
    if free:
        item["is_free"] = True
    if price is not None:
        item["best_purchase_option"] = {"final_price_in_cents": str(price * 100)}
    if demo:
        item["related_items"] = {"demo_appid": [appid + 100]}
    return item


STORE_ITEMS = {
    1001: store_item(1001, "Quiet Hallway", count=1000, pct=95, price=520, langs=(0, 10), demo=True),
    1004: store_item(1004, "Brand New Fright", count=42, pct=95, free=True, release="2026-10-01"),
}


class FakeSteam:
    """SteamSpy / ストアAPIの振る舞いをまとめたモック。"""

    def __init__(self, spy_status=200):
        self.spy_status = spy_status
        self.getitems_calls: list[list[int]] = []
        self.website_calls: list[int] = []
        self.store_items = dict(STORE_ITEMS)
        self.getitems_body = None  # 文字列を入れると、その本文を返す（JSON以外の応答の再現用）
        self.spy_bodies = {
            "Horror": load_fixture("steamspy_tag_horror.json"),
            "Short": {"1001": {"appid": 1001, "name": "Quiet Hallway"}},
            "Sexual Content": {"1005": {"appid": 1005, "name": "Spicy Haunt"}},
        }

    def install(self, rsps):
        rsps.add_callback(responses.GET, steamspy.API_URL, callback=self.spy)
        rsps.add_callback(responses.GET, steam_store.GETITEMS_URL, callback=self.getitems)
        rsps.add_callback(responses.GET, steam_store.APPDETAILS_URL, callback=self.appdetails)
        rsps.get(steam_store.TAGLIST_URL, json=[{"tagid": i, "name": n} for i, n in
                                                enumerate(["Horror", "Short ", "Sexual Content"])])

    def spy(self, req):
        if self.spy_status != 200:
            return self.spy_status, {}, ""
        return 200, {}, json.dumps(self.spy_bodies.get(req.params["tag"], []))

    def getitems(self, req):
        payload = json.loads(parse_qs(urlparse(req.url).query)["input_json"][0])
        ids = [i["appid"] for i in payload["ids"]]
        self.getitems_calls.append(ids)
        if self.getitems_body is not None:
            return 200, {"Content-Type": "text/html"}, self.getitems_body
        items = [self.store_items[a] for a in ids if a in self.store_items]
        return 200, {}, json.dumps({"response": {"store_items": items}})

    def appdetails(self, req):
        appid = int(parse_qs(urlparse(req.url).query)["appids"][0])
        self.website_calls.append(appid)
        return 200, {}, json.dumps({str(appid): {"success": True, "data": {"website": f"https://{appid}.example/"}}})


@pytest.fixture
def fake_steam():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        fs = FakeSteam()
        fs.install(rsps)
        yield fs


def test_end_to_end(cfg, client, tmp_path, fake_steam):
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert result.exit_code == 0
    # 1002 レビュー過多 / 1003 低評価 / 1005 除外タグ は事前フィルタで落ち、GetItems は1回のバッチ
    assert fake_steam.getitems_calls == [[1001, 1004]]
    assert sorted(i["id"] for i in result.items) == ["steam:1001", "steam:1004"]

    data = json.loads((tmp_path / "site" / "data.json").read_text(encoding="utf-8"))
    item = next(i for i in data["items"] if i["id"] == "steam:1001")
    assert item["price_jpy"] == 520
    assert item["tags"] == ["Horror", "Short"]
    assert item["languages"] == ["english", "japanese"]
    assert item["has_demo"] is True
    assert item["website"] == "https://1001.example/"
    assert (item["positive"], item["negative"]) == (950, 50)
    assert set(item["categories"]) == {"horror"}
    new = next(i for i in data["items"] if i["id"] == "steam:1004")
    # SteamSpy ではレビュー0件だが、GetItems の 42件×95% で補われる
    assert (new["positive"], new["negative"]) == (40, 2)
    assert [s["status"] for s in data["sources"]] == ["ok", "ok"]
    assert data["warnings"] == []  # 'Short ' の末尾空白でも実在扱い

    state = StateStore(tmp_path / "state")
    cache = state.load(STEAM_CACHE, {})
    assert cache["steam:1001"]["fetched_at"] == "2026-11-15T03:17:00Z"
    assert len(state.load("last_output.json", {})["items"]) == 2


def test_batch_size_is_respected(client, tmp_path, fake_steam):
    cfg = make_config(fetch={"getitems_batch_size": 1})
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert fake_steam.getitems_calls == [[1001], [1004]]


def test_cache_is_reused_within_ttl(cfg, client, tmp_path, fake_steam):
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=3))
    assert len(fake_steam.getitems_calls) == 1  # 7日以内は再取得しない
    assert len(fake_steam.website_calls) == 2
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=8))
    assert len(fake_steam.getitems_calls) == 2
    assert len(fake_steam.website_calls) == 4
    # GetItems で再取得しても、公式サイトURLは引き継がれる
    cache = StateStore(tmp_path / "state").load(STEAM_CACHE, {})
    assert cache["steam:1001"]["website"] == "https://1001.example/"


def test_max_new_limits_fetch_and_rest_is_deferred(cfg, client, tmp_path, fake_steam):
    r1 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=1)
    assert fake_steam.getitems_calls == [[1001]]  # 仮スコアの高い順
    assert [i["id"] for i in r1.items] == ["steam:1001"]
    assert "未取得(次回以降) 1件" in r1.statuses[1].notes[0]
    r2 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=1)
    assert fake_steam.getitems_calls == [[1001], [1004]]
    assert len(r2.items) == 2


def test_no_reviews_item_is_filtered(cfg, client, tmp_path, fake_steam):
    fake_steam.store_items[1004] = store_item(1004, "Brand New Fright", count=0, pct=0, free=True)
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert [i["id"] for i in result.items] == ["steam:1001"]  # レビューなし → min 30 で落ちる


def test_missing_tag_is_warned(client, tmp_path, fake_steam):
    cfg = make_config(categories={"horror": {"label": "ホラー", "steam": {
        "collect": ["Horror"], "bonus": {"Shrot": 5}}}})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=0)
    assert any("'Shrot' は存在しません" in w for w in result.warnings)
    assert any("'Shrot' の一覧が0件" in w for w in result.warnings)


def test_getitems_non_json_stops_calls(client, tmp_path, fake_steam):
    """GetItems が JSON 以外を返したら、その回はそれ以上呼ばない。"""
    fake_steam.getitems_body = "<!DOCTYPE html><title>Welcome to Steam</title>"
    cfg = make_config(fetch={"getitems_batch_size": 1})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert len(fake_steam.getitems_calls) == 1  # 2バッチ必要だが、1回目で打ち切る
    assert result.statuses[1].status == "partial"
    assert result.items == []


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
