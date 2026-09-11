from hh_scout.db import MIGRATIONS, connect, kv_get, kv_set, migrate


def test_migrations_create_schema_and_are_idempotent():
    conn = connect(":memory:")
    assert migrate(conn) == len(MIGRATIONS)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"areas_cache", "vacancies", "evaluations", "digests",
            "digest_items", "feedback", "runs", "kv"} <= tables
    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"page_loads", "bridge_calls", "trigger"} <= run_cols
    # second run is a no-op
    assert migrate(conn) == len(MIGRATIONS)


def test_m006_adds_site_with_hh_default():
    conn = connect(":memory:")
    assert migrate(conn) == 7
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(vacancies)")}
    assert cols["site"][4] == "'hh'"  # default value
    conn.execute("INSERT INTO vacancies(hh_id, title, url, source, search_pass, status, first_seen_at, updated_at) "
                 "VALUES ('1','t','u','search:0','regional','new','x','x')")
    assert conn.execute("SELECT site FROM vacancies").fetchone()[0] == "hh"


def test_kv_roundtrip():
    conn = connect(":memory:")
    migrate(conn)
    assert kv_get(conn, "paused") is None
    kv_set(conn, "paused", "1")
    assert kv_get(conn, "paused") == "1"
    kv_set(conn, "paused", None)
    assert kv_get(conn, "paused", "0") == "0"


def test_m007_backfills_employer_id_from_stored_vacancy_view():
    conn = connect(":memory:")
    for step in MIGRATIONS[:6]:
        step(conn)
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, url, source, search_pass, status, raw_json, first_seen_at, updated_at) "
                 "VALUES ('1','t','Амперика','u','s','regional','evaluated','{\"company\": {\"id\": 9070507, \"name\": \"Амперика\"}}','x','x')")
    conn.execute("INSERT INTO vacancies(hh_id, site, title, employer, url, source, search_pass, status, raw_json, first_seen_at, updated_at) "
                 "VALUES ('profi:5','profi','t','Сергей','u','profi','profi','evaluated','{\"client\": \"Сергей\"}','x','x')")
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, url, source, search_pass, status, first_seen_at, updated_at) "
                 "VALUES ('2','t','ООО','u','s','regional','to_fetch','x','x')")
    assert migrate(conn) == 7
    got = {r[0]: r[1] for r in conn.execute("SELECT hh_id, employer_id FROM vacancies")}
    assert got == {"1": "9070507", "profi:5": None, "2": None}



def test_transaction_commits_a_batch_and_rolls_back_on_error():
    from hh_scout.db import transaction

    conn = connect(":memory:")
    migrate(conn)
    with transaction(conn):
        kv_set(conn, "a", "1")
        assert conn.in_transaction  # a real BEGIN, unlike `with conn:` under isolation_level=None
        with transaction(conn):  # nested: joins the outer one, no "cannot start a transaction within a transaction"
            kv_set(conn, "b", "2")
        assert conn.in_transaction
    assert not conn.in_transaction
    assert (kv_get(conn, "a"), kv_get(conn, "b")) == ("1", "2")

    try:
        with transaction(conn):
            kv_set(conn, "a", "changed")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not conn.in_transaction
    assert kv_get(conn, "a") == "1"
