"""ソース横断で使うデータ型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Status = Literal["ok", "partial", "failed", "skipped"]


@dataclass
class SourceStatus:
    name: str
    status: Status = "ok"
    count: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        self.status = "failed"

    def partial(self, msg: str) -> None:
        self.errors.append(msg)
        if self.status == "ok":
            self.status = "partial"

    def to_dict(self) -> dict:
        return asdict(self)
