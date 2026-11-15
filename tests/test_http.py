import pytest
import responses

from yamo.http import HttpStatusError, SourceUnavailable

URL = "https://api.example.invalid/x"


@responses.activate
def test_user_agent_is_sent(client):
    responses.get(URL, json={})
    client.get(URL, rate_key="k", interval=0)
    assert responses.calls[0].request.headers["User-Agent"].startswith("yamo-test/")


@responses.activate
def test_interval_is_respected_per_key(client, fake_clock):
    responses.get(URL, json={})
    client.get(URL, rate_key="list", interval=60)
    client.get(URL, rate_key="list", interval=60)
    client.get(URL, rate_key="other", interval=60)  # 別キーは待たない
    assert fake_clock.sleeps == [60]


@responses.activate
def test_retry_429_with_backoff_then_success(client, fake_clock):
    responses.get(URL, status=429)
    responses.get(URL, status=503)
    responses.get(URL, json={"ok": True})
    resp = client.get(URL, rate_key="k", interval=0)
    assert resp.json() == {"ok": True}
    assert fake_clock.sleeps == [2, 4]  # base 2秒の指数バックオフ


@responses.activate
def test_retry_after_header_is_honored(client, fake_clock):
    responses.get(URL, status=429, headers={"Retry-After": "7"})
    responses.get(URL, json={})
    client.get(URL, rate_key="k", interval=0)
    assert fake_clock.sleeps == [7]


@responses.activate
def test_exhausted_retries_raise_source_unavailable(client):
    responses.get(URL, status=500)
    with pytest.raises(SourceUnavailable):
        client.get(URL, rate_key="k", interval=0)
    assert len(responses.calls) == 3


@responses.activate
def test_404_is_not_retried(client):
    responses.get(URL, status=404)
    with pytest.raises(HttpStatusError) as e:
        client.get(URL, rate_key="k", interval=0)
    assert e.value.status == 404
    assert len(responses.calls) == 1


@responses.activate
def test_redirect_loop_raises_source_unavailable_without_retry(client):
    responses.get(URL, status=302, headers={"Location": URL})
    with pytest.raises(SourceUnavailable):
        client.get(URL, rate_key="k", interval=0)
    assert len(responses.calls) <= 31  # 1回分のリダイレクト追跡だけで、再試行はしない
