"""共通HTTPクライアント: User-Agent 付与、キー別の最小間隔、429/5xx の指数バックオフ。"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from .config import RetryConfig

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SourceUnavailable(Exception):
    """再試行上限を超えた。呼び出し元はそのソースをスキップする。"""


class HttpStatusError(Exception):
    """再試行しない HTTP エラー（404 等）。"""

    def __init__(self, url: str, status: int):
        super().__init__(f"HTTP {status}: {url}")
        self.url = url
        self.status = status


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        retry: RetryConfig,
        timeout: float = 30.0,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.retry = retry
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def _wait_interval(self, key: str, interval: float) -> None:
        last = self._last.get(key)
        if last is not None:
            remaining = interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[key] = self._clock()

    def _backoff(self, attempt: int, resp: requests.Response | None) -> float:
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra and ra.strip().isdigit():
                return min(float(ra), self.retry.max_sec)
        return min(self.retry.base_sec * (2 ** (attempt - 1)), self.retry.max_sec)

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        rate_key: str,
        interval: float,
    ) -> requests.Response:
        """GET。同じ rate_key のリクエストは interval 秒以上空ける。

        429/5xx/接続エラーは指数バックオフで再試行し、上限を超えたら SourceUnavailable。
        それ以外の 4xx は HttpStatusError（再試行しない）。
        """
        attempts = self.retry.max_attempts
        for attempt in range(1, attempts + 1):
            self._wait_interval(rate_key, interval)
            resp: requests.Response | None = None
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRYABLE_STATUS:
                    raise HttpStatusError(resp.url or url, resp.status_code)
                reason = f"HTTP {resp.status_code}"

            if attempt == attempts:
                raise SourceUnavailable(f"{url}: {reason}（{attempts}回試行）")
            wait = self._backoff(attempt, resp)
            log.warning("%s: %s → %.0f秒後に再試行 (%d/%d)", url, reason, wait, attempt, attempts)
            self._sleep(wait)
        raise AssertionError("unreachable")
