"""Select leads for a digest and record what was sent. Telegram I/O lives in bot/digest.py."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from hh_scout.config import Settings
from hh_scout.pipeline import dedup, repo

log = logging.getLogger(__name__)


@dataclass
class DigestPlan:
    leads: list[sqlite3.Row]        # what is left of today's quota, best first by queue priority
    checked: int
    waiting: list[sqlite3.Row]      # the next ones in the queue, listed by name in the tail
    waiting_total: int = 0          # how many are waiting altogether
    sent_today: int = 0             # leads already delivered today by the instant sends (v9.8)


def plan_digest(conn: sqlite3.Connection, settings: Settings) -> DigestPlan:
    """The day's quota off the top of the lead queue, plus the tail that keeps waiting.

    The queue outlives the day on purpose (v9.1): a strong vacancy found tomorrow goes before a weak one that
    waited, and the weak one is not written off — it waits its turn or expires after `QUEUE_TTL_DAYS`.
    """
    dedup.dedupe_evaluated(conn, settings)  # one lead per company (writes skipped/duplicate_employer; idempotent)
    with conn:
        gone = repo.expire_queue(conn, settings.queue_ttl_days)
    if gone:
        log.info("Из очереди выбыло по сроку (%d дн.): %d", settings.queue_ttl_days, gone)
    # Since v9.8 leads go out right after every sitting, so by noon most of the quota is usually spent.
    # What is left here is the catch-up: leads whose letter was not ready in time, or that arrived while
    # the quota was momentarily full.
    sent_today = repo.leads_sent_today(conn)
    quota = max(0, settings.digest_max_items - sent_today)
    leads = repo.lead_queue(conn, settings.score_threshold, quota, wait_bonus_max=settings.queue_wait_bonus_max) if quota else []
    waiting = repo.lead_queue(conn, settings.score_threshold, settings.digest_tail_items,
                              wait_bonus_max=settings.queue_wait_bonus_max, offset=len(leads))
    total = repo.queue_size(conn, settings.score_threshold)
    return DigestPlan(leads=leads, checked=repo.evaluations_since_last_digest(conn, daily_only=True),
                      waiting=waiting, waiting_total=max(0, total - len(leads)), sent_today=sent_today)


def finalize_digest(conn: sqlite3.Connection, settings: Settings, sent: list[tuple],
                    checked: int, note: str | None = None, reject: bool = True) -> int:
    """Record the digest, mark sent leads `sent` and (unless `reject=False`) everything below threshold `rejected`.

    `sent` items are (row, card_message_id) or (row, card_message_id, letter_message_id).
    `reject=False` is for instant sends between digests: the noon digest still owns the below-threshold cleanup.
    """
    with conn:
        digest_id = repo.create_digest(conn, len(sent), checked, note)
        for pos, item in enumerate(sent, 1):
            row, msg_id = item[0], item[1]
            letter_id = item[2] if len(item) > 2 else None
            repo.add_digest_item(conn, digest_id, row["id"], pos, msg_id, letter_id)
        rejected = repo.reject_below(conn, settings.score_threshold) if reject else 0
    log.info("Дайджест #%d: отправлено %d, отклонено ниже порога %d, проверено %d", digest_id, len(sent), rejected, checked)
    return digest_id


def record_manual_letter(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row,
                         card_message_id: int | None, letter_message_id: int | None) -> None:
    """`/letter <id>` on a lead still in the queue: the owner now holds its card and letter, so it is a sent lead
    like any other — closable with «✅ Написал», counted by /stats, and never delivered a second time (v9.11).
    """
    if row["status"] != "evaluated":
        return
    finalize_digest(conn, settings, [(row, card_message_id, letter_message_id)], checked=0,
                    note="manual:/letter", reject=False)


def mark_previewed_as_sent(conn: sqlite3.Connection, settings: Settings, note: str) -> int:
    """First-start helper: leads the owner already saw as previews must not be re-sent."""
    leads = repo.evaluated_leads(conn, settings.score_threshold)
    if not leads:
        return 0
    finalize_digest(conn, settings, [(r, None) for r in leads], checked=repo.evaluations_since_last_digest(conn), note=note)
    return len(leads)
