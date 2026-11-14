"""パイプライン結合テスト（外部APIはすべてモック）。"""

import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from yamo.pipeline import WEBSITE_CACHE, run
from yamo.sources import steam_store
from yamo.state import StateStore

from .conftest import NOW, make_config

TAGS = {"Horror": 1667, "Psychological Horror": 1721, "Short": 4234, "Sexual Content": 12095}


def _ts(date: str) -> int:
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp())


def store_item(appid, name, *, tags, count, pct, price=None, free=False, langs=(0,),
               release="2024-03-12", demo=False):
    item = {
        "id": appid, "appid": appid, "success": 1, "visible": True, "type": 0, "name": name,
        "tags": [{"tagid": t, "weight": 100} for t in tags],
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


def catalog():
    H, PH, SHORT, SEX = TAGS["Horror"], TAGS["Psychological Horror"], TAGS["Short"], TAGS["Sexual Content"]
    return {
        1001: store_item(1001, "Quiet Hallway", tags=[H, SHORT], count=1000, pct=95, price=520,
                         langs=(0, 10), demo=True),
        1002: store_item(1002, "Mega Horror Blockbuster", tags=[H], count=200000, pct=95, price=1980),
        1003: store_item(1003, "Disliked Shack", tags=[PH], count=300, pct=33, price=100),
        1004: store_item(1004, "Brand New Fright", tags=[PH], count=42, pct=95, free=True, release="2026-10-01"),
        1005: store_item(1005, "Spicy Haunt", tags=[H, SEX], count=400, pct=95, price=300),
        1006: store_item(1006, "Silent Room", tags=[H], count=0, pct=0, price=300),
    }


class FakeSteam:
    """StoreQuery（タグの OR / 除外 / appid 昇順のページング）と appdetails を再現するモック。"""

    def __init__(self):
        self.catalog = catalog()
        self.queries: list[dict] = []
        self.website_calls: list[int] = []
        self.query_body = None  # 文字列を入れると、その本文を返す（JSON以外の応答の再現用）
        self.fail_from_start = None  # この start 以降は 500 を返す
        self.taglist_status = 200
        self.description_calls: list[list[int]] = []

    def install(self, rsps):
        rsps.add_callback(responses.GET, steam_store.QUERY_URL, callback=self.query)
        rsps.add_callback(responses.GET, steam_store.APPDETAILS_URL, callback=self.appdetails)
        rsps.add_callback(responses.GET, steam_store.TAGLIST_URL, callback=self.taglist)
        rsps.add_callback(responses.GET, steam_store.GETITEMS_URL, callback=self.getitems_ja)

    def getitems_ja(self, req):
        payload = json.loads(parse_qs(urlparse(req.url).query)["input_json"][0])
        assert payload["context"]["language"] == "japanese"
        ids = [i["appid"] for i in payload["ids"]]
        self.description_calls.append(ids)
        items = [{"id": a, "appid": a, "success": 1, "basic_info": {"short_description": f"{a} の説明"}}
                 for a in ids if a != 1004]  # 1004 は日本語説明なし
        return 200, {}, json.dumps({"response": {"store_items": items}})

    def taglist(self, req):
        if self.taglist_status != 200:
            return self.taglist_status, {}, ""
        # 実データ同様、末尾空白つきの名前を混ぜる
        return 200, {}, json.dumps([{"tagid": i, "name": n + (" " if n == "Short" else "")}
                                    for n, i in TAGS.items()])

    def query(self, req):
        payload = json.loads(parse_qs(urlparse(req.url).query)["input_json"][0])
        q = payload["query"]
        self.queries.append(q)
        if self.query_body is not None:
            return 200, {"Content-Type": "text/html"}, self.query_body
        if self.fail_from_start is not None and q["start"] >= self.fail_from_start:
            return 500, {}, ""
        assert q["sort"] == 2
        f = q["filters"]
        any_ids = set(f["tagids_must_match"][0]["tagids"])
        exclude = set(f.get("tagids_exclude", []))
        matched = [
            item for appid, item in sorted(self.catalog.items())
            if any_ids & {t["tagid"] for t in item["tags"]} and not exclude & {t["tagid"] for t in item["tags"]}
        ]
        page = matched[q["start"]:q["start"] + q["count"]]
        return 200, {}, json.dumps({"response": {
            "metadata": {"total_matching_records": len(matched), "start": q["start"], "count": len(page)},
            "store_items": page}})

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
    assert len(fake_steam.queries) == 1  # 1000件/ページなので1回
    assert fake_steam.queries[0]["filters"]["tagids_must_match"] == [{"tagids": [1667, 1721]}]
    assert fake_steam.queries[0]["filters"]["tagids_exclude"] == [12095]
    # 1002 レビュー過多 / 1003 低評価 / 1005 除外タグ（サーバー側）/ 1006 レビューなし で落ちる
    assert sorted(i["id"] for i in result.items) == ["steam:1001", "steam:1004"]

    data = json.loads((tmp_path / "site" / "data.json").read_text(encoding="utf-8"))
    item = next(i for i in data["items"] if i["id"] == "steam:1001")
    assert item["price_jpy"] == 520
    assert item["tags"] == ["Horror", "Short"]  # tagid → 名前（末尾空白は除去）
    assert item["languages"] == ["english", "japanese"]
    assert item["has_demo"] is True
    assert item["website"] == "https://1001.example/"
    assert (item["positive"], item["negative"]) == (950, 50)
    assert set(item["categories"]) == {"horror"}
    new = next(i for i in data["items"] if i["id"] == "steam:1004")
    assert (new["positive"], new["negative"]) == (40, 2)
    assert [s["status"] for s in data["sources"]] == ["ok"]
    assert data["warnings"] == []

    state = StateStore(tmp_path / "state")
    assert state.load(WEBSITE_CACHE, {})["steam:1001"]["fetched_at"] == "2026-11-15T03:17:00Z"
    assert len(state.load("last_output.json", {})["items"]) == 2


def test_japanese_descriptions_for_listed_items(cfg, client, tmp_path, fake_steam):
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert sorted(sum(fake_steam.description_calls, [])) == [1001, 1004]  # 掲載作品だけ
    by_id = {i["id"]: i for i in result.items}
    assert by_id["steam:1001"]["description_ja"] == "1001 の説明"
    assert by_id["steam:1004"]["description_ja"] is None
    assert by_id["steam:1004"]["description"] == "Brand New Fright desc"  # 英語は残る


def test_new_badge_on_second_run(cfg, client, tmp_path, fake_steam):
    r1 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert not any(i["is_new"] for i in r1.items)  # 初回は付けない
    fake_steam.catalog[1007] = store_item(1007, "Fresh Scare", tags=[1667], count=500, pct=97, price=300)
    r2 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=3))
    assert {i["id"] for i in r2.items if i["is_new"]} == {"steam:1007"}
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert '<span class="new-badge">NEW</span> 1件' in html


def test_bonus_tag_raises_score(cfg, client, tmp_path, fake_steam):
    fake_steam.catalog[1004] = store_item(1004, "Brand New Fright", tags=[1721], count=1000, pct=95,
                                          price=520, langs=(0, 10), demo=True)
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    by_id = {i["id"]: i for i in result.items}
    # 1001 と 1004 は Short タグ（+5）以外は同条件
    assert by_id["steam:1001"]["score"] - by_id["steam:1004"]["score"] == 5


def test_paging_collects_all(client, tmp_path, fake_steam):
    cfg = make_config(fetch={"query_page_size": 2})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    # 該当5件（1005 は除外）→ 2件ずつ3ページ
    assert [q["start"] for q in fake_steam.queries] == [0, 2, 4]
    assert sorted(i["id"] for i in result.items) == ["steam:1001", "steam:1004"]


def test_query_max_items_caps_collection(client, tmp_path, fake_steam):
    cfg = make_config(fetch={"query_page_size": 2, "query_max_items": 2})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert [q["start"] for q in fake_steam.queries] == [0]
    assert [i["id"] for i in result.items] == ["steam:1001"]  # appid 昇順の先頭2件（1001, 1002）だけ
    assert any("上限 2 件まで取得" in n for n in result.statuses[0].notes)


def test_max_items_keeps_top_per_category(client, tmp_path, fake_steam):
    base = {"require_any": ["Horror", "Psychological Horror"]}
    filters = {"max_price_jpy": 2000, "min_positive_ratio": 0.8, "reviews": {"min": 30, "max": 20000}}
    cfg = make_config(categories={
        "a": {"label": "A", "steam": {**base, "bonus": {"Short": 50}}, "filters": filters, "max_items": 1},
        "b": {"label": "B", "steam": base, "filters": filters},  # 既定 300件
    })
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    by_id = {i["id"]: i for i in result.items}
    # a は上位1件（Short で +50 の 1001）だけ。1004 は a から外れ b にだけ残る
    assert set(by_id["steam:1001"]["categories"]) == {"a", "b"}
    assert set(by_id["steam:1004"]["categories"]) == {"b"}
    assert by_id["steam:1004"]["score"] == by_id["steam:1004"]["categories"]["b"]
    # 除外タグ未指定なので 1005 も通過し、条件通過は3件
    assert set(by_id["steam:1005"]["categories"]) == {"b"}
    assert "a: 条件通過 3件 → 掲載 1件" in result.statuses[0].notes


def test_item_dropped_when_outside_all_limits(client, tmp_path, fake_steam):
    cfg = make_config()
    cfg.categories["horror"].max_items = 1
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert len(result.items) == 1
    assert len(fake_steam.website_calls) == 1  # 掲載外の作品の公式サイトは取りに行かない


def test_website_cache_ttl(cfg, client, tmp_path, fake_steam):
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert sorted(fake_steam.website_calls) == [1001, 1004]
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=3))
    assert len(fake_steam.website_calls) == 2  # 7日以内は再取得しない
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=8))
    assert len(fake_steam.website_calls) == 4


def test_max_new_limits_website_fetch(cfg, client, tmp_path, fake_steam):
    r1 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=1)
    assert len(fake_steam.website_calls) == 1
    top = r1.items[0]
    assert fake_steam.website_calls == [int(top["id"].split(":")[1])]  # スコアの高い順
    assert r1.items[1]["website"] is None
    assert any("未取得(次回以降) 1件" in n for n in r1.statuses[0].notes)
    r2 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, max_new=1)
    assert len(fake_steam.website_calls) == 2
    assert all(i["website"] for i in r2.items)


def test_missing_tag_is_warned(client, tmp_path, fake_steam):
    cfg = make_config(categories={"horror": {"label": "ホラー", "steam": {
        "require_any": ["Horror"], "bonus": {"Shrot": 5}}}})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert any("'Shrot' は存在しません" in w for w in result.warnings)
    assert result.exit_code == 0


def test_category_without_valid_tags_is_skipped(client, tmp_path, fake_steam):
    cfg = make_config(categories={"x": {"label": "x", "steam": {"require_any": ["Nope"]}}})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert fake_steam.queries == []
    assert result.statuses[0].status == "failed" and result.exit_code == 1


def test_query_failure_midway_keeps_first_pages(client, tmp_path, fake_steam):
    fake_steam.fail_from_start = 2
    cfg = make_config(fetch={"query_page_size": 2, "retry": {"max_attempts": 2}})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert result.statuses[0].status == "partial"
    assert result.exit_code == 0
    assert [i["id"] for i in result.items] == ["steam:1001"]  # 1ページ目（1001, 1002）の分は残る


def test_query_non_json_fails_source(cfg, client, tmp_path, fake_steam):
    fake_steam.query_body = "<!DOCTYPE html><title>Welcome to Steam</title>"
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    assert len(fake_steam.queries) == 1
    assert result.statuses[0].status == "failed"
    assert result.exit_code == 1


def test_steam_down_falls_back_to_previous(cfg, client, tmp_path, fake_steam):
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    fake_steam.taglist_status = 503
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=3))
    assert result.exit_code == 1  # 全ソース失敗 → ジョブ失敗
    assert [s.status for s in result.statuses] == ["failed"]
    assert all(i["stale"] for i in result.items) and len(result.items) == 2
    # 失敗時は last_output を上書きしない（次回の NEW 判定を壊さない）
    prev = StateStore(tmp_path / "state").load("last_output.json", {})
    assert prev["generated_at"] == "2026-11-15T03:17:00Z"
