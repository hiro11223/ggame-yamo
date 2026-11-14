from pathlib import Path

import pytest
import yaml

from yamo.streaming.classify import classify, classify_text, split_sentences

SAMPLES = yaml.safe_load((Path(__file__).parent / "fixtures" / "streaming_samples.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["text"][:40] for s in SAMPLES])
def test_samples(sample):
    verdict, _ = classify_text(sample["text"])
    assert verdict == sample["expect"], sample.get("note", "")


def test_required_samples_present():
    assert sum(1 for s in SAMPLES if s.get("required")) == 7
    assert len(SAMPLES) >= 30


def test_window_is_used_for_ng_but_not_for_ok():
    # 許可語が隣の文にあっても OK にはならない
    assert classify_text("We support streamers. You can stream it.")[0] == "ok"  # 2文目にトリガーと許可語の両方
    assert classify_text("Our game is great. Streaming? Feel free.")[0] == "unknown"
    # 禁止語は隣の文でも効く
    assert classify_text("About streaming. It is prohibited.")[0] == "ng"


def test_most_severe_wins_across_pages():
    pages = [
        ("https://a.example/", "Feel free to stream this game!"),
        ("https://a.example/faq", "Monetization is not allowed. You cannot monetize streams."),
    ]
    r = classify(pages)
    assert r.verdict == "conditional"
    assert all(e.url == "https://a.example/faq" for e in r.evidence)


def test_evidence_excerpt_is_short_and_has_matches():
    long_text = "Streaming is prohibited. " + "x" * 500
    r = classify([("https://a.example/", long_text)])
    assert r.verdict == "ng"
    ev = r.evidence[0]
    assert len(ev.excerpt) <= 200
    assert "prohibited" in ev.matched


def test_no_trigger_means_unknown_without_evidence():
    r = classify([("https://a.example/", "A cozy game about cats. Allowed to pet them.")])
    assert r.verdict == "unknown" and r.evidence == []


def test_sentence_split_handles_japanese_and_curly_quotes():
    assert split_sentences("配信OK。収益化は不可です。") == ["配信OK。", "収益化は不可です。"]
    assert classify_text("Feel free to make a Let’s Play!")[0] == "ok"
