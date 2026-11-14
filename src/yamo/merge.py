"""Steam と itch.io の同一作品の統合。

統合するのは「正規化タイトルが一致」かつ「Steam の開発者名と itch の作者名が近い」場合のみ。
タイトルだけで統合すると、同名の別作品（短い一般名詞のタイトルは多い）を誤って1つにしてしまうため。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# タイトル末尾の版表記（統合時は同じ作品とみなす）
_EDITION_RE = re.compile(
    r"\b(demo|prologue|prototype|jam\s*version|jam\s*edition|alpha|beta|early\s*access|"
    r"remastered|definitive\s*edition|director'?s\s*cut)\b"
)
_BRACKETS_RE = re.compile(r"[\[(（【][^\])）】]*[\])）】]")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z぀-ヿ㐀-鿿]")
# 開発者名でよく付く語（比較の邪魔になる）。単語単位で取り除く
_NAME_NOISE = {
    "game", "games", "studio", "studios", "team", "dev", "devs", "development", "interactive",
    "entertainment", "software", "production", "productions", "llc", "inc", "ltd", "co", "corp", "official",
}
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z぀-ヿ㐀-鿿]+")


def normalize_title(title: str) -> str:
    """NFKC → 小文字 → ™®© と括弧内（[Free] [Horror] など）を除去 → 版表記を除去 → 記号・空白を除去。"""
    # NFKC は ™ を "TM" に変えてしまうので、先に取り除く
    s = (title or "").replace("™", "").replace("®", "").replace("©", "")
    s = unicodedata.normalize("NFKC", s).lower()
    s = _BRACKETS_RE.sub(" ", s)
    s = _EDITION_RE.sub(" ", s)
    return _NON_ALNUM_RE.sub("", s)


def normalize_name(name: str) -> str:
    """'Tiny Ghost Games, LLC' と 'tiny-ghost-games' → どちらも 'tinyghost'。"""
    s = unicodedata.normalize("NFKC", name or "").lower()
    tokens = [t for t in _TOKEN_SPLIT_RE.split(s) if t]
    kept = [t for t in tokens if t not in _NAME_NOISE]
    return "".join(kept or tokens)  # 全部ノイズ語なら元のまま


def names_close(a: str, b: str, threshold: float) -> bool:
    """開発者名と作者名が近いか。完全一致、または（4文字以上で）包含関係か類似度が閾値以上。"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    if len(shorter) < 4:
        return False  # 短い名前は偶然似やすいので、完全一致以外は認めない
    if shorter in longer:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def find_match(itch_item: dict[str, Any], steam_by_title: dict[str, list[dict[str, Any]]], threshold: float):
    """itch 作品に対応する Steam 作品を探す。なければ None。"""
    key = normalize_title(itch_item["title"])
    if not key:
        return None
    author = itch_item.get("author") or next(iter(itch_item.get("developers") or []), "")
    for steam in steam_by_title.get(key, []):
        if any(names_close(dev, author, threshold) for dev in steam.get("developers") or []):
            return steam
    return None


def merge(
    steam_items: list[dict[str, Any]], itch_items: list[dict[str, Any]], threshold: float,
) -> tuple[list[dict[str, Any]], int]:
    """itch 作品を、対応する Steam 作品に統合する。

    統合された itch 作品は一覧から消え、Steam 作品側に itch の URL と ID（aliases）が付く。
    itch 側にしかないカテゴリは、itch のスコアでその Steam 作品に追加する。
    戻り値: (統合後の一覧, 統合件数)
    """
    by_title: dict[str, list[dict[str, Any]]] = {}
    for s in steam_items:
        by_title.setdefault(normalize_title(s["title"]), []).append(s)

    remaining: list[dict[str, Any]] = []
    merged = 0
    for it in itch_items:
        steam = find_match(it, by_title, threshold)
        if steam is None:
            remaining.append(it)
            continue
        merged += 1
        steam.setdefault("aliases", []).append(it["id"])
        steam["itch_url"] = it["url"]
        for key, score in it["categories"].items():
            steam["categories"].setdefault(key, score)
        steam["score"] = max(steam["categories"].values())
    return steam_items + remaining, merged
