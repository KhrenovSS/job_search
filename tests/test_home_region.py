"""Home regions (v9.43, decision #77): while the owner is away, leads come only from the central regions."""
import json
import random
from pathlib import Path

from hh_scout.config import HOME_EXTRA_PLACES_MOSCOW, HOME_REGIONS_CENTRAL, Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import home_region, repo
from hh_scout.pipeline.prefilter import CardFacts, decide

CENTRAL = ", ".join(HOME_REGIONS_CENTRAL)


def _settings(**kw):
    return Settings(_env_file=None, home_regions=CENTRAL, home_extra_places=", ".join(HOME_EXTRA_PLACES_MOSCOW), **kw)


def _names(node, out):
    if isinstance(node, dict):
        if "name" in node:
            out.add(node["name"])
        for v in node.values():
            _names(v, out)
    elif isinstance(node, list):
        for v in node:
            _names(v, out)
    return out


def test_the_central_perimeter_exists_in_the_hh_dictionary():
    names = _names(json.loads((Path(__file__).parent / "fixtures" / "areas_russia.json").read_text()), set())
    missing = [n for n in HOME_REGIONS_CENTRAL if n not in names]
    assert not missing, missing


def test_region_names_become_stems_and_free_text_places_are_judged_by_them():
    st = home_region.stems(("Москва", "Московская область", "Тверская область", "Санкт-Петербург"))
    assert st == ("москва", "московск", "тверск", "санкт", "петербург")
    assert home_region.outside_name("105203, Г.МОСКВА, УЛ. ПЕРВОМАЙСКАЯ", st) is None     # a procurement address
    assert home_region.outside_name("Белгородская обл", st) == "Белгородская обл"           # ОВЕН's spelling
    assert home_region.outside_name("Кингисепп, Ленинградская область", st) == "Кингисепп, Ленинградская область"
    assert home_region.outside_name("Подольск, Московская область", st) is None
    assert home_region.outside_name("", st) is None and home_region.outside_name(None, st) is None  # unknown → let through
    assert home_region.outside_name("Челябинск", ()) is None                                  # the list is off
    # a town that does not name its region counts as home only through HOME_EXTRA_PLACES
    s = _settings()
    assert home_region.outside_name("Люберцы", home_region.stems(s.home_region_names)) == "Люберцы"
    assert home_region.outside_name("Люберцы", home_region.stems_of(s)) is None
    assert home_region.stems_of(Settings(_env_file=None)) == ()


def test_hh_rows_are_judged_by_the_region_id_in_area_path_never_guessed():
    home = home_region.Home(ids=[1, 2019], stems=("москва", "московск"))
    assert home.hh_place(".113.1.", "Москва") is None
    assert home.hh_place(".113.2019.2097.", "Подольск") is None
    assert home.hh_place(".113.1620.", "Пермь") == "Пермь"
    assert home.hh_place(None, "Пермь") is None and home.hh_place("", "Пермь") is None
    assert home_region.Home().hh_place(".113.1620.", "Пермь") is None   # the list is off
    assert decide(CardFacts(hh_id="1", title="Инженер АСУ ТП", applied=False, archived=False, outside="Пермь")) == "outside_home:Пермь"
    assert decide(CardFacts(hh_id="1", title="Инженер АСУ ТП", applied=False, archived=False)) is None


def _db():
    conn = connect(":memory:")
    migrate(conn)
    cols = "hh_id, site, title, url, source, search_pass, status, skip_reason, area_name, area_path, raw_json, first_seen_at, updated_at"
    rows = [
        ("1", "hh", "t", "u", "s", "regional", "evaluated", None, "Москва", ".113.1.", None),
        ("2", "hh", "t", "u", "s", "regional", "evaluated", None, "Пермь", ".113.1620.", None),
        ("3", "hh", "t", "u", "s", "regional", "new", None, "Казань", ".113.1347.", None),
        ("4", "hh", "t", "u", "s", "regional", "skipped", repo.PLANT_POOL, "Самара", ".113.1586.", None),
        ("5", "hh", "t", "u", "s", "regional", "sent", None, "Самара", ".113.1586.", None),
        ("6", "hh", "t", "u", "s", "regional", "rejected", None, "Самара", ".113.1586.", None),
        ("7", "hh", "t", "u", "s", "regional", "evaluated", None, None, None, None),                # no path: left alone
        ("tv:1", "trudvsem", "t", "u", "s", "regional", "evaluated", None, "Томская область", None, None),
        ("tv:2", "trudvsem", "t", "u", "s", "regional", "evaluated", None, "Павловский Посад", None, None),
        ("owen:1", "owen", "t", "u", "s", "owen_si", "new", None, "Белгород", None, json.dumps({"region": "Белгородская обл"})),
        ("owen:2", "owen", "t", "u", "s", "owen_si", "new", None, "Тверь", None, json.dumps({"region": "Тверская область"})),
        ("zk:1", "zakupki", "t", "u", "s", "tender", "prefiltered", None, "Люберцы", None,
         json.dumps({"address": "140000, ОБЛ МОСКОВСКАЯ, Г ЛЮБЕРЦЫ"})),
        ("zk:2", "zakupki", "t", "u", "s", "tender", "new", None, None, None, json.dumps({"customer": "x"})),   # no geography yet
    ]
    conn.executemany(f"INSERT INTO vacancies({cols}) VALUES ({','.join('?' * 11)}, 't', 't')", rows)
    return conn


def test_sweep_withdraws_rows_outside_home_from_every_source_and_stage():
    conn = _db()
    s = _settings()
    gone = repo.skip_outside_home(conn, [1, 2019], home_region.stems_of(s))
    assert gone == {"hh": 4, "trudvsem": 1, "owen": 1}
    st = {r["hh_id"]: (r["status"], r["skip_reason"]) for r in conn.execute("SELECT hh_id, status, skip_reason FROM vacancies")}
    assert st["1"] == ("evaluated", None) and st["7"] == ("evaluated", None)
    assert st["2"] == ("skipped", "outside_home:Пермь") and st["3"] == ("skipped", "outside_home:Казань")
    assert st["4"] == ("skipped", "outside_home:Самара")            # the plant pool too
    assert st["5"] == ("sent", None)                                 # history stays
    assert st["6"] == ("rejected", "outside_home:Самара")            # only the reason: the floor leaves it alone
    assert st["tv:1"] == ("skipped", "outside_home:Томская область") and st["tv:2"] == ("evaluated", None)
    assert st["owen:1"] == ("skipped", "outside_home:Белгород Белгородская обл") and st["owen:2"] == ("new", None)
    assert st["zk:1"] == ("prefiltered", None) and st["zk:2"] == ("new", None)
    # the second pass finds nothing new
    assert repo.skip_outside_home(conn, [1, 2019], home_region.stems_of(s)) == {}


def test_requeue_sends_hh_cards_to_triage_and_the_other_sources_back_to_new():
    conn = _db()
    repo.skip_outside_home(conn, [1, 2019], home_region.stems_of(_settings()))
    conn.execute("UPDATE vacancies SET first_seen_at = '2099-01-01'")   # «within N days» for the test clock
    assert repo.requeue_skipped(conn, "outside_home%", 30) == 5
    st = {r["hh_id"]: r["status"] for r in conn.execute("SELECT hh_id, status FROM vacancies")}
    assert st["2"] == "triage" and st["3"] == "triage" and st["4"] == "triage"
    assert st["tv:1"] == "new" and st["owen:1"] == "new"
    assert st["6"] == "rejected"   # never was skipped


def test_the_collector_searches_the_home_areas_only(monkeypatch):
    from hh_scout.browser import pacing
    from hh_scout.pipeline import collector as mod
    from tests.test_collector import FakeSession

    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    asked = []
    monkeypatch.setattr(mod, "resolve_region_ids", lambda conn, s, names=None: asked.append(names) or [1, 2019, 1783])
    monkeypatch.setattr(mod, "SEARCH_QUERIES", ("Q",))
    conn = connect(":memory:")
    migrate(conn)
    loads = []
    s = Settings(_env_file=None, daily_page_loads_min=20, daily_page_loads_max=20, max_pages_per_query=4,
                 home_regions="Москва, Московская область, Тверская область", company_channels="")
    assert s.search_all_russia is True   # the home list wins over the whole-country default
    mod.Collector(s, conn, session_factory=lambda b: FakeSession(b, loads), rng=random.Random(0), page_budget=20).run()
    assert asked == [("Москва", "Московская область", "Тверская область")]
    regional = [u for u in loads if "area=" in u]
    assert regional and all("area=1&" in u and "area=2019" in u and "area=1783" in u and "area=113" not in u for u in regional)


def test_the_home_load_survives_a_dead_dictionary(monkeypatch):
    from hh_scout.hh import areas
    monkeypatch.setattr(home_region, "resolve_region_ids", lambda *a, **k: (_ for _ in ()).throw(areas.AreaResolutionError("x")))
    home = home_region.Home.load(connect(":memory:"), _settings())
    assert home.ids == [] and home.active and "московск" in home.stems
    assert not home_region.Home.load(connect(":memory:"), Settings(_env_file=None)).active


def test_web_discovery_walks_only_the_home_regions_while_away():
    from hh_scout.sources import web_discovery as wd
    s = _settings()
    assert wd.regions(s) == HOME_REGIONS_CENTRAL
    assert len(wd.tasks(s)) == len(wd.DISCOVERY_VENDORS) + len(HOME_REGIONS_CENTRAL) * len(wd.DISCOVERY_REGION_QUERIES)
    assert len(wd.regions()) > len(HOME_REGIONS_CENTRAL) and wd.regions(Settings(_env_file=None)) == wd.regions()
