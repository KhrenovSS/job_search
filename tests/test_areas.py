import json
from pathlib import Path

from hh_scout.config import KNOWN_AREA_IDS, REGION_NAMES

FIXTURE = Path(__file__).parent / "fixtures" / "areas_russia.json"


def test_all_configured_regions_exist_in_hh_dictionary():
    names = {r["name"] for r in json.loads(FIXTURE.read_text(encoding="utf-8"))}
    missing = [n for n in REGION_NAMES if n not in names]
    assert not missing, f"регионы отсутствуют в справочнике hh: {missing}"
    assert len(REGION_NAMES) == len(set(REGION_NAMES)) == 49


def test_control_ids():
    by_name = {r["name"]: r["area_id"] for r in json.loads(FIXTURE.read_text(encoding="utf-8"))}
    for name, expected in KNOWN_AREA_IDS.items():
        assert by_name[name] == expected
    # excluded on purpose
    for name in ("Мурманская область", "Свердловская область", "Республика Дагестан", "Донецкая Народная Республика"):
        assert name in by_name and name not in REGION_NAMES
