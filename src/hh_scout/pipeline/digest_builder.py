"""Select leads for a digest and record what was sent. Telegram I/O lives in bot/digest.py."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from hh_scout.config import Settings
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)


@dataclass
class DigestPlan:
    leads: list[sqlite3.Row]
    checked: int


def plan_digest(conn: sqlite3.Connection, settings: Settings) -> DigestPlan:
    leads = repo.evaluated_leads(conn, settings.score_threshold, settings.digest_max_items)
    return DigestPlan(leads=leads, checked=repo.evaluations_since_last_digest(conn))


def finalize_digest(conn: sqlite3.Connection, settings: Settings, sent: list[tuple],
                    checked: int, note: str | None = None) -> int:
    """Record the digest, mark sent leads `sent` and everything below threshold `rejected`.

    `sent` items are (row, card_message_id) or (row, card_message_id, letter_message_id).
    """
    with conn:
        digest_id = repo.create_digest(conn, len(sent), checked, note)
        for pos, item in enumerate(sent, 1):
            row, msg_id = item[0], item[1]
            letter_id = item[2] if len(item) > 2 else None
            repo.add_digest_item(conn, digest_id, row["id"], pos, msg_id, letter_id)
        rejected = repo.reject_below(conn, settings.score_threshold)
    log.info("Дайджест #%d: отправлено %d, отклонено ниже порога %d, проверено %d", digest_id, len(sent), rejected, checked)
    return digest_id


def mark_previewed_as_sent(conn: sqlite3.Connection, settings: Settings, note: str) -> int:
    """First-start helper: leads the owner already saw as previews must not be re-sent."""
    leads = repo.evaluated_leads(conn, settings.score_threshold)
    if not leads:
        return 0
    finalize_digest(conn, settings, [(r, None) for r in leads], checked=repo.evaluations_since_last_digest(conn), note=note)
    return len(leads)
