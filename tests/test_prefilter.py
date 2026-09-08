from hh_scout.pipeline.prefilter import CardFacts, decide

MIN = 120_000


def _c(title, **kw):
    base = dict(hh_id="1", title=title, applied=False, archived=False, employment="full", salary_to_net=None)
    base.update(kw)
    return CardFacts(**base)


def test_applied_archived_fifo_first():
    assert decide(_c("Инженер АСУ ТП", applied=True), MIN) == "applied"
    assert decide(_c("Инженер АСУ ТП", archived=True), MIN) == "archived"
    assert decide(_c("Инженер АСУ ТП", employment="fly_in_fly_out"), MIN) == "fly_in_fly_out"


def test_stop_words_and_keep_words():
    assert decide(_c("Менеджер по продажам оборудования"), MIN) == "stopword:менеджер по продажам"
    assert decide(_c("Программист 1С"), MIN) is None  # keep-word 'программ' wins
    assert decide(_c("Бухгалтер"), MIN) == "stopword:бухгалтер"
    # substring inside another word must not trigger: руко-водитель
    assert decide(_c("Руководитель группы в службу автоматики"), MIN) is None
    assert decide(_c("Руководитель отдела продаж"), MIN) == "no_engineering_title"
    assert decide(_c("Водитель-экспедитор"), MIN) == "stopword:водитель"


def test_engineering_title_required():
    assert decide(_c("Бизнес-аналитик"), MIN) == "no_engineering_title"
    assert decide(_c("Оператор зернового склада"), MIN) == "no_engineering_title"
    assert decide(_c("Сервисный инженер"), MIN) is None
    assert decide(_c("Наладчик КИПиА"), MIN) is None
    assert decide(_c("PLC Programmer"), MIN) is None
    assert decide(_c("Automation Engineer"), MIN) is None


def test_salary_is_not_a_rule():
    # leads for contracting: even a very low stated salary passes to triage
    assert decide(_c("Инженер АСУ ТП", salary_to_net=60_000), MIN) is None
    assert decide(_c("Инженер АСУ ТП", salary_to_net=None), MIN) is None
