import json

import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.llm.bridge_client import BridgeClient, extract_json
from hh_scout.llm.triage import Triager


def _settings(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "card_triage.md").write_text("header\n---\nSYSTEM {candidate_profile}", encoding="utf-8")
    return Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts, triage_batch_size=2)


def _db():
    conn = connect(":memory:")
    migrate(conn)
    for i, title in enumerate(["Инженер АСУ ТП", "Менеджер", "PLC Programmer"], start=1):
        conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (str(i), title, f"u{i}", "s", "regional", "triage", "t", "t"))
    return conn


def test_extract_json_variants():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]
    assert extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json('Вот ответ: [{"a": 1}] спасибо') == [{"a": 1}]


@respx.mock
def test_triage_applies_verdicts_and_retries_invalid(tmp_path):
    s = _settings(tmp_path)
    conn = _db()
    answers = iter([
        json.dumps([{"hh_id": "1", "open": True, "priority": 1, "reason": "ядро"},
                    {"hh_id": "2", "open": False, "priority": 3, "reason": "продажи"}]),
        "мусор без json",
        "снова мусор",
    ])
    route = respx.post("http://bridge.test/complete").mock(
        side_effect=lambda req: httpx.Response(200, json={"text": next(answers), "usage": {}, "cost_usd": 0.001}))
    bridge = BridgeClient(s, sleep=lambda x: None)
    stats = Triager(s, conn, bridge).run()
    assert route.call_count == 3  # batch1 ok, batch2 invalid twice
    assert stats.opened == 1 and stats.closed == 1 and stats.failed_batches == 1
    rows = {r["hh_id"]: r for r in conn.execute("SELECT * FROM vacancies")}
    assert rows["1"]["status"] == "to_fetch" and rows["1"]["triage_priority"] == 1
    assert rows["2"]["status"] == "skipped" and rows["2"]["skip_reason"] == "triage"
    assert rows["3"]["status"] == "triage"  # failed batch stays for next time
    sent = json.loads(route.calls[0].request.content)
    assert sent["system_text"] == "SYSTEM ПРОФИЛЬ"
    assert "Инженер АСУ ТП" in sent["messages"][0]["content"]


@respx.mock
def test_bridge_retries_transient_then_fails(tmp_path):
    s = _settings(tmp_path)
    respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(503, text="busy"))
    bridge = BridgeClient(s, sleep=lambda x: None)
    import pytest
    from hh_scout.llm.bridge_client import BridgeUnavailable
    with pytest.raises(BridgeUnavailable):
        bridge.complete("s", "u")
    assert bridge.calls == 3


def test_missing_private_profile_gives_helpful_error(tmp_path):
    import pytest

    from hh_scout.llm.prompts import PrivatePromptMissing, render
    (tmp_path / "card_triage.md").write_text("h\n---\nSYS {candidate_profile}", encoding="utf-8")
    with pytest.raises(PrivatePromptMissing, match="candidate_profile.example.md"):
        render(tmp_path, "card_triage.md")
