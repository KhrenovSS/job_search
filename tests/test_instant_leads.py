"""v9.8: a ready lead goes out right after the sitting. v9.14 removed the daily quota — DIGEST_MAX_ITEMS
survives only as a fuse, so the tests below run both with it set and with it off (the default)."""

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


def _settings(**kw):
    """The real pause between messages is politeness to Telegram, not behaviour under test."""
    return Settings(_env_file=None, telegram_pause_s=0, **kw)


@pytest.fixture(autouse=True)
def _fixed_rules(monkeypatch):
    """The rules stamp comes from the owner's private prompts, which tests do not have."""
    monkeypatch.setattr(digest_mod, "rules_hash", lambda settings, site="hh": RULES)


class _FakeBot:
    def __init__(self):
        self.messages = []
        self.deleted = []
        self._id = 0

    async def send_message(self, chat_id, text, **kw):
        self._id += 1
        self.messages.append(text)
        return type("M", (), {"message_id": self._id})()

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


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
    s = _settings(digest_max_items=20)
    conn = _db()
    bot = _FakeBot()
    n = asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh"))
    assert n == 3                                      # 1, 2, 3 are above the threshold and have letters
    assert "Новые лиды: 3" in bot.messages[0]
    assert repo.count_by_status(conn) == {"sent": 3, "evaluated": 1}  # the 40 is NOT rejected here
    assert repo.leads_sent_today(conn) == 3


def test_a_lead_without_a_letter_waits_instead_of_holding_up_the_chat():
    """Its letter failed this run; it keeps its place in the queue for the next sitting or for noon."""
    s = _settings(digest_max_items=20)
    conn = _db(letters=(1, 3))
    n = asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    assert n == 2
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"


def test_a_letter_written_under_older_rules_waits_for_the_noon_digest():
    """v9.9: the rules changed while the lead sat in the queue — the stored text must not go out as it is."""
    s = _settings(digest_max_items=20)
    conn = _db()
    repo.save_cover_letter(conn, 2, "письмо 2", rules_hash="rules-of-last-week")
    conn.execute("UPDATE cover_letters SET rules_hash = NULL WHERE vacancy_id = 3")  # written before the stamp existed
    bot = _FakeBot()
    assert asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh")) == 1
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"
    assert repo.lead_by_hh_id(conn, "3")["status"] == "evaluated"
    assert not any("письмо 2" in m or "письмо 3" in m for m in bot.messages)


def test_the_fuse_when_set_is_a_property_of_the_day_not_of_one_message():
    """DIGEST_MAX_ITEMS is 0 by default since v9.14; set above zero it caps the day as it always did."""
    s = _settings(digest_max_items=2)
    conn = _db()
    assert asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh")) == 2
    # the morning sitting spent the whole quota: the evening one sends nothing...
    assert asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh")) == 0
    # ...and the noon digest does not hand out a second quota either
    plan = plan_digest(conn, s)
    assert plan.leads == [] and plan.sent_today == 2
    assert plan.waiting_total == 1                      # the 70 still waits its turn


def test_noon_digest_says_what_already_went_out_instead_of_lead_not_found():
    s = _settings(digest_max_items=2)
    conn = _db()
    asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    plan = plan_digest(conn, s)
    head = digest_header(len(plan.leads), plan.checked, sent_today=plan.sent_today)
    assert "не нашлось" not in head and "2 лида уже ушло" in head


def test_below_threshold_is_cleaned_up_by_the_noon_digest_not_by_instant_sends():
    s = _settings(digest_max_items=20)
    conn = _db()
    asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    assert repo.lead_by_hh_id(conn, "4")["status"] == "evaluated"
    plan = plan_digest(conn, s)
    finalize_digest(conn, s, [], plan.checked, note="noon")
    assert repo.lead_by_hh_id(conn, "4")["status"] == "rejected"


# --- v9.11: one delivery loop, per-site rules, no race with the noon digest --------------------------

def _profi_order(conn, vid=9, letter_rules="rules-profi"):
    conn.execute("INSERT INTO vacancies(id,hh_id,site,title,employer,url,source,search_pass,status,first_seen_at,updated_at) "
                 "VALUES (?,?,'profi','Наладить ПЛК','Сергей','u','profi','profi','evaluated','t','t')", (vid, f"profi:{vid}"))
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (?,80,0,0,80,60,85,'yes',0,'project','end_customer','v','p','[]','2026-09-08T10:00:00+00:00')", (vid,))
    repo.save_cover_letter(conn, vid, "предложение", rules_hash=letter_rules)


def test_noon_digest_judges_a_profi_bid_by_the_profi_rules(monkeypatch):
    """Before v9.11 the digest compared every letter with the hh stamp: a bid was "stale" every day and went out empty."""
    from hh_scout.bot.digest import send_digest

    monkeypatch.setattr(digest_mod, "rules_hash", lambda settings, site="hh": f"rules-{site}")
    s = _settings(digest_max_items=20, daily_letters_floor=0)   # the floor is tested on its own below
    conn = _db(letters=())
    for i in (1, 2, 3):
        repo.save_cover_letter(conn, i, f"письмо {i}", rules_hash="rules-hh")
    _profi_order(conn)
    bot = _FakeBot()
    n = asyncio.run(send_digest(bot, conn, s, chat_id=1, evaluate=False))
    assert n == 4
    assert any("предложение" in m for m in bot.messages)      # the bid went out with its card
    assert sum("письмо" in m for m in bot.messages) == 3


def test_digest_skips_the_scoring_pass_while_a_sitting_runs(monkeypatch):
    from hh_scout.bot.digest import send_digest

    calls = []
    monkeypatch.setattr(digest_mod, "_evaluate_pending", lambda settings: calls.append("eval") or (0, 0))
    s = _settings(digest_max_items=20)
    conn = _db()
    asyncio.run(send_digest(_FakeBot(), conn, s, chat_id=1, evaluate=False))
    assert calls == []
    conn2 = _db()
    asyncio.run(send_digest(_FakeBot(), conn2, s, chat_id=1))
    assert calls == ["eval"]


def test_instant_send_takes_ready_leads_from_the_whole_queue_not_just_its_head():
    """Quota 2, the two strongest leads have no letter yet: the ready one further down still goes out."""
    s = _settings(digest_max_items=2)
    conn = _db(letters=(3,))
    bot = _FakeBot()
    assert asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh")) == 1
    assert repo.lead_by_hh_id(conn, "3")["status"] == "sent"


def test_a_letter_written_without_a_dossier_goes_stale_once_the_dossier_exists():
    from hh_scout.llm.cover_letter import usable_letter

    conn = _db(letters=())
    conn.execute("UPDATE vacancies SET employer_id = '77' WHERE id = 1")
    repo.save_cover_letter(conn, 1, "письмо без досье", rules_hash=RULES, with_dossier=False)
    row = repo.lead_by_hh_id(conn, "1")
    assert usable_letter(row, RULES) == "письмо без досье"
    conn.execute("INSERT INTO employers(employer_id, name, found, brief, sources, researched_at) "
                 "VALUES ('77', 'ООО 1', 1, '{\"what_they_do\": \"линии розлива\"}', '[]', 't')")
    row = repo.lead_by_hh_id(conn, "1")
    assert usable_letter(row, RULES) is None                     # the card now names the production, the letter does not
    repo.save_cover_letter(conn, 1, "письмо с досье", rules_hash=RULES, with_dossier=True)
    assert usable_letter(repo.lead_by_hh_id(conn, "1"), RULES) == "письмо с досье"


# --- v9.12: the daily floor (decision #52) -----------------------------------------------------------

def _near_lead(conn, vid, total, *, employer=None, employer_id=None, status="evaluated", role=55, agency=0,
               created="2026-09-19T10:00:00+00:00", skip_reason=None):
    conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,employer_id,url,source,search_pass,status,skip_reason,site,"
                 "first_seen_at,updated_at) VALUES (?,?,?,?,?,'u','s','regional',?,?,'hh','t','t')",
                 (vid, str(vid), f"Инженер {vid}", employer or f"ООО {vid}", employer_id, status, skip_reason))
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (?,60,0,0,?,40,?,'maybe',?,'staff','integrator','v','взять программную часть','[]',?)",
                 (vid, role, total, agency, created))


def _empty_db():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def test_the_floor_tops_up_from_below_the_threshold_when_the_day_came_up_short():
    from hh_scout.pipeline.digest_builder import promote_floor

    s = Settings(_env_file=None, daily_letters_floor=5, score_threshold=50)
    conn = _empty_db()
    _near_lead(conn, 10, 48)                                            # today's near miss, still evaluated
    _near_lead(conn, 11, 47, status="rejected")                         # written off yesterday by score: eligible
    _near_lead(conn, 12, 49, status="rejected", created="2026-09-01T10:00:00+00:00")   # too old
    _near_lead(conn, 13, 46, status="rejected", skip_reason="queue_expired")            # written off by age, not by score
    _near_lead(conn, 14, 45, agency=1)                                  # agency
    _near_lead(conn, 15, 45, role=30)                                   # not a programmer's role
    _near_lead(conn, 16, 39)                                            # below the floor's own minimum (40)
    _near_lead(conn, 17, 44, employer="ООО 10", employer_id=None)       # same company as 10 (by name): one per employer
    _near_lead(conn, 18, 43, employer="Закрытая", employer_id="9")
    conn.execute("INSERT INTO lead_actions(vacancy_id, action, created_at) VALUES (18, 'responded', ?)", (repo.utcnow(),))
    _near_lead(conn, 19, 42)
    assert promote_floor(conn, s) == 3                                  # only three eligible: the floor stays short
    assert {r["hh_id"] for r in repo.lead_queue(conn, 50)} == {"10", "11", "19"}
    assert repo.lead_by_hh_id(conn, "11")["status"] == "evaluated" and repo.lead_by_hh_id(conn, "11")["floor"] == 1
    for hh_id in ("12", "13", "14", "15", "16", "17"):
        assert repo.lead_by_hh_id(conn, hh_id)["floor"] == 0
    # the noon clean-up leaves floor rows in the queue and writes off the rest below the threshold
    assert repo.reject_below(conn, 50) >= 1
    assert repo.lead_by_hh_id(conn, "10")["status"] == "evaluated" and repo.lead_by_hh_id(conn, "16")["status"] == "rejected"
    # a second call takes nothing more: the taken rows carry floor = 1, the rest are ineligible
    assert promote_floor(conn, s) == 0


def test_the_floor_is_quiet_when_enough_leads_went_out_or_when_disabled():
    from hh_scout.pipeline.digest_builder import finalize_digest, promote_floor

    s = Settings(_env_file=None, daily_letters_floor=2, score_threshold=50)
    conn = _empty_db()
    for vid, total in ((1, 80), (2, 75), (3, 48)):
        _near_lead(conn, vid, total)
    finalize_digest(conn, s, [(repo.lead_by_hh_id(conn, "1"), 1, 2), (repo.lead_by_hh_id(conn, "2"), 3, 4)], checked=0,
                    note="instant:hh", reject=False)
    assert promote_floor(conn, s) == 0                                   # two leads in the last 24 h — floor met
    assert promote_floor(conn, Settings(_env_file=None, daily_letters_floor=0)) == 0


def test_floor_rows_get_letters_and_a_marked_card_in_the_noon_digest(monkeypatch):
    """The whole path: short day → floor → the digest's own scoring pass writes the letter → card says so."""
    from hh_scout.bot.digest import send_digest
    from hh_scout.pipeline.ranker import format_card

    s = Settings(_env_file=None, daily_letters_floor=3, score_threshold=50)
    conn = _empty_db()
    _near_lead(conn, 10, 48)
    written = []

    def fake_eval(settings):
        # stands in for Evaluator + CoverLetterWriter on a separate connection: the floor row is in the queue by now
        for r in repo.lead_queue(conn, settings.score_threshold):
            repo.save_cover_letter(conn, r["id"], f"письмо {r['hh_id']}", rules_hash=RULES)
            written.append(r["hh_id"])
        return 0, len(written)

    monkeypatch.setattr(digest_mod, "_evaluate_pending", fake_eval)
    bot = _FakeBot()
    assert asyncio.run(send_digest(bot, conn, s, chat_id=1)) == 1
    assert written == ["10"]
    assert "Добрано по дневному минимуму (ниже порога): 1" in bot.messages[0]
    assert any("Ниже порога, взят по дневному минимуму" in m for m in bot.messages)
    assert repo.lead_by_hh_id(conn, "10")["status"] == "sent"
    row = repo.lead_by_hh_id(conn, "10")
    assert "📉" in format_card(1, row, row)


def test_readmit_puts_recently_rejected_leads_back_after_a_threshold_change():
    conn = _empty_db()
    _near_lead(conn, 20, 55, status="rejected")
    _near_lead(conn, 21, 55, status="rejected", created="2026-09-01T10:00:00+00:00")
    _near_lead(conn, 22, 45, status="rejected")
    _near_lead(conn, 23, 55, status="rejected", skip_reason="queue_expired")
    assert repo.readmit_rejected(conn, min_total=50, days=3) == 1
    assert [r["hh_id"] for r in repo.lead_queue(conn, 50)] == ["20"]


# --- v9.14: no daily quota, decision #54 ------------------------------------------------

def test_without_a_quota_the_whole_ready_queue_goes_out():
    """The default since v9.14: a lead found is a letter sent — holding one back is a guaranteed no."""
    s = _settings()                       # digest_max_items = 0
    conn = _db()
    for i in (5, 6, 7, 8, 9):             # a flood the old quota of 20 would have capped
        conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,url,source,search_pass,status,site,"
                     "first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,'evaluated','hh',?,?)",
                     (i, str(i), f"Инженер {i}", f"ООО {i}", "u", "s", "regional", "t", "t"))
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,"
                     "total,ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,"
                     "created_at) VALUES (?,80,0,0,80,60,55,'maybe',0,'staff','integrator','v','p','[]',"
                     "'2026-09-08T10:00:00+00:00')", (i,))
        repo.save_cover_letter(conn, i, f"письмо {i}", rules_hash=RULES)
    assert asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh")) == 8
    assert repo.leads_sent_today(conn) == 8
    # ...and the noon digest does not stop either: only the 40 is left, below the threshold
    assert plan_digest(conn, s).leads == []


def test_a_send_cut_short_keeps_what_reached_the_chat():
    """v9.14: dozens of leads per send — a Telegram failure halfway must not re-send the ones already delivered."""
    s = _settings()
    conn = _db()

    class _BreakingBot(_FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if len(self.messages) >= 3:            # header + card + letter, then it dies
                raise RuntimeError("Telegram упал")
            return await super().send_message(chat_id, text, **kw)

    with pytest.raises(RuntimeError):
        asyncio.run(send_instant_leads(_BreakingBot(), conn, s, chat_id=1, site="hh"))
    assert repo.lead_by_hh_id(conn, "1")["status"] == "sent"        # it is in the chat, so it is recorded
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"   # never made it — stays in the queue
    assert repo.last_digest(conn)["items_count"] == 1
    # the next send picks up exactly where it stopped, with no duplicate
    assert asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh")) == 2


def test_flood_control_is_waited_out_not_given_up_on():
    from aiogram.exceptions import TelegramRetryAfter

    s = _settings()
    conn = _db(letters=(1,))

    class _FloodingBot(_FakeBot):
        def __init__(self):
            super().__init__()
            self.refused = 0

        async def send_message(self, chat_id, text, **kw):
            if self.refused < 1:
                self.refused += 1
                raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
            return await super().send_message(chat_id, text, **kw)

    bot = _FloodingBot()
    assert asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh")) == 1
    assert bot.refused == 1 and any("письмо 1" in m for m in bot.messages)


def test_the_noon_digest_holds_back_a_lead_whose_letter_is_not_written():
    """Without a quota the digest takes the whole queue — a card without its letter would close the lead for good."""
    from hh_scout.bot.digest import send_digest

    s = _settings(daily_letters_floor=0)
    conn = _db(letters=(1, 3))
    bot = _FakeBot()
    assert asyncio.run(send_digest(bot, conn, s, chat_id=1, evaluate=False)) == 2
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"
    assert any("Ждут очереди" in m and "/letter 2" in m for m in bot.messages)


def test_a_card_whose_letter_never_followed_is_taken_back():
    """21.09: a restart between the card and the letter left a card without a letter in the chat, and the lead — still
    queued — went out a second time. The pair is atomic now: no letter, no card, the lead goes out whole next time."""
    s = _settings()
    conn = _db()

    class _DiesOnLetterTwo(_FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if "письмо 2" in text:
                raise asyncio.CancelledError()          # the service is being stopped
            return await super().send_message(chat_id, text, **kw)

    bot = _DiesOnLetterTwo()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(send_instant_leads(bot, conn, s, chat_id=1, site="hh"))
    assert repo.lead_by_hh_id(conn, "1")["status"] == "sent"
    assert repo.lead_by_hh_id(conn, "2")["status"] == "evaluated"
    card_2 = [i for i, m in enumerate(bot.messages, 1) if "ООО 2" in m][0]
    assert bot.deleted == [card_2]                      # the orphan card is gone
    # the next send delivers lead 2 whole — one card, one letter
    bot2 = _FakeBot()
    assert asyncio.run(send_instant_leads(bot2, conn, s, chat_id=1, site="hh")) == 2
    assert len(bot2.messages) == 5                      # header + two leads × (card + letter): no orphan, no twin
    assert any("письмо 2" in m for m in bot2.messages) and repo.lead_by_hh_id(conn, "2")["status"] == "sent"


def test_a_digest_cut_off_with_the_process_is_settled_on_the_next_start():
    s = _settings()
    conn = _db()
    asyncio.run(send_instant_leads(_FakeBot(), conn, s, chat_id=1, site="hh"))
    d = repo.last_digest(conn)
    conn.execute("UPDATE digests SET items_count = 0 WHERE id = ?", (d["id"],))   # as a killed close_digest leaves it
    assert repo.repair_open_digests(conn) == 1
    assert repo.last_digest(conn)["items_count"] == 3
    assert repo.repair_open_digests(conn) == 0
