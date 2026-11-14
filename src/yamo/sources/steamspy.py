"""SteamSpy: タグ別一覧で候補を収集する。

タグ別一覧（request=tag）は「一覧系」なので 1req/分 を守る。
各作品が持つタグは「どのタグ一覧に現れたか」から決める（作品ごとのAPI呼び出しをしない）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..http import HttpClient, HttpStatusError, SourceUnavailable
from ..models import SourceStatus

log = logging.getLogger(__name__)

API_URL = "https://steamspy.com/api.php"


@dataclass
class SpyApp:
    appid: int
    name: str
    developer: str = ""
    positive: int = 0
    negative: int = 0
    owners: str = ""
    price_usd_cents: int | None = None
    tags: set[str] = field(default_factory=set)

    @property
    def reviews(self) -> int:
        return self.positive + self.negative


def _int(v, default: int | None = 0) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_tag_response(text: str) -> dict[int, SpyApp]:
    data = json.loads(text)
    if not isinstance(data, dict):  # 存在しないタグでは [] が返る
        return {}
    apps: dict[int, SpyApp] = {}
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        appid = _int(row.get("appid", key), None)
        if appid is None:
            continue
        apps[appid] = SpyApp(
            appid=appid,
            name=str(row.get("name") or ""),
            developer=str(row.get("developer") or ""),
            positive=_int(row.get("positive")) or 0,
            negative=_int(row.get("negative")) or 0,
            owners=str(row.get("owners") or ""),
            price_usd_cents=_int(row.get("price"), None),
        )
    return apps


def collect(
    client: HttpClient, tags: list[str], list_interval: float
) -> tuple[dict[int, SpyApp], SourceStatus, list[str]]:
    """タグごとに一覧を取得し、作品単位にまとめる。

    戻り値: (appid→SpyApp, ステータス, 0件だったタグ)
    1タグの失敗は partial、全タグ失敗なら failed。
    """
    status = SourceStatus("steamspy")
    apps: dict[int, SpyApp] = {}
    empty: list[str] = []
    ok = 0
    for tag in tags:
        log.info("SteamSpy: タグ %r を取得", tag)
        try:
            resp = client.get(
                API_URL,
                params={"request": "tag", "tag": tag},
                rate_key="steamspy",
                interval=list_interval,
            )
            found = parse_tag_response(resp.text)
        except SourceUnavailable as e:
            # 再試行上限を超えた＝ソース自体が落ちている。残りのタグも諦める
            status.partial(f"タグ {tag}: {e}")
            break
        except (HttpStatusError, ValueError) as e:
            status.partial(f"タグ {tag}: {e}")
            continue
        ok += 1
        if not found:
            empty.append(tag)
        for appid, app in found.items():
            cur = apps.setdefault(appid, app)
            cur.tags.add(tag)
        log.info("SteamSpy: タグ %r → %d件", tag, len(found))

    if tags and ok == 0:
        status.status = "failed"
    status.count = len(apps)
    return apps, status, empty
