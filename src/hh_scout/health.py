"""Alarms: tell the owner in Telegram when the day is going wrong, not only how a crawl ended.

Pure checks (unit-tested, no I/O):
* `schedule_checks`  — a window passed without a sitting, no plan, zero page loads by the end of the day, a run
                       stuck in 'running';
* `analyze_report`   — signs of a block or a markup change in a finished crawl (captcha/login, not an applicant,
                       search pages without cards, vacancy pages without data, bridge down);
* `precheck`         — Firefox/Marionette and the Claude bridge shortly before a sitting;
* `day_summary`      — one line for the end of the day: sittings done vs planned, pages, new vacancies, leads;
                       `day_summary_alert` turns it into an Alert only when sittings fell short (a full day is silent).

`Alerter` sends each alert key once per day (kv `alert:<key>:<date>`), so a persistent condition does not spam.
The process-crash case is covered outside Python: systemd `OnFailure=hh-scout-alert.service` → scripts/tg_alert.sh.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Awaitable, Callable

from hh_scout.config import TZ
from hh_scout.db import kv_get, kv_set

log = logging.getLogger(__name__)

Window = tuple[time, time]
WATCHDOG_INTERVAL_MIN = 30
PRECHECK_LEAD_MIN = 30
STALE_RUN_HOURS = 3.0
WINDOW_GRACE_MIN = 30  # a sitting may start this late after its window closes (lead time + restart delay)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=TZ)


def _fmt(w: Window) -> str:
    return f"{w[0].strftime('%H:%M')}–{w[1].strftime('%H:%M')}"


@dataclass(frozen=True)
class Alert:
    key: str    # dedup key, unique per condition per day (e.g. "missed:1")
    text: str


# --- schedule --------------------------------------------------------------------

def schedule_checks(now: datetime, windows: list[Window], run_starts_today: list[datetime], *, next_crawl_at: datetime | None,
                    crawl_running: bool, paused: bool, page_loads_today: int, stale_running_since: datetime | None) -> list[Alert]:
    """What is wrong with today's schedule right now. Called every WATCHDOG_INTERVAL_MIN minutes."""
    alerts: list[Alert] = []
    if paused:
        return alerts
    for idx, (start, end) in enumerate(windows):
        w_start, w_end = _at(now.date(), start), _at(now.date(), end)
        if now < w_end + timedelta(minutes=WINDOW_GRACE_MIN):
            continue  # window still open (or a late start is still possible)
        if not any(w_start <= s <= w_end + timedelta(minutes=WINDOW_GRACE_MIN) for s in run_starts_today):
            alerts.append(Alert(f"missed:{idx}", f"⚠️ Окно {_fmt((start, end))} прошло без подхода. Проверьте /status и журнал: "
                                                 "journalctl -u hh-scout -n 50"))
    if next_crawl_at is None and not crawl_running:
        alerts.append(Alert("noplan", "⚠️ Подход не был назначен — назначаю сейчас. Если повторится, это ошибка планировщика."))
    last_end = _at(now.date(), windows[-1][1])
    if now >= last_end + timedelta(minutes=WINDOW_GRACE_MIN) and page_loads_today == 0 and not crawl_running:
        alerts.append(Alert("zero_day", "🚨 За весь день ни одной загрузки страницы hh.ru — сборы не работали."))
    if stale_running_since is not None and now - stale_running_since > timedelta(hours=STALE_RUN_HOURS):
        alerts.append(Alert("stale_run", f"⚠️ Прогон висит в статусе running с {stale_running_since.astimezone(TZ).strftime('%H:%M')} — "
                                         "процесс, видимо, убит; запись снимется автоматически, новый сбор до этого не стартует."))
    return alerts


def day_summary(now: datetime, windows: list[Window], run_starts_today: list[datetime], *, page_loads_today: int, daily_cap: int,
                new_vacancies: int, leads: int) -> str:
    planned = len(windows)
    done = 0
    for start, end in windows:
        w_start, w_end = _at(now.date(), start), _at(now.date(), end) + timedelta(minutes=WINDOW_GRACE_MIN)
        if any(w_start <= s <= w_end for s in run_starts_today):
            done += 1
    mark = "📊" if done >= planned else "⚠️"
    return (f"{mark} Итог дня: подходов {done} из {planned} · страниц {page_loads_today}/{daily_cap} · "
            f"новых вакансий {new_vacancies} · лидов {leads}")


def day_summary_alert(now: datetime, windows: list[Window], run_starts_today: list[datetime], *, page_loads_today: int,
                      daily_cap: int, new_vacancies: int, leads: int) -> Alert | None:
    """The end-of-day line as an Alert — only when fewer sittings ran than windows; a full day stays quiet."""
    text = day_summary(now, windows, run_starts_today, page_loads_today=page_loads_today, daily_cap=daily_cap,
                       new_vacancies=new_vacancies, leads=leads)
    return Alert("day_summary", text) if text.startswith("⚠️") else None


# --- crawl report ----------------------------------------------------------------

def analyze_report(report) -> list[Alert]:
    """Signs of a block or a broken parser in a finished `CrawlReport` (duck-typed, see pipeline/run.py)."""
    alerts: list[Alert] = []
    be = report.browser_error or ""
    if "без данных" in be or "капча" in be.lower():
        alerts.append(Alert("hh_blocked", "🚫 hh.ru не отдал данные — похоже на капчу или требование войти. Откройте hh.ru в этом "
                                          "Firefox, пройдите проверку/войдите; следующий подход продолжит сам."))
    elif "Marionette" in be or "geckodriver" in be:
        alerts.append(Alert("browser_down", f"🦊 Браузер недоступен: {be}. Запустите Firefox с --marionette (scripts/check_browser.py)."))
    if getattr(report, "not_logged_in", False):
        alerts.append(Alert("not_applicant", "👤 hh.ru видит нас не как соискателя — вход в аккаунт в Firefox не выполнен. "
                                             "Отклики и «подходящие вакансии» не видны."))
    if getattr(report, "search_pages", 0) > 0 and getattr(report, "cards_seen", 0) == 0:
        alerts.append(Alert("no_cards", f"🧩 Поиск загрузил {report.search_pages} страниц, но карточек не нашёл — возможно, "
                                        "hh.ru изменил структуру страницы (HH-Lux-InitialState)."))
    fe = getattr(report, "format_errors", 0)
    if fe >= 3 and fe * 2 >= max(1, fe + report.details):
        alerts.append(Alert("no_vacancy_view", f"🧩 {fe} страниц вакансий без данных за один подход — вероятно, изменилась "
                                               "разметка страницы вакансии."))
    if report.bridge_error:
        alerts.append(Alert("bridge_down", f"🤖 Мост Claude не отвечал: {report.bridge_error}. systemctl status hh-scout-bridge; "
                                           "curl :8766/health"))
    pe = getattr(report, "profi_error", None)
    if pe:
        alerts.append(Alert("profi_blocked", f"🚫 profi.ru: {pe}. Заказы подождут следующего подхода; hh.ru это не затронуло."))
    return alerts


# --- pre-sitting -----------------------------------------------------------------

def precheck(when: datetime, *, marionette_ok: bool, bridge_ok: bool) -> list[Alert]:
    alerts: list[Alert] = []
    t = when.strftime("%H:%M")
    if not marionette_ok:
        alerts.append(Alert("pre_browser", f"🦊 Подход в {t}: Firefox с --marionette не отвечает — сбор сорвётся. "
                                           "Запустите Firefox из меню (или firefox-esr --marionette &)."))
    if not bridge_ok:
        alerts.append(Alert("pre_bridge", f"🤖 Подход в {t}: мост Claude недоступен — вакансии соберутся, но триаж и оценка "
                                          "не пройдут. sudo systemctl restart hh-scout-bridge"))
    return alerts


# --- delivery with per-day dedup -----------------------------------------------------

class Alerter:
    def __init__(self, conn: sqlite3.Connection, notify: Callable[[str], Awaitable[None]]) -> None:
        self.conn = conn
        self.notify = notify

    def already_sent(self, key: str, today: date) -> bool:
        return kv_get(self.conn, f"alert:{key}:{today.isoformat()}") is not None

    def mark_sent(self, key: str, now: datetime) -> None:
        """Remember a key for today without sending anything (a check that passed and need not be repeated)."""
        with self.conn:
            kv_set(self.conn, f"alert:{key}:{now.date().isoformat()}", now.isoformat())

    async def send(self, alerts: list[Alert], now: datetime | None = None) -> int:
        now = now or datetime.now(TZ)
        sent = 0
        for a in alerts:
            if self.already_sent(a.key, now.date()):
                continue
            with self.conn:
                kv_set(self.conn, f"alert:{a.key}:{now.date().isoformat()}", now.isoformat())
                kv_set(self.conn, "alerts_today", str(self.count_today(now.date()) + 1))
            await self.notify(a.text)
            log.warning("Тревога %s: %s", a.key, a.text)
            sent += 1
        return sent

    def count_today(self, today: date) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM kv WHERE key LIKE ?", (f"alert:%:{today.isoformat()}",)).fetchone()
        return int(row["n"])

    def forget_old(self, today: date) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM kv WHERE key LIKE 'alert:%' AND key NOT LIKE ?", (f"%:{today.isoformat()}",))
