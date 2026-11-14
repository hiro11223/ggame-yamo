"""state ディレクトリ（data ブランチ）の JSON 読み書きと TTL 判定。"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_expired(fetched_at: str | None, ttl_days: int, now: dt.datetime) -> bool:
    if not fetched_at:
        return True
    try:
        return now - parse_iso(fetched_at) >= dt.timedelta(days=ttl_days)
    except ValueError:
        return True


class StateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def load(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, name: str, data: Any) -> None:
        """差分が読みやすいよう、キー順固定・1キー1行に近い形で書く。書き込みは原子的。"""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
