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


def test_kv_roundtrip():
    conn = connect(":memory:")
    migrate(conn)
    assert kv_get(conn, "paused") is None
    kv_set(conn, "paused", "1")
    assert kv_get(conn, "paused") == "1"
    kv_set(conn, "paused", None)
    assert kv_get(conn, "paused", "0") == "0"
