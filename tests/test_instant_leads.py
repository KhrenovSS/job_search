"""v9.8: a ready lead goes out right after the sitting, and the day's quota is shared with the noon digest."""

import asyncio

import pytest

from hh_scout.bot import digest as digest_mod
from hh_scout.bot.digest import send_instant_leads
from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.pipeline.digest_builder import finalize_digest, plan_digest
from hh_scout.pipeline.ranker import digest_header


RULES = "rules-of-today"   # the stamp a letter written under the current rules carries (decision #46)


@pytest.fixture(autouse=True)
def _no_telegram_pauses(monkeypatch):
    """The real 1 s pause between messages is politeness to Telegram, not behaviour under test."""
    monkeypatch.setattr(digest_mod, "PAUSE_S", 0)


@pytest.fixture(autouse=True)
def _fixed_rules(monkeypatch):
    """The rules stamp comes from the owner's private prompts, which tests do not have."""
    monkeypatch.setattr(digest_mod, "rules_hash", lambda settings, site="hh": RULES)


class _FakeBot:
    def __init__(self):
        self.messages = []
        self._id = 0

    async def send_message(self, chat_id, text, **kw):
        self._id += 1
        self.messages.append(text)
        return type("M", (), {"message_id": self._id})()


def _db(letters=(1, 2, 3)):
    conn = connect(":memory:")
    migrate(conn)
    for i, total in ((1, 90), (2, 80), (3, 70), (4, 40)):
        conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,url,source,search_pass,status,site,"
                     "first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,'evaluated','hh',?,?)",
                     (i, str(i), f"Инженер {i}", f"ООО {i}", "u", "s", "regional", "t", "t"))
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                     "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                     "VALUES (?,?,0,0,?,?,?,'maybe',0,'staff','integrator','v','p','[]','2026-09-08T10:00:00+00:00')",
                     (i, 80, 80, 60, total))
    for i in letters:
        repo.save_cover_letter(conn, i, f"письмо {i}", rules_hash=RULES)
    return conn


def test_instant_send_delivers_ready_leads_in_queue_order():
    s = Settings(_env_file=None, digest_max_items=20)
    conn = _db()
    bot = _FakeBot()
    n = asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh"))
    assert n == 3                                      # 1, 2, 3 are above the threshold and have letters
    assert "Новые лиды: 3" in bot.messages[0]
    assert repo.count_by_status(conn) == {"sent": 3, "evaluated": 1}  # the 40 is NOT rejected here
    assert repo.leads_sent_today(conn) == 3


def test_a_lead_without_a_letter_waits_instead_of_holding_up_the_chat():
    """Its letter failed this run; it keeps its place in the queue for the next sitting or for noon."""
    s = Settings(_env_file=None, digest_max_items=20)
    conn = _db(letters=(1, 3))
    n = asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    assert n == 2
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"


def test_a_letter_written_under_older_rules_waits_for_the_noon_digest():
    """v9.9: the rules changed while the lead sat in the queue — the stored text must not go out as it is."""
    s = Settings(_env_file=None, digest_max_items=20)
    conn = _db()
    repo.save_cover_letter(conn, 2, "письмо 2", rules_hash="rules-of-last-week")
    conn.execute("UPDATE cover_letters SET rules_hash = NULL WHERE vacancy_id = 3")  # written before the stamp existed
    bot = _FakeBot()
    assert asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh")) == 1
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"
    assert repo.lead_by_hh_id(conn, "3")["status"] == "evaluated"
    assert not any("письмо 2" in m or "письмо 3" in m for m in bot.messages)


def test_the_quota_is_a_property_of_the_day_not_of_one_message():
    s = Settings(_env_file=None, digest_max_items=2)
    conn = _db()
    assert asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh")) == 2
    # the morning sitting spent the whole quota: the evening one sends nothing...
    assert asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh")) == 0
    # ...and the noon digest does not hand out a second quota either
    plan = plan_digest(conn, s)
    assert plan.leads == [] and plan.sent_today == 2
    assert plan.waiting_total == 1                      # the 70 still waits its turn


def test_noon_digest_says_what_already_went_out_instead_of_lead_not_found():
    s = Settings(_env_file=None, digest_max_items=2)
    conn = _db()
    asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    plan = plan_digest(conn, s)
    head = digest_header(len(plan.leads), plan.checked, sent_today=plan.sent_today)
    assert "не нашлось" not in head and "2 лида уже ушло" in head


def test_below_threshold_is_cleaned_up_by_the_noon_digest_not_by_instant_sends():
    s = Settings(_env_file=None, digest_max_items=20)
    conn = _db()
    asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    assert repo.lead_by_hh_id(conn, "4")["status"] == "evaluated"
    plan = plan_digest(conn, s)
    finalize_digest(conn, s, [], plan.checked, note="noon")
    assert repo.lead_by_hh_id(conn, "4")["status"] == "rejected"
