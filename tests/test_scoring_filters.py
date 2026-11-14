import datetime as dt

from yamo import filters
from yamo.config import WeightsConfig
from yamo.scoring import recency_score, steam_score, volume_score, wilson_lower

TODAY = dt.date(2026, 11, 15)


def test_wilson_penalizes_small_samples():
    assert wilson_lower(5, 5) < wilson_lower(95, 100)
    assert wilson_lower(0, 0) == 0


def test_volume_peaks_at_sweet_spot():
    peak = volume_score(500, 500, 0.5)
    assert peak == 1.0
    assert volume_score(50, 500, 0.5) < peak
    assert volume_score(200000, 500, 0.5) < volume_score(5000, 500, 0.5)  # 多すぎは減点
    assert volume_score(0, 500, 0.5) == 0


def test_recency_half_life():
    two_years_ago = (TODAY - dt.timedelta(days=round(365.25 * 2))).isoformat()
    assert abs(recency_score(two_years_ago, TODAY, 2.0) - 0.5) < 0.01
    assert recency_score(None, TODAY, 2.0) == 0.5


def test_mid_volume_beats_blockbuster():
    w = WeightsConfig()
    common = dict(release_date="2024-01-01", tags=set(), weights=w, bonus={}, today=TODAY)
    mid = steam_score(positive=480, negative=20, **common)
    huge = steam_score(positive=190000, negative=10000, **common)
    assert mid > huge


def test_bonus_tags_are_added():
    w = WeightsConfig()
    common = dict(positive=480, negative=20, release_date="2024-01-01", weights=w,
                  bonus={"Short": 5, "Online Co-Op": 8}, today=TODAY)
    assert steam_score(tags={"Short"}, **common) - steam_score(tags=set(), **common) == 5


def _details(**kw):
    d = {"unavailable": False, "type": "game", "price_jpy": 520, "has_demo": False,
         "languages": {"japanese": False, "english": True}, "release_date": "2024-03-12",
         "coming_soon": False}
    d.update(kw)
    return d


def test_final_filter(cfg):
    cat = cfg.categories["horror"]
    ff = lambda **kw: filters.final_filter(_details(**kw), 950, 50, cat, cfg.defaults)  # noqa: E731
    assert ff() == (True, "")
    assert ff(price_jpy=2500)[1] == "価格上限超過"
    assert ff(price_jpy=None)[1] == "価格不明"
    assert ff(languages={"japanese": False, "english": False})[1] == "対応言語外"
    assert ff(coming_soon=True)[1] == "未発売"
    assert ff(release_date="2012-01-01")[1] == "発売日が古い"
    assert ff(type="dlc")[1] == "種別 dlc"
    assert ff(unavailable=True)[1] == "ストア情報なし"


def test_final_filter_require_demo(cfg):
    cat = cfg.categories["horror"].model_copy(deep=True)
    cat.filters.require_demo = True
    assert filters.final_filter(_details(), 950, 50, cat, cfg.defaults)[1] == "デモなし"
    assert filters.final_filter(_details(has_demo=True), 950, 50, cat, cfg.defaults) == (True, "")
