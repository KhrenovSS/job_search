import json
from pathlib import Path

from hh_scout.config import BLOCKED_REGIONS, KNOWN_AREA_IDS, REGION_NAMES
from hh_scout.hh.areas import blocked_region, path_ids

FIXTURE = Path(__file__).parent / "fixtures" / "areas_russia.json"


def test_all_configured_regions_exist_in_hh_dictionary():
    names = {r["name"] for r in json.loads(FIXTURE.read_text(encoding="utf-8"))}
    missing = [n for n in REGION_NAMES if n not in names]
    assert not missing, f"регионы отсутствуют в справочнике hh: {missing}"
    assert len(REGION_NAMES) == len(set(REGION_NAMES)) == 48   # Крым out since decision #65


def test_control_ids():
    by_name = {r["name"]: r["area_id"] for r in json.loads(FIXTURE.read_text(encoding="utf-8"))}
    for name, expected in KNOWN_AREA_IDS.items():
        assert by_name[name] == expected
    # excluded on purpose
    for name in ("Мурманская область", "Свердловская область", "Республика Дагестан", "Донецкая Народная Республика",
                 "Республика Крым"):
        assert name in by_name and name not in REGION_NAMES


def test_blocked_regions_match_the_hh_dictionary_by_id_and_name():
    """Decision #65: the five regions are keyed by hh.ru area id; the names are the dictionary's, not ours."""
    by_id = {r["area_id"]: r["name"] for r in json.loads(FIXTURE.read_text(encoding="utf-8"))}
    for area_id, name in BLOCKED_REGIONS.items():
        assert by_id[area_id] == name
    assert set(BLOCKED_REGIONS.values()) == {"Республика Крым", "Донецкая Народная Республика", "Запорожская область",
                                             "Луганская Народная Республика", "Херсонская область"}
    # Sevastopol has no region of its own on hh.ru — it sits inside Crimea (path .113.225.2114.130.)
    assert "Севастополь" not in by_id.values()


def test_blocked_region_reads_the_path_by_region_id_not_by_position():
    assert path_ids(".113.225.2114.131.") == [113, 225, 2114, 131]
    assert path_ids(None) == [] and path_ids("") == [] and path_ids("abc") == []
    assert blocked_region(".113.225.2114.131.") == "Республика Крым"          # Симферополь, federal district in the path
    assert blocked_region(".113.225.2114.130.") == "Республика Крым"          # Севастополь
    assert blocked_region(".113.2173.123.") == "Луганская Народная Республика"  # no federal district in the path
    assert blocked_region(".2209.") == "Херсонская область"                    # built from `regionId` alone
    assert blocked_region(".113.232.1.") is None                               # Москва
    assert blocked_region(".113.226.1530.1543.") is None                       # Донецк (Ростовская область)
    assert blocked_region(None) is None                                         # an old row: never guessed by name
    assert blocked_region(".113.2173.123.", blocked={}) is None
