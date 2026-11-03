import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
from memorial import parse_list, parse_person  # noqa: E402
from post import age_words, build_post  # noqa: E402
from regions import DONETSK, LUHANSK, origin_regions  # noqa: E402

FX = ROOT / "tests" / "fixtures"


def read(name):
    return (FX / name).read_text(encoding="utf-8")


def test_list():
    cards, last = parse_list(read("list_militaries.html"), "militaries")
    assert last == 447
    assert [c["id"] for c in cards] == ["13383", "12368", "13367"]
    assert cards[0]["fields"]["Дата загибелі"] == "07 квітня 2026"
    assert cards[0]["fields"]["Місто загибелі"] == "м. Костянтинівка"
    # Дніпропетровщина — не наш, хоч загинув у Костянтинівці
    assert bot.card_regions(cards[0], {}) == []
    assert bot.card_regions(cards[1], {}) == [DONETSK]
    assert bot.card_regions(cards[2], {}) == [LUHANSK]


def test_origin_not_battle():
    assert origin_regions("Загинув у бою під Бахмутом. Родом з Полтави.") == set()
    assert origin_regions("Народилась та мешкала у місті Словʼянськ на Донеччині.") == {DONETSK}


def test_person_civilian():
    p = parse_person(read("person_civilian.html"), "/obituaries/civilians/konevska-olena-13229", "civilians")
    assert p.name == "Олена Коневська"
    assert p.photo.endswith(".png")
    assert p.f("Місто народження") == "м. Слов'янськ, Донецька область"
    assert len(p.story) == 5
    with mock.patch("bot.get_person", return_value=p):
        person, region = bot.verify({"url": p.url, "cat": "civilians"})
    assert region == DONETSK
    text = build_post(person, region)
    assert "<b>Олена Коневська, 41 рік</b>" in text
    assert "Слов'янськ · загинула 28 серпня 2026 року" in text
    assert "Сфера" not in text and "Торгівля" not in text
    assert "#Словянськ" in text
    print("\n" + text)


def test_military_from_kyiv_rejected():
    p = parse_person(read("person_military_kyiv.html"), "/obituaries/militaries/vozhdaienko-volodymyr-13428", "militaries")
    with mock.patch("bot.get_person", return_value=p):
        person, reason = bot.verify({"url": p.url, "cat": "militaries"})
    assert person is None, reason


def test_military_post_format():
    p = parse_person(read("person_military_kyiv.html"), "/x/y-1", "militaries")
    p.fields["Місто народження"] = ["м. Краматорськ, Донецька область"]
    text = build_post(p, DONETSK)
    assert "Володимир Вождаєнко, 54 роки" in text
    assert "Краматорськ · загинув 2 липня 2024 року" in text
    assert "Бойовий медик, 114-та окрема бригада" in text
    assert "memorial_ukraine" in text
    fb = build_post(p, DONETSK, html_mode=False, network="facebook")
    assert "memorialofheroes" in fb and "<b>" not in fb


def test_age_words():
    assert [age_words(n) for n in (1, 4, 11, 12, 21, 22, 25, 41, 54)] == [
        "1 рік", "4 роки", "11 років", "12 років", "21 рік", "22 роки", "25 років", "41 рік", "54 роки"]


def test_schedule():
    from datetime import date
    assert bot.today_plan(date(2026, 11, 3)) == ("militaries", LUHANSK)   # вівторок
    assert bot.today_plan(date(2026, 11, 8)) == ("children", DONETSK)     # неділя
