import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from yamo import render
from yamo.pipeline import mark_new


def item(id_, **kw):
    base = {
        "id": id_, "source": "steam", "title": f"Game {id_}", "url": "https://store.steampowered.com/app/1/",
        "thumbnail": None, "price_jpy": 500, "positive": 90, "negative": 10, "reviews": 100,
        "positive_ratio": 0.9, "tags": ["Horror"], "has_demo": False, "early_access": False,
        "languages": ["english"], "release_date": "2024-01-01", "developers": ["Dev"], "website": None,
        "description": "desc", "description_ja": None, "categories": {"horror": 80.0}, "score": 80.0,
        "streaming": {"verdict": "unknown", "method": None, "checked_at": None, "evidence": []},
        "is_new": False, "stale": False,
    }
    base.update(kw)
    return base


def payload(items, **kw):
    p = {
        "generated_at": "2026-11-15T03:17:00Z",
        "categories": {"horror": "ホラー", "bakage": "バカゲー"},
        "sources": [{"name": "steam", "status": "ok", "count": len(items), "errors": [], "notes": ["n1"]}],
        "warnings": [],
        "items": items,
    }
    p.update(kw)
    return p


def embedded_data(html: str):
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "データ埋め込みが見つからない"
    return json.loads(m.group(1))


def test_write_site_outputs_files(tmp_path):
    render.write_site(payload([item("steam:1")]), tmp_path)
    for name in ("index.html", "app.js", "style.css", "data.json", "robots.txt", ".nojekyll"):
        assert (tmp_path / name).exists(), name
    assert (tmp_path / "robots.txt").read_text(encoding="utf-8") == "User-agent: *\nDisallow: /\n"


def test_index_has_noindex_and_tabs(tmp_path):
    render.write_site(payload([item("steam:1"), item("steam:2", categories={"bakage": 70.0}, score=70.0)]), tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert 'data-tab="horror"' in html and 'data-tab="bakage"' in html and 'data-tab="all"' in html
    assert "2026-11-15 12:17 JST" in html  # UTC 03:17 → JST
    assert "Steam: 正常（2件）" in html


def test_untrusted_text_cannot_break_out_of_script(tmp_path):
    evil = '</script><script>alert(1)</script><!--'
    p = payload([item("steam:1", title=evil, description="a & b > c ")])
    render.write_site(p, tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html
    assert html.count("</script>") == 2  # データ埋め込み と app.js の2つだけ
    assert embedded_data(html)["items"][0]["title"] == evil  # 値自体は壊れずに復元できる


def test_warnings_failures_and_stale_are_shown(tmp_path):
    p = payload(
        [item("steam:1", stale=True)],
        sources=[{"name": "steam", "status": "failed", "count": 0, "errors": ["boom <b>"], "notes": []}],
        warnings=["Steamタグ 'Shrot' は存在しません"],
    )
    render.write_site(p, tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Steam: 失敗" in html
    assert "boom &lt;b&gt;" in html  # テンプレート側は自動エスケープ
    assert "警告 1件" in html
    assert "Shrot" in html
    assert "1件は前回のデータを表示しています" in html


def test_new_count_in_header(tmp_path):
    render.write_site(payload([item("steam:1", is_new=True), item("steam:2")]), tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '<span class="new-badge">NEW</span> 1件' in html


# ---- NEW 判定 -------------------------------------------------------------

def test_mark_new_skips_first_run():
    items = [item("steam:1"), item("steam:2")]
    mark_new(items, {"items": []})
    assert not any(i["is_new"] for i in items)


def test_mark_new_flags_items_missing_from_previous():
    items = [item("steam:1"), item("steam:2"), item("steam:3", stale=True)]
    mark_new(items, {"generated_at": "2026-11-12T03:17:00Z", "items": [{"id": "steam:1"}]})
    assert [i["is_new"] for i in items] == [False, True, False]


# ---- JS（Node があれば実行） ---------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node が見つからない")
def test_app_js_unit_tests():
    files = sorted(str(p) for p in (Path(__file__).parent / "js").glob("*.test.cjs"))
    assert files
    result = subprocess.run(
        ["node", "--test", *files], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
