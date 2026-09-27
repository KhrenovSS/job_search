from hh_scout.pipeline.prefilter import CardFacts, decide


def _c(title, **kw):
    base = dict(hh_id="1", title=title, applied=False, archived=False)
    base.update(kw)
    return CardFacts(**base)


def test_applied_and_archived_are_skipped():
    assert decide(_c("Инженер АСУ ТП", applied=True)) == "applied"
    assert decide(_c("Инженер АСУ ТП", archived=True)) == "archived"


def test_rotation_work_is_not_a_rule_anymore():
    """v9.7: вахта says where the object is, not what the work is — the triage model decides (decision #43).
    Since v9.11 the rule has no employment or salary inputs at all: the title is the only thing it reads."""
    assert decide(_c("Инженер АСУ ТП")) is None
    # a rotation vacancy that is not engineering at all still goes out on its title
    assert decide(_c("Повар вахтой")) == "stopword:повар"


def test_stop_words_and_keep_words():
    assert decide(_c("Менеджер по продажам оборудования")) == "stopword:менеджер по продажам"
    assert decide(_c("Программист 1С")) is None  # keep-word 'программ' wins
    assert decide(_c("Бухгалтер")) == "stopword:бухгалтер"
    # substring inside another word must not trigger: руко-водитель
    assert decide(_c("Руководитель группы в службу автоматики")) is None
    assert decide(_c("Руководитель отдела продаж")) == "no_engineering_title"
    assert decide(_c("Водитель-экспедитор")) == "stopword:водитель"


def test_engineering_title_required():
    assert decide(_c("Бизнес-аналитик")) == "no_engineering_title"
    assert decide(_c("Оператор зернового склада")) == "no_engineering_title"
    assert decide(_c("Сервисный инженер")) is None
    assert decide(_c("Наладчик КИПиА")) is None
    assert decide(_c("PLC Programmer")) is None
    assert decide(_c("Automation Engineer")) is None


def test_requeue_skipped_returns_only_recent_cards_of_one_reason():
    """One-off after decision #43: the 478 cards skipped as вахта go back to triage — the fresh ones."""
    from datetime import datetime, timedelta, timezone

    from hh_scout.db import connect, migrate
    from hh_scout.pipeline import repo

    conn = connect(":memory:")
    migrate(conn)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    for hh_id, reason, seen in (("1", "fly_in_fly_out", new), ("2", "fly_in_fly_out", old), ("3", "triage", new)):
        conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,skip_reason,first_seen_at,updated_at) "
                     "VALUES (?,?,'u','s','regional','skipped',?,?,?)", (hh_id, "Инженер", reason, seen, seen))
    assert repo.requeue_skipped(conn, "fly_in_fly_out", 14) == 1
    st = {r["hh_id"]: (r["status"], r["skip_reason"]) for r in conn.execute("SELECT * FROM vacancies")}
    assert st == {"1": ("triage", None), "2": ("skipped", "fly_in_fly_out"), "3": ("skipped", "triage")}


def test_a_company_card_passes_without_an_engineering_word_but_stop_words_still_hold():
    assert decide(_c("Сборщик электрощитового оборудования")) == "no_engineering_title"   # a vacancy card
    assert decide(_c("Сборщик электрощитового оборудования", company=True)) is None       # the company is the lead
    assert decide(_c("Менеджер по продажам щитового оборудования", company=True)) == "stopword:менеджер по продажам"


def test_blocked_region_beats_the_title(monkeypatch):
    """Decision #65: a vacancy in Crimea or the annexed regions is skipped by the rules, whatever the title."""
    assert decide(_c("Инженер-программист ПЛК", region="Республика Крым")) == "region:Республика Крым"
    assert decide(_c("Сборщик шкафов", company=True, region="Херсонская область")) == "region:Херсонская область"
    assert decide(_c("Инженер-программист ПЛК", region=None)) is None
    # `applied` still wins: the owner has already answered, the row must count as such
    assert decide(_c("Инженер-программист ПЛК", applied=True, region="Республика Крым")) == "applied"


def test_run_skips_blocked_regions_from_the_card_path_and_withdraws_rows_already_past_the_rules():
    from hh_scout.config import Settings
    from hh_scout.db import connect, migrate
    from hh_scout.pipeline import repo
    from hh_scout.pipeline.prefilter import run

    conn = connect(":memory:")
    migrate(conn)
    rows = [
        # hh_id, area_name, area_path, status, skip_reason
        ("1", "Симферополь", ".113.225.2114.131.", "new", None),           # rule: Крым
        ("2", "Донецк", ".113.2134.2136.", "new", None),                   # rule: ДНР
        ("3", "Донецк (Ростовская область)", ".113.226.1530.1543.", "new", None),  # passes
        ("4", "Луганск", None, "new", None),                               # no path → not guessed by name
        ("5", "Севастополь", ".113.225.2114.130.", "evaluated", None),     # in the queue → withdrawn
        ("6", "Ялта", ".113.225.2114.2120.", "rejected", None),            # written off by score → reason only
        ("7", "Мелитополь", ".113.2155.2159.", "skipped", "plant_pool"),   # plant pool → withdrawn
        ("8", "Херсон", ".113.2209.2210.", "sent", None),                  # history stays
        ("9", "Евпатория", ".113.225.2114.2115.", "skipped", "triage"),    # already skipped for another reason
    ]
    for hh_id, area, path, status, reason in rows:
        conn.execute("INSERT INTO vacancies(hh_id,title,url,area_name,area_path,source,search_pass,status,skip_reason,"
                     "first_seen_at,updated_at) VALUES (?,?,'u',?,?,'s','regional',?,?,'t','t')",
                     (hh_id, "Инженер-программист ПЛК", area, path, status, reason))
    conn.execute("INSERT INTO vacancies(hh_id,site,title,url,area_path,source,search_pass,status,first_seen_at,updated_at) "
                 "VALUES ('owen:x','owen','Интегратор','u','.113.225.2114.131.','s','owen_si','new','t','t')")
    outcomes = run(conn, Settings(_env_file=None))
    assert outcomes["region"] == 2 and outcomes["passed"] == 2
    st = {r["hh_id"]: (r["status"], r["skip_reason"]) for r in conn.execute("SELECT * FROM vacancies")}
    assert st["1"] == ("skipped", "region:Республика Крым")
    assert st["2"] == ("skipped", "region:Донецкая Народная Республика")
    assert st["3"] == ("triage", None)
    assert st["4"] == ("triage", None)
    assert st["5"] == ("skipped", "region:Республика Крым")
    assert st["6"] == ("rejected", "region:Республика Крым")
    assert st["7"] == ("skipped", "region:Запорожская область")
    assert st["8"] == ("sent", None)
    assert st["9"] == ("skipped", "triage")
    assert st["owen:x"] == ("new", None)      # catalogue rows are not hh.ru cards; the sweep is hh-only
    # the floor no longer sees the Ялта row: `rejected` with a reason is "by age/rule", not "by score"
    assert repo.floor_candidates(conn, threshold=50, min_total=40, min_role=40, lookback_days=3) == []
    # a second pass changes nothing
    assert repo.skip_blocked_regions(conn, {2114: "Республика Крым"}) == {}
