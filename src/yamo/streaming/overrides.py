"""overrides.yaml: 配信可否の手動確定。自動判定より優先し、ページには「手動確認済み」と表示する。

GitHub の Web 画面で編集する前提なので、1行の書き間違いで更新全体を止めない。
不正な行はその行だけ読み飛ばし、ページの「警告」に理由を出す。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

ID_PATTERN = r"^(steam:\d+|itch:[a-z0-9_-]+/[A-Za-z0-9_.-]+)$"


class Override(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    verdict: Literal["ok", "conditional", "ng", "unknown"]
    evidence_url: str | None = None
    checked_on: dt.date
    note: str | None = Field(None, max_length=500)

    @field_validator("evidence_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("https://", "http://")):
            raise ValueError("evidence_url は http(s):// で始まる URL にしてください")
        return v

    def to_streaming(self) -> dict[str, Any]:
        """item["streaming"] の形に変換する。"""
        evidence = []
        if self.evidence_url or self.note:
            evidence.append({"url": self.evidence_url, "excerpt": self.note or "", "matched": []})
        return {
            "verdict": self.verdict,
            "method": "manual",
            "checked_at": f"{self.checked_on.isoformat()}T00:00:00Z",
            "checked_urls": [self.evidence_url] if self.evidence_url else [],
            "evidence": evidence,
        }


class _NoDatesLoader(yaml.SafeLoader):
    """日付を文字列のまま読む SafeLoader。

    標準の SafeLoader は 2026-13-40 のような存在しない日付で読み込み自体が失敗し、
    ファイル全体が読めなくなる。日付の検証は行ごとに pydantic で行う。
    """


_NoDatesLoader.yaml_implicit_resolvers = {
    k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _describe(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "(行全体)"
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


def load_overrides(path: str | Path) -> tuple[dict[str, Override], list[str]]:
    """overrides.yaml を読む。戻り値: (ID→手動確定, 警告)。ファイルがなければ空。"""
    p = Path(path)
    if not p.exists():
        return {}, []
    try:
        raw = yaml.load(p.read_text(encoding="utf-8"), Loader=_NoDatesLoader)  # noqa: S506 — SafeLoader 派生
    except yaml.YAMLError as e:
        return {}, [f"overrides.yaml を読めませんでした（YAML の書式エラー）: {e}"]
    if raw is None:
        return {}, []
    if not isinstance(raw, list):
        return {}, ["overrides.yaml は「- id: ...」で始まる項目のリストにしてください"]

    result: dict[str, Override] = {}
    warnings: list[str] = []
    for n, row in enumerate(raw, start=1):
        label = f"overrides.yaml の {n}件目"
        if isinstance(row, dict) and row.get("id"):
            label += f"（{row.get('id')}）"
        if not isinstance(row, dict):
            warnings.append(f"{label}: 項目の書式が正しくないため無視しました")
            continue
        try:
            ov = Override.model_validate(row)
        except ValidationError as e:
            warnings.append(f"{label}: {_describe(e)} のため無視しました")
            continue
        if ov.id in result:
            warnings.append(f"{label}: ID {ov.id} が重複しています（後に書かれた方を使います）")
        result[ov.id] = ov
    log.info("overrides: %d件読み込み、警告 %d件", len(result), len(warnings))
    return result, warnings


def apply_overrides(items: list[dict[str, Any]], overrides: dict[str, Override]) -> tuple[int, list[str]]:
    """掲載作品に手動確定を反映する。統合された itch 作品の ID（aliases）でも当たる。

    戻り値: (反映件数, 掲載中の作品に当たらなかった ID)
    """
    used: set[str] = set()
    applied = 0
    for item in items:
        for key in [item["id"], *item.get("aliases", [])]:
            if key in overrides:
                item["streaming"] = overrides[key].to_streaming()
                used.add(key)
                applied += 1
                break
    unused = sorted(set(overrides) - used)
    return applied, unused


def overridden_ids(items: list[dict[str, Any]], overrides: dict[str, Override]) -> set[str]:
    """手動確定がある作品の ID（自動判定の対象から外す）。"""
    return {i["id"] for i in items if any(k in overrides for k in [i["id"], *i.get("aliases", [])])}
