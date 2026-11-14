import pytest

from yamo.merge import merge, names_close, normalize_name, normalize_title


@pytest.mark.parametrize("a,b", [
    ("Quiet Hallway™", "quiet hallway"),
    ("QUIET HALLWAY [Free] [Horror]", "Quiet Hallway"),
    ("Quiet Hallway - Demo", "Quiet Hallway"),
    ("Quiet Hallway (Prologue)", "Quiet Hallway"),
    ("Ｑｕｉｅｔ　Ｈａｌｌｗａｙ", "Quiet Hallway"),  # 全角
    ("🟥The Penjikent Creature🟥🕹️", "The Penjikent Creature"),
    ("Quiet: Hallway!", "Quiet Hallway"),
    ("静かな廊下", "静かな 廊下"),
])
def test_normalize_title_equal(a, b):
    assert normalize_title(a) == normalize_title(b)


@pytest.mark.parametrize("a,b", [
    ("Quiet Hallway", "Quiet Hallway 2"),
    ("Hallway", "Quiet Hallway"),
])
def test_normalize_title_different(a, b):
    assert normalize_title(a) != normalize_title(b)


def test_normalize_name_drops_noise_words_only():
    assert normalize_name("Tiny Ghost Games, LLC") == "tinyghost"
    assert normalize_name("tiny-ghost-games") == "tinyghost"
    assert normalize_name("Scott Cawthon") == "scottcawthon"  # 単語の途中の 'co' は消さない
    assert normalize_name("Games Studio") == "gamesstudio"  # 全部ノイズ語なら元のまま


@pytest.mark.parametrize("dev,author,expected", [
    ("Tiny Ghost Games", "tinyghost", True),
    ("Tiny Ghost", "tiny-ghost-games", True),
    ("Puck Redflix", "puckredflix", True),
    ("DeepLook Games", "deeplookgames", True),
    ("Big Studio Inc.", "someone-else", False),
    ("AB", "abc", False),  # 2文字の包含だけでは一致にしない
])
def test_names_close(dev, author, expected):
    assert names_close(dev, author, 0.8) is expected


def steam(id_, title, devs, cats):
    return {"id": id_, "title": title, "developers": devs, "categories": dict(cats), "score": max(cats.values())}


def itch_item(id_, title, author, cats):
    return {"id": id_, "title": title, "author": author, "url": f"https://{author}.itch.io/x",
            "categories": dict(cats), "score": max(cats.values())}


def test_merge_requires_title_and_developer():
    s = [steam("steam:1", "Quiet Hallway", ["Tiny Ghost"], {"horror": 80})]
    i = [
        itch_item("itch:tiny-ghost/quiet-hallway", "Quiet Hallway [Free]", "tiny-ghost", {"horror": 60}),
        itch_item("itch:other/quiet-hallway", "Quiet Hallway", "someone-else", {"horror": 55}),  # 同名の別作品
        itch_item("itch:tiny-ghost/other", "Another Game", "tiny-ghost", {"horror": 50}),
    ]
    out, merged = merge(s, i, 0.8)
    assert merged == 1
    assert [x["id"] for x in out] == ["steam:1", "itch:other/quiet-hallway", "itch:tiny-ghost/other"]
    assert out[0]["aliases"] == ["itch:tiny-ghost/quiet-hallway"]
    assert out[0]["itch_url"] == "https://tiny-ghost.itch.io/x"
    assert out[0]["categories"] == {"horror": 80}  # Steam のスコアを優先


def test_merge_adds_itch_only_category():
    s = [steam("steam:1", "Quiet Hallway", ["Tiny Ghost"], {"horror": 80})]
    i = [itch_item("itch:tiny-ghost/quiet-hallway", "Quiet Hallway", "tiny-ghost", {"horror": 60, "bakage": 90})]
    out, _ = merge(s, i, 0.8)
    assert out[0]["categories"] == {"horror": 80, "bakage": 90}
    assert out[0]["score"] == 90
