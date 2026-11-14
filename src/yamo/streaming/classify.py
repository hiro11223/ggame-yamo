"""配信可否の自動判定（文言 → 判定）。外部に依存しない純粋関数。

規則（doc/PLAN.md 7章）:
- 「配信系の語（トリガー）」を含む文だけを判定対象にする
  - 英語は単語境界つき（mainstream / streamline は対象外）。YouTube / Twitch 単独はトリガーにしない
  - 日本語の「配信日」「配信開始」「配信予定」「DL配信」等は、販売の意味なので先に取り除く
- トリガー文の前後1文（窓）に禁止語 → NG
  - ただし同じ文に「without permission / unless / 無断 / 許可なく」があれば「許可を取れば可」なので条件付き
- 窓に条件語（要許可・非商用・収益化不可・クレジット表記 等）→ 条件付き
  - 「without asking permission」「許可不要」のように条件語が否定されている場合は数えない
- トリガー語と許可語が「同じ文の中」にある → OK（窓は使わない）
  - 単独の "OK" / "welcome" は許可語にしない（複合表現のみ）
- それ以外は不明。複数の根拠があれば最も厳しい判定（NG > 条件付き > OK）を採用する
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

SEVERITY = {"unknown": 0, "ok": 1, "conditional": 2, "ng": 3}
EXCERPT_LIMIT = 200
MAX_EVIDENCE = 3

_I = re.IGNORECASE

TRIGGER = re.compile(
    r"\b(?:live-?)?stream(?:s|ing|ed|er|ers)?\b"
    r"|\blet'?s ?plays?\b"
    r"|\bcontent creators?\b"
    r"|\bmonetis(?:e|ed|es|ing|ation)\b|\bmonetiz(?:e|ed|es|ing|ation)\b"
    r"|\bvideos? (?:of|about) (?:the|this|our) game\b"
    r"|配信|実況|収益化",
    _I,
)
# 「配信」が販売・提供の意味で使われる語（トリガー判定の前に取り除く）
JA_NOT_STREAMING = re.compile(r"配信日|配信開始|配信予定|dl配信|ダウンロード配信|配信中|配信元|配信版|配信サービス", _I)

# 条件語の否定（「許可不要」など）。条件語の判定の前に取り除く
NEGATED_CONDITION = re.compile(
    r"without asking(?: us)?(?: for)?(?: any)? permission"
    r"|no need to ask(?: us)?(?: for(?: any)? permission)?"
    r"|(?:do not|don't|doesn't|does not) need(?: to ask)?(?: us)?(?: for)?(?: any)? permission"
    r"|no permission (?:is )?(?:needed|required|necessary)"
    r"|許可(?:は|も)?(?:不要|いりません|要りません|必要ありません)",
    _I,
)
# 収益化だけを禁じる表現は「条件付き」（配信自体は可）
MONETIZATION_LIMIT = re.compile(
    r"\b(?:not|cannot|can't|don't|do not|may not|must not|no)\s+(?:\w+\s+){0,2}monetiz\w*"
    r"|\bnon-?monetiz\w*"
    r"|\bmonetiz\w* (?:of \w+ )?(?:is|are) (?:\w+ )?(?:not allowed|not permitted|prohibited|forbidden)"
    r"|収益化(?:は|を|の|も)?(?:不可|できません|ng|禁止|ご遠慮|お控え|しないで|不許可)",
    _I,
)
CONDITION = re.compile(
    r"\bwithout (?:\w+ ){0,2}permission\b"
    r"|\bask(?:ing)? (?:us )?(?:for )?(?:our )?permission\b"
    r"|\b(?:get|obtain|request|need) (?:our |prior |written |explicit )?permission\b"
    r"|\bcontact us (?:first|before|beforehand)\b"
    r"|\bnon-?commercial\b"
    r"|\bplease credit\b|\bgive (?:us )?credit\b|\bcredit (?:us|the (?:game|developer|dev|studio))\b"
    r"|\bcredit (?:is|would be) appreciated\b|\bwith (?:a )?credit\b|\bcrediting\b"
    r"|\blink (?:back )?to (?:the|our) (?:store|steam|page|game)\b"
    r"|要許可|許可が必要|事前(?:に|の)?(?:ご)?(?:連絡|申請|許可)|クレジット表記|クレジットを(?:記載|表記|明記)",
    _I,
)
PROHIBITION = re.compile(
    r"\bnot allowed\b|\bprohibited\b|\bforbidden\b"
    r"|\b(?:do not|don't|may not|must not|cannot|can't|are not permitted to|is not permitted to)\s+"
    r"(?:\w+\s+){0,2}(?:stream|record|upload|broadcast|make videos|let's play)"
    r"|禁止|ご遠慮(?:ください|下さい)|お控え(?:ください|下さい)"
    r"|(?:配信|実況)(?:は|を)?(?:できません|不可|ng)",
    _I,
)
# 禁止語と同じ文にあれば「許可を取れば可」＝条件付き
PROHIBITION_UNLESS = re.compile(
    r"\bwithout (?:\w+ ){0,2}permission\b|\bunless\b|無断|許可なく|許可のない|許可を得ず", _I,
)
PERMISSION = re.compile(
    r"\ballowed to (?:\w+ )?(?:stream|record|monetiz|make videos)"
    r"|\bfree to (?:stream|record|monetiz|make videos)"
    r"|\bfeel free to (?:stream|record|monetiz|make (?:videos|content)|(?:do|make) (?:a )?let's play)"
    r"|\bwelcome to (?:stream|record|monetiz|make videos)"
    r"|\b(?:streaming|streams|streamers|let's plays?|videos?|content creators?|monetization)"
    r" (?:are|is) (?:\w+ )?(?:welcome|allowed|encouraged|permitted)\b"
    r"|\byou (?:can|may) (?:\w+ )?(?:stream|monetiz|record|make videos)"
    r"|\bpermission (?:is )?(?:hereby )?granted\b"
    r"|\bwe encourage (?:\w+ ){0,2}(?:stream|let's play|videos|content creators)"
    r"|(?:配信|実況)(?:・(?:配信|実況))?(?:ok|歓迎|自由)"
    r"|(?:配信|実況)[^。!?]{0,20}(?:構いません|かまいません|大丈夫です|問題ありません)"
    r"|収益化(?:も)?(?:ok|可)",
    _I,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[。])|\n+")


@dataclass
class Evidence:
    url: str
    excerpt: str
    matched: list[str]


@dataclass
class Result:
    verdict: str = "unknown"
    evidence: list[Evidence] = field(default_factory=list)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    return text


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(normalize(text)) if s and s.strip()]


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= EXCERPT_LIMIT else text[: EXCERPT_LIMIT - 1] + "…"


def _has_trigger(sentence: str) -> bool:
    return bool(TRIGGER.search(JA_NOT_STREAMING.sub(" ", sentence)))


def classify_sentence(sentences: list[str], i: int) -> tuple[str, list[str], str]:
    """i 番目（トリガーを含む文）を判定する。戻り値: (判定, 一致した語, 抜粋の元テキスト)"""
    sentence = sentences[i]
    window_list = sentences[max(0, i - 1): i + 2]
    window = " ".join(window_list)

    # 条件語の否定を先に消す（"without asking permission" を条件語として数えない）
    window_c = NEGATED_CONDITION.sub(" ", window)
    sentence_c = NEGATED_CONDITION.sub(" ", sentence)

    # 収益化だけの制限は条件付き。禁止語の判定からは外す
    monet = [m.group(0) for m in MONETIZATION_LIMIT.finditer(window_c)]
    window_np = MONETIZATION_LIMIT.sub(" ", window_c)

    for m in PROHIBITION.finditer(window_np):
        # 禁止語を含む文に「無断で / without permission / unless」があれば条件付き
        owner = next((s for s in window_list if m.group(0) in NEGATED_CONDITION.sub(" ", s)), sentence)
        if PROHIBITION_UNLESS.search(owner):
            return "conditional", [m.group(0), PROHIBITION_UNLESS.search(owner).group(0)], window
        return "ng", [m.group(0)], window

    cond = monet + [m.group(0) for m in CONDITION.finditer(window_c)]
    if cond:
        return "conditional", cond, window

    ok = [m.group(0) for m in PERMISSION.finditer(sentence_c)]
    if ok:
        return "ok", ok, sentence
    return "unknown", [], sentence


def classify_text(text: str) -> tuple[str, list[tuple[list[str], str]]]:
    """1ページ分のテキストを判定。戻り値: (判定, [(一致した語, 抜粋), ...]（その判定の根拠のみ）)"""
    sentences = split_sentences(text)
    best = "unknown"
    hits: dict[str, list[tuple[list[str], str]]] = {}
    for i, s in enumerate(sentences):
        if not _has_trigger(s):
            continue
        verdict, matched, source = classify_sentence(sentences, i)
        if verdict == "unknown":
            continue
        hits.setdefault(verdict, []).append(([t.lower() for t in matched], _excerpt(source)))
        if SEVERITY[verdict] > SEVERITY[best]:
            best = verdict
    return best, hits.get(best, [])


def classify(pages: list[tuple[str, str]]) -> Result:
    """複数ページ [(URL, テキスト)] をまとめて判定。最も厳しい判定を採用し、その根拠を返す。"""
    result = Result()
    for url, text in pages:
        verdict, hits = classify_text(text)
        if verdict == "unknown":
            continue
        if SEVERITY[verdict] > SEVERITY[result.verdict]:
            result = Result(verdict, [])
        if verdict == result.verdict:
            for matched, excerpt in hits:
                if len(result.evidence) < MAX_EVIDENCE:
                    result.evidence.append(Evidence(url, excerpt, sorted(set(matched))))
    return result
