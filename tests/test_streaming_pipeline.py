"""配信可否チェックを含めたパイプライン結合テスト（外部はすべてモック）。"""

import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from yamo.pipeline import run
from yamo.state import StateStore

from .conftest import NOW, make_config
from .test_pipeline import FakeSteam

# 1001 はストア説明文には記述なし、公式サイトの /press に許可の記述
# 1004 はストア説明文に禁止の記述
STORE_TEXTS = {
    1001: "<p>A short walk down a hallway.</p>",
    1004: "<p>Streaming and recording of this game is prohibited.</p>",
}
SITE_PAGES = {
    "/": "<p>Welcome! Follow us on <a href='https://x.com/dev'>X</a>.</p>",
    "/press": "<h2>Streaming</h2><p>Feel free to stream and monetize videos of our game!</p>",
}


class FakeSteamWithTexts(FakeSteam):
    def appdetails(self, req):
        appid = int(parse_qs(urlparse(req.url).query)["appids"][0])
        self.website_calls.append(appid)
        data = {"website": f"https://{appid}.example/", "detailed_description": STORE_TEXTS.get(appid, "")}
        return 200, {}, json.dumps({str(appid): {"success": True, "data": data}})


class FakeSites:
    def __init__(self):
        self.calls: list[str] = []

    def install(self, rsps):
        rsps.add_callback(responses.GET, re.compile(r"https://\d+\.example/.*"), callback=self.cb)

    def cb(self, req):
        self.calls.append(req.url)
        u = urlparse(req.url)
        if u.path == "/robots.txt":
            return 404, {}, ""
        if u.hostname == "1001.example" and u.path in SITE_PAGES:
            return 200, {"Content-Type": "text/html; charset=utf-8"}, SITE_PAGES[u.path]
        return 404, {}, ""


@pytest.fixture
def world():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        steam, sites = FakeSteamWithTexts(), FakeSites()
        steam.install(rsps)
        sites.install(rsps)
        yield steam, sites


def test_verdicts_evidence_and_history(cfg, client, tmp_path, world):
    steam, sites = world
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    by_id = {i["id"]: i for i in result.items}

    ok = by_id["steam:1001"]["streaming"]
    assert ok["verdict"] == "ok" and ok["method"] == "auto" and ok["checked_at"] == "2026-11-15T03:17:00Z"
    assert ok["evidence"][0]["url"] == "https://1001.example/press"
    assert "feel free to stream" in ok["evidence"][0]["matched"]
    assert len(ok["evidence"][0]["excerpt"]) <= 200
    assert "https://1001.example/press" in ok["checked_urls"]

    ng = by_id["steam:1004"]["streaming"]
    assert ng["verdict"] == "ng"
    assert ng["evidence"][0]["url"] == "https://store.steampowered.com/app/1004/"

    # SNS（x.com）には行かない
    assert not any("x.com" in c for c in sites.calls)

    store = StateStore(tmp_path / "state")
    cache = store.load("streaming.json", {})
    assert set(cache) == {"steam:1001", "steam:1004"}
    # ページ全文は保存しない（抜粋のみ）
    assert "A short walk" not in json.dumps(cache)
    history = (tmp_path / "state" / "verdict_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(h)["to"] for h in history} == {"ok", "ng"}

    st = {s.name: s for s in result.statuses}["streaming"]
    assert st.count == 2 and "OK 1・条件付き 0・NG 1・不明 0" in st.notes[0]
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "配信可否チェック: 正常" in html


def test_recheck_only_after_ttl(cfg, client, tmp_path, world):
    steam, sites = world
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    n = len(sites.calls)
    r2 = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=10))
    assert len(sites.calls) == n  # 30日以内は再判定しない
    assert {i["id"]: i["streaming"]["verdict"] for i in r2.items} == {"steam:1001": "ok", "steam:1004": "ng"}
    run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW + dt.timedelta(days=31))
    assert len(sites.calls) > n
    # 判定が変わっていなければ履歴は増えない
    history = (tmp_path / "state" / "verdict_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 2


def test_max_streaming_checks_by_score(client, tmp_path, world):
    cfg = make_config(fetch={"max_streaming_checks": 1})
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    checked = [i for i in result.items if i["streaming"]["checked_at"]]
    assert len(checked) == 1 and checked[0]["id"] == result.items[0]["id"]  # スコア最上位から
    st = {s.name: s for s in result.statuses}["streaming"]
    assert "次回以降 1件" in st.notes[0]


def test_streaming_failure_does_not_fail_job(cfg, client, tmp_path):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        broken = FakeSteamWithTexts()
        broken.appdetails = lambda req: (503, {}, "")  # install より前に差し替える
        broken.install(rsps)
        result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW)
    st = {s.name: s for s in result.statuses}
    assert st["streaming"].status == "partial"
    assert result.exit_code == 0  # 配信可否チェックの失敗はジョブの成否に含めない
    assert all(i["streaming"]["verdict"] == "unknown" for i in result.items)
