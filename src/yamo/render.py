"""静的サイトの出力: index.html（データ埋め込み）、app.js、style.css、data.json、robots.txt。

一覧の描画・並べ替え・絞り込みはブラウザ側（app.js）で行う。
作品名や説明文は外部由来なので、app.js 側で textContent として描画する（HTMLとして解釈しない）。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .state import StateStore, parse_iso

WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = ("app.js", "style.css")
JST = dt.timezone(dt.timedelta(hours=9), "JST")

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

STATUS_LABELS = {
    "ok": ("✅", "正常"),
    "partial": ("⚠️", "一部失敗"),
    "failed": ("❌", "失敗"),
    "skipped": ("⏭️", "未実行"),
}
SOURCE_LABELS = {"steam": "Steam", "itch": "itch.io", "streaming": "配信可否チェック"}


def embed_json(data: Any) -> Markup:
    """<script type="application/json"> に安全に埋め込める JSON 文字列。"""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # </script> や <!-- で script 要素が途中で閉じられないようにする
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return Markup(text)


def _jst(iso_str: str) -> str:
    return parse_iso(iso_str).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(WEB_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["jst"] = _jst
    return env


def render_index(payload: dict[str, Any]) -> str:
    items = payload["items"]
    counts = {key: sum(1 for i in items if key in i["categories"]) for key in payload["categories"]}
    sources = [
        {
            **s,
            "label": SOURCE_LABELS.get(s["name"], s["name"]),
            "icon": STATUS_LABELS.get(s["status"], ("", s["status"]))[0],
            "status_label": STATUS_LABELS.get(s["status"], ("", s["status"]))[1],
        }
        for s in payload["sources"]
    ]
    version = payload["generated_at"].replace(":", "").replace("-", "")
    return _env().get_template("index.html.j2").render(
        generated_at=payload["generated_at"],
        categories=payload["categories"],
        counts=counts,
        total=len(items),
        new_count=sum(1 for i in items if i.get("is_new")),
        stale_count=sum(1 for i in items if i.get("stale")),
        sources=sources,
        warnings=payload["warnings"],
        data_json=embed_json(payload),
        version=version,
    )


def write_site(payload: dict[str, Any], out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    StateStore(out).save("data.json", payload)
    (out / "index.html").write_text(render_index(payload), encoding="utf-8", newline="\n")
    for name in STATIC_FILES:
        shutil.copyfile(WEB_DIR / name, out / name)
    (out / "robots.txt").write_text(ROBOTS_TXT, encoding="utf-8", newline="\n")
    # Pages の Jekyll 処理を無効化（そのまま配信させる）
    (out / ".nojekyll").write_text("", encoding="utf-8")
