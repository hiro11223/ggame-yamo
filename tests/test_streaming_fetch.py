import re

import pytest
import responses

from yamo.config import RetryConfig
from yamo.streaming.fetch import PageFetcher, candidate_urls, decode_body, host_excluded, html_to_text

EXCLUDED = ["x.com", "linktr.ee", "youtube.com"]


def test_html_to_text_skips_scripts_and_keeps_blocks():
    html = ("<html><head><title>T</title><style>.a{}</style></head><body>"
            "<script>var stream='prohibited'</script><h1>Press</h1><p>Feel free to <b>stream</b>.</p>"
            "<ul><li>One</li><li>Two</li></ul><br>End</body></html>")
    assert html_to_text(html) == "Press\nFeel free to stream.\nOne\nTwo\nEnd"


def test_html_to_text_only_class_and_fallback():
    html = ('<div class="header">Nav stream</div>'
            '<div class="formatted_description user_formatted"><p>Streaming <img src=x> is welcome</p>'
            '<div><p>nested</p></div></div><div class="comments">Do not stream</div>')
    assert html_to_text(html, "formatted_description") == "Streaming is welcome\nnested"
    # クラスが見つからなければページ全体
    assert "Do not stream" in html_to_text(html, "no_such_class")


def test_decode_body():
    ja = "配信OK"
    assert decode_body(ja.encode("utf-8"), "text/html") == ja  # charset なし → UTF-8（ISO-8859-1 にしない）
    sjis = b'<meta charset="shift_jis">' + ja.encode("shift_jis")
    assert ja in decode_body(sjis, "text/html")
    assert decode_body(ja.encode("euc-jp"), "text/html; charset=EUC-JP") == ja


def test_candidate_urls():
    assert candidate_urls("https://studio.example/games/foo", ["/press", "/faq"]) == [
        "https://studio.example/games/foo", "https://studio.example/press", "https://studio.example/faq"]
    assert candidate_urls("https://studio.example/", ["/press", "/"]) == [
        "https://studio.example/", "https://studio.example/press"]
    assert candidate_urls("javascript:alert(1)", ["/press"]) == []


def test_host_excluded():
    assert host_excluded("https://x.com/dev", EXCLUDED)
    assert host_excluded("https://www.youtube.com/@dev", EXCLUDED)
    assert not host_excluded("https://notx.com/", EXCLUDED)


@pytest.fixture
def fetcher(client):
    web = client.derive(RetryConfig(max_attempts=2, base_sec=1, max_sec=2), 10)
    return PageFetcher(web, interval=2, excluded_hosts=EXCLUDED, max_bytes=20_000)


@responses.activate
def test_robots_disallow_is_respected(fetcher):
    responses.get("https://s.example/robots.txt", body="User-agent: *\nDisallow: /private\n")
    responses.get("https://s.example/press", body="<p>Feel free to stream</p>", content_type="text/html")
    assert fetcher.get_text("https://s.example/private/faq") is None
    assert fetcher.get_text("https://s.example/press") == "Feel free to stream"
    # robots.txt は1ホスト1回だけ
    assert sum(1 for c in responses.calls if c.request.url.endswith("robots.txt")) == 1
    assert not any("/private" in c.request.url for c in responses.calls)


@responses.activate
def test_missing_robots_means_allowed_and_404_page_is_none(fetcher):
    responses.get("https://s.example/robots.txt", status=404)
    responses.get("https://s.example/faq", status=404)
    responses.get("https://s.example/", body="<p>hello</p>", content_type="text/html; charset=utf-8")
    assert fetcher.get_text("https://s.example/faq") is None
    assert fetcher.get_text("https://s.example/") == "hello"


@responses.activate
def test_excluded_host_is_never_requested(fetcher):
    assert fetcher.get_text("https://linktr.ee/dev") is None
    assert len(responses.calls) == 0


@responses.activate
def test_redirect_to_excluded_host_is_ignored(fetcher):
    responses.get("https://s.example/robots.txt", status=404)
    responses.get("https://s.example/links", status=302, headers={"Location": "https://linktr.ee/dev"})
    responses.get("https://linktr.ee/dev", body="<p>Feel free to stream</p>", content_type="text/html")
    assert fetcher.get_text("https://s.example/links") is None


@responses.activate
def test_non_html_and_size_limit(fetcher):
    responses.get("https://s.example/robots.txt", status=404)
    responses.get("https://s.example/kit.zip", body=b"PK\x03\x04", content_type="application/zip")
    responses.get("https://s.example/big", body="<p>" + "a" * 50_000 + " stream</p>", content_type="text/html")
    assert fetcher.get_text("https://s.example/kit.zip") is None
    text = fetcher.get_text("https://s.example/big")
    assert text is not None and len(text) <= 20_000 and "stream" not in text  # 上限で打ち切り


@responses.activate
def test_per_host_interval(fetcher, fake_clock):
    responses.get("https://a.example/robots.txt", status=404)
    responses.get("https://b.example/robots.txt", status=404)
    responses.get(re.compile(r"https://[ab]\.example/p\d"), body="<p>x</p>", content_type="text/html")
    fetcher.get_text("https://a.example/p1")
    fetcher.get_text("https://b.example/p1")  # 別ホストは待たない
    fetcher.get_text("https://a.example/p2")
    # 同じホストの連続アクセス（robots → p1）でだけ待つ。a の p2 は b を取っている間に間隔が空いたので待たない
    assert fake_clock.sleeps == [2, 2]


@responses.activate
def test_redirect_loop_page_is_none(fetcher):
    # robots.txt もページもリダイレクトがループするサイトで、実行全体を落とさない
    responses.get("https://loop.example/robots.txt", status=302, headers={"Location": "https://loop.example/robots.txt"})
    responses.get("https://loop.example/press", status=302, headers={"Location": "https://loop.example/press"})
    assert fetcher.get_text("https://loop.example/press") is None
