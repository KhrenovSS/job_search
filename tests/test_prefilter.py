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
