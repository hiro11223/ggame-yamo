import json
import textwrap

import pytest
import responses

from yamo.pipeline import run
from yamo.streaming.overrides import apply_overrides, load_overrides

from .conftest import NOW
from .test_streaming_pipeline import FakeSites, FakeSteamWithTexts


def write(tmp_path, text):
    p = tmp_path / "overrides.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_load_valid(tmp_path):
    p = write(tmp_path, """
        - id: steam:620
          verdict: conditional
          note: 収益化はクレジット表記が条件
          evidence_url: https://example.com/streaming-policy
          checked_on: 2026-11-10
        - id: itch:some-dev/my-game
          verdict: ok
          checked_on: 2026-11-01
    """)
    ovs, warnings = load_overrides(p)
    assert warnings == []
    assert set(ovs) == {"steam:620", "itch:some-dev/my-game"}
    s = ovs["steam:620"].to_streaming()
    assert s == {
        "verdict": "conditional", "method": "manual", "checked_at": "2026-11-10T00:00:00Z",
        "checked_urls": ["https://example.com/streaming-policy"],
        "evidence": [{"url": "https://example.com/streaming-policy", "excerpt": "収益化はクレジット表記が条件", "matched": []}],
    }
    assert ovs["itch:some-dev/my-game"].to_streaming()["evidence"] == []


def test_invalid_rows_are_skipped_with_reasons(tmp_path):
    p = write(tmp_path, """
        - id: steam:1
          verdict: maybe
          checked_on: 2026-11-10
        - id: steam:abc
          verdict: ok
          checked_on: 2026-11-10
        - id: steam:2
          verdict: ok
          evidence_url: javascript:alert(1)
          checked_on: 2026-11-10
        - id: steam:3
          verdict: ok
          checked_on: 2026-13-40
        - id: steam:4
          verdict: ok
          checked_on: 2026-11-10
          verdit: ng
        - id: steam:5
          verdict: ok
        - just a string
        - id: steam:6
          verdict: ng
          checked_on: 2026-11-10
    """)
    ovs, warnings = load_overrides(p)
    assert set(ovs) == {"steam:6"}  # 正しい行だけ使う
    assert len(warnings) == 7
    assert "1件目（steam:1）" in warnings[0] and "verdict" in warnings[0]
    assert "steam:abc" in warnings[1]
    assert "http(s)" in warnings[2]
    assert "checked_on" in warnings[3]
    assert "verdit" in warnings[4]  # typo のキー
    assert "checked_on" in warnings[5]  # 必須項目なし
    assert "書式が正しくない" in warnings[6]


def test_duplicate_id_last_wins(tmp_path):
    p = write(tmp_path, """
        - {id: "steam:1", verdict: ok, checked_on: 2026-11-01}
        - {id: "steam:1", verdict: ng, checked_on: 2026-11-10}
    """)
    ovs, warnings = load_overrides(p)
    assert ovs["steam:1"].verdict == "ng"
    assert "重複" in warnings[0]


@pytest.mark.parametrize("text,expect_warning", [
    ("", False),
    ("[]\n", False),
    ("id: steam:1\n", True),  # リストになっていない
    ("- id: steam:1\n  verdict: [ok\n", True),  # YAML の書式エラー
])
def test_file_level_problems(tmp_path, text, expect_warning):
    ovs, warnings = load_overrides(write(tmp_path, text))
    assert ovs == {}
    assert bool(warnings) is expect_warning


def test_missing_file_is_fine(tmp_path):
    assert load_overrides(tmp_path / "nope.yaml") == ({}, [])


def test_apply_by_id_and_alias(tmp_path):
    p = write(tmp_path, """
        - {id: "steam:1", verdict: ok, checked_on: 2026-11-01}
        - {id: "itch:dev/game", verdict: ng, checked_on: 2026-11-01}
        - {id: "steam:999", verdict: ok, checked_on: 2026-11-01}
    """)
    ovs, _ = load_overrides(p)
    items = [
        {"id": "steam:1", "streaming": {"verdict": "unknown"}},
        {"id": "steam:2", "aliases": ["itch:dev/game"], "streaming": {"verdict": "ok", "method": "auto"}},
        {"id": "steam:3", "streaming": {"verdict": "unknown"}},
    ]
    applied, unused = apply_overrides(items, ovs)
    assert applied == 2 and unused == ["steam:999"]
    assert [i["streaming"]["verdict"] for i in items] == ["ok", "ng", "unknown"]
    assert items[1]["streaming"]["method"] == "manual"  # 自動判定より優先


# ---- パイプライン結合 -----------------------------------------------------

@pytest.fixture
def world():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        steam, sites = FakeSteamWithTexts(), FakeSites()
        steam.install(rsps)
        sites.install(rsps)
        yield steam, sites


def test_manual_override_wins_and_skips_auto_check(cfg, client, tmp_path, world):
    steam, sites = world
    p = write(tmp_path, """
        - id: steam:1001
          verdict: ng
          note: 公式 Discord で配信禁止と回答あり
          evidence_url: https://discord.example/answer
          checked_on: 2026-11-12
        - id: steam:42
          verdict: ok
          checked_on: 2026-11-12
        - id: steam:1004
          verdict: perhaps
          checked_on: 2026-11-12
    """)
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW, overrides_path=p)
    by_id = {i["id"]: i for i in result.items}

    manual = by_id["steam:1001"]["streaming"]
    assert manual["verdict"] == "ng" and manual["method"] == "manual"
    assert manual["evidence"][0]["excerpt"] == "公式 Discord で配信禁止と回答あり"
    # 手動確定のある 1001 は自動判定しない（公式サイトに行かない・streaming.json に入らない）
    assert not any("1001.example" in c for c in sites.calls)
    cache = json.loads((tmp_path / "state" / "streaming.json").read_text(encoding="utf-8"))
    assert "steam:1001" not in cache
    # 不正な行（1004）は無視され、1004 は自動判定のまま
    assert by_id["steam:1004"]["streaming"]["method"] == "auto"
    assert any("steam:1004" in w and "verdict" in w for w in result.warnings)

    st = {s.name: s for s in result.statuses}["streaming"]
    assert any("2件のうち掲載中 1件に反映" in n for n in st.notes)
    assert any("当たらなかった ID: steam:42" in n for n in st.notes)
    assert any("うち手動確認済み 1件" in n for n in st.notes)
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "警告 1件" in html


def test_override_applies_even_when_streaming_checks_are_off(cfg, client, tmp_path, world):
    p = write(tmp_path, '- {id: "steam:1001", verdict: ok, checked_on: 2026-11-12}\n')
    result = run(cfg, client, tmp_path / "state", tmp_path / "site", now=NOW,
                 overrides_path=p, check_streaming=False)
    by_id = {i["id"]: i for i in result.items}
    assert by_id["steam:1001"]["streaming"]["method"] == "manual"
    st = {s.name: s for s in result.statuses}["streaming"]
    assert st.status == "skipped"
    assert result.exit_code == 0


def test_repository_overrides_file_is_valid():
    from pathlib import Path

    ovs, warnings = load_overrides(Path(__file__).parent.parent / "overrides.yaml")
    assert warnings == []
