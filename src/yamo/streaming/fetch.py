"""配信可否の探索範囲のページ取得 → テキスト化。

- robots.txt を尊重する（取得できなければ許可とみなす）
- SNS・リンク集系のホスト（config の excluded_hosts）には行かない（リダイレクト先も確認）
- HTML / テキスト以外は読まない。1ページ max_page_bytes まで
- 失敗はページ単位で握りつぶす（1サイトの不調で全体を止めない）
"""

from __future__ import annotations

import logging
import re
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from ..http import HttpClient, HttpStatusError, SourceUnavailable

log = logging.getLogger(__name__)

ROBOTS_AGENT = "yamo"
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
    "section", "article", "header", "footer", "blockquote", "pre", "dd", "dt", "hr", "table",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _TextExtractor(HTMLParser):
    """本文テキストを取り出す。only_class を指定すると、そのクラスを持つ要素の中だけを読む。"""

    def __init__(self, only_class: str | None = None):
        super().__init__(convert_charrefs=True)
        self.only_class = only_class
        self.parts: list[str] = []
        self.skip_depth = 0
        self.capture_depth = 0 if only_class else 1
        self.found_class = False

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            if tag in _BLOCK_TAGS:
                self.parts.append("\n")
            return
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.only_class:
            classes = (dict(attrs).get("class") or "").split()
            if self.capture_depth:
                self.capture_depth += 1
            elif self.only_class in classes:
                self.capture_depth = 1
                self.found_class = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.only_class and self.capture_depth:
            self.capture_depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth == 0 and self.capture_depth:
            self.parts.append(data)


def html_to_text(html: str, only_class: str | None = None) -> str:
    """HTML → テキスト。only_class の要素が見つからなければ、ページ全体を読む。"""
    p = _TextExtractor(only_class)
    p.feed(html)
    p.close()
    if only_class and not p.found_class:
        return html_to_text(html)
    lines = (" ".join(line.split()) for line in "".join(p.parts).split("\n"))
    return "\n".join(line for line in lines if line)


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


def decode_body(body: bytes, content_type: str) -> str:
    """ヘッダーの charset → <meta charset> → UTF-8 の順で決める。

    requests はヘッダーに charset がない HTML を ISO-8859-1 とみなすので、それには頼らない。
    """
    candidates = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    m2 = _META_CHARSET.search(body[:4096])
    if m2:
        candidates.append(m2.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for enc in candidates:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def host_excluded(url: str, excluded: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in excluded)


def candidate_urls(website: str, paths: list[str]) -> list[str]:
    """公式サイトURL と、そのオリジンに決め打ちパスを付けたもの。"""
    u = urlparse(website)
    if u.scheme not in ("http", "https") or not u.hostname:
        return []
    origin = f"{u.scheme}://{u.netloc}"
    urls = [website]
    for path in paths:
        url = urljoin(origin + "/", path.lstrip("/"))
        if url.rstrip("/") not in {x.rstrip("/") for x in urls}:
            urls.append(url)
    return urls


class PageFetcher:
    def __init__(self, client: HttpClient, *, interval: float, excluded_hosts: list[str], max_bytes: int):
        self.client = client
        self.interval = interval
        self.excluded = [h.lower() for h in excluded_hosts]
        self.max_bytes = max_bytes
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _rate_key(self, url: str) -> str:
        return "web:" + (urlparse(url).hostname or "")

    def _allowed_by_robots(self, url: str) -> bool:
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self._robots:
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                resp = self.client.get(origin + "/robots.txt", rate_key=self._rate_key(url), interval=self.interval)
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text[: self.max_bytes].splitlines())
            except (HttpStatusError, SourceUnavailable, ValueError):
                parser = None  # robots.txt がない・取れない → 制限なしとみなす
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(ROBOTS_AGENT, url)

    def get_text(self, url: str, only_class: str | None = None) -> str | None:
        """ページのテキスト。取れない・対象外なら None。"""
        if host_excluded(url, self.excluded):
            return None
        try:
            if not self._allowed_by_robots(url):
                log.info("robots.txt により取得しない: %s", url)
                return None
            resp = self.client.get(url, rate_key=self._rate_key(url), interval=self.interval, stream=True)
        except (HttpStatusError, SourceUnavailable, ValueError) as e:
            log.info("取得できない: %s (%s)", url, e)
            return None
        try:
            if host_excluded(resp.url or url, self.excluded):
                return None  # 除外ホストへのリダイレクト
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and not ("html" in ctype or ctype.startswith("text/")):
                return None
            body = b""
            for chunk in resp.iter_content(chunk_size=65536):
                body += chunk
                if len(body) >= self.max_bytes:
                    body = body[: self.max_bytes]
                    break
        except Exception as e:  # noqa: BLE001 — 外部サイトの不調はページ単位で無視する
            log.info("読み込み失敗: %s (%s)", url, e)
            return None
        finally:
            resp.close()
        text = decode_body(body, ctype)
        if "html" in ctype or text.lstrip()[:1] == "<":
            return html_to_text(text, only_class)
        return text
