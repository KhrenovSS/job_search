"""Owner commands."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import sqlite3
from datetime import datetime, timedelta

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hh_scout.bot.digest import send_digest
from hh_scout.bot.feedback import awaiting_reason, set_awaiting_reason
from hh_scout.bot.keyboards import vote_kb
from hh_scout.bot.lead_actions import cleanup_stale, collapse_lead
from hh_scout.config import TZ, Settings
from hh_scout.db import kv_get
from hh_scout.llm.cover_letter import CoverLetterWriter
from hh_scout.pipeline import outcomes, repo
from hh_scout.pipeline.digest_builder import record_manual_letter
from hh_scout.pipeline.ranker import format_card, format_inbox, format_letter, format_outcome_dimensions
from hh_scout.pipeline.rows import letter_key, row_site

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = (
    "<b>HH-Scout</b> — лиды с hh.ru (и заказы с profi.ru, если включено) для сотрудничества по ИП.\n"
    "Дайджест приходит каждый день в {digest}. Сбор — три подхода в день, старт в случайное время в окнах {windows}; "
    "внутри подхода бот листает сериями ~10 мин с паузами ~5 мин.\n\n"
    "/status — состояние: Firefox, мост, последний и следующий сбор, лимиты\n"
    "/inbox — открытые (необработанные) лиды: с чего продолжить\n"
    "/done &lt;hh_id&gt; — отметить «написал», свернуть карточку\n"
    "/cleanup [дней] — свернуть открытые лиды старше N дней (по умолчанию 14)\n"
    "/digest — прислать накопленные лиды сейчас\n"
    "/crawl [N] — запустить сбор сейчас: не больше N страниц (без N — весь остаток дневного лимита)\n"
    "/next — когда следующий подход\n"
    "/pause · /resume — приостановить/возобновить автоматические сборы\n"
    "/skipped [N] — последние отсеянные вакансии с причинами\n"
    "/stats [дней] — что ответили компании: письма, ответы, приглашения и отказы по полосам балла\n"
    "/letter &lt;hh_id&gt; [пожелание] — написать или переписать отклик "
    "(«/letter 137256632 больше про SCADA»)\n"
    "/help — эта справка\n\n"
    "Кнопки под карточкой: 👍/👎 — обратная связь для ИИ (👎 сворачивает карточку; «✍️ своими словами» — "
    "напишите причину ответом на вопрос бота, оценщик её учтёт), "
    "✅ Написал — отклик отправлен (карточка сворачивается, письмо удаляется), ⏸ Позже — отложить."
)


def _fmt_dt(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw).astimezone(TZ).strftime("%d.%m %H:%M")
    except ValueError:
        return raw


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@router.message(Command("start", "help"))
async def help_cmd(m: Message, settings: Settings) -> None:
    await m.answer(HELP.format(digest=settings.digest_time, windows=settings.crawl_windows.replace(",", ", ")))


@router.message(Command("status"))
async def status_cmd(m: Message, settings: Settings, conn: sqlite3.Connection, scheduler) -> None:
    ff = _port_open(settings.marionette_host, settings.marionette_port)
    bridge = "недоступен"
    try:
        r = await asyncio.to_thread(httpx.get, f"{settings.bridge_url}/health", timeout=5)
        if r.status_code == 200:
            bridge = f"ok, модель {r.json().get('model')}"
    except Exception:  # noqa: BLE001
        pass
    last = repo.last_run(conn)
    counts = repo.count_by_status(conn)
    used = repo.page_loads_today(conn)
    cap = scheduler.daily_cap()
    nxt = scheduler.next_crawl_at()
    idx = scheduler.next_window_idx()
    n_windows = len(scheduler.windows)
    db_mb = settings.db_path.stat().st_size / 1e6 if settings.db_path.exists() else 0
    lines = [
        "<b>Состояние HH-Scout</b>",
        f"Firefox/Marionette: {'✅ доступен' if ff else '❌ не отвечает (запустите Firefox с --marionette)'}",
        f"Мост Claude: {bridge}",
        f"Автосбор: {'⏸ пауза' if scheduler.paused() else '▶️ включён'} · сбор идёт: {'да' if scheduler.crawl_lock.locked() else 'нет'}",
        "Следующий подход: " + (f"{nxt.strftime('%d.%m %H:%M')} (окно {idx + 1 if idx is not None else '?'} из {n_windows})" if nxt
                              else "назначится после текущего сбора"),
        f"Загрузок страниц сегодня: {used} / {cap} (лимит дня) · в очереди описаний: {counts.get('to_fetch', 0)}",
    ]
    if last:
        lines.append(f"Последний прогон: {_fmt_dt(last['started_at'])} → {last['status']} ({last['trigger']}); "
                     f"страниц {last['page_loads'] or 0}, новых {last['collected'] or 0}, оценено {last['evaluated'] or 0}"
                     + (f"; {last['error']}" if last['error'] else ""))
    order = ["new", "triage", "to_fetch", "prefiltered", "evaluated", "sent", "rejected", "skipped", "evaluation_failed"]
    lines.append("Вакансии: " + " · ".join(f"{k} {counts[k]}" for k in order if counts.get(k)))
    if "owen_si" in settings.company_channels_set:
        t = repo.owen_totals(conn)
        lines.append(f"Каталог ОВЕН: компаний {t['total']} · ждут допуска {t['waiting']} · отправлено {t['sent']} "
                     f"(по {settings.company_leads_per_day} в день)")
    if repo.PLANT_PASS in settings.company_channels_set:
        t = repo.plant_totals(conn)
        lines.append(f"Эксплуатанты автоматики: в пуле {t['pool']} · ждут страницы {t['to_fetch']} · отправлено {t['sent']} "
                     f"(по {settings.plant_leads_per_day} в день)")
    lines.append("profi.ru: " + (f"✅ включён · заказов в базе {repo.count_site(conn, 'profi')}" if settings.profi_enabled
                                else "выключен (PROFI_ENABLED=false)"))
    wd = kv_get(conn, "watchdog_last")
    sending = kv_get(conn, "sending_since")
    lines.append("Отправка лидов идёт: " + (f"да, с {_fmt_dt(sending)} — не перезапускать" if sending else "нет"))
    lines.append(f"Сторож: последняя проверка {_fmt_dt(wd)} · тревог сегодня {scheduler.alerter.count_today(datetime.now(TZ).date())}")
    bk = kv_get(conn, "last_backup")
    bk_when, _, bk_name = (bk or "").partition("|")
    lines.append(f"БД: {db_mb:.1f} МБ · копия: " + (f"{_fmt_dt(bk_when)} ({bk_name})" if bk else "ещё не делалась"))
    await m.answer("\n".join(lines))


@router.message(Command("digest"))
async def digest_cmd(m: Message, settings: Settings, conn: sqlite3.Connection, scheduler, send_lock: asyncio.Lock) -> None:
    async with send_lock:   # never interleaved with the instant sends of a sitting that is just finishing
        n = await send_digest(m.bot, conn, settings, m.chat.id, note="manual /digest",
                              evaluate=not scheduler.crawl_lock.locked())
    if n:
        await m.answer(f"Отправлено лидов: {n}")


@router.message(Command("crawl"))
async def crawl_cmd(m: Message, command: CommandObject, scheduler) -> None:
    if scheduler.crawl_lock.locked():
        await m.answer("Сбор уже идёт")
        return
    budget = int(command.args) if command.args and command.args.strip().isdigit() else None
    await scheduler.trigger_manual_crawl(budget)


@router.message(Command("next"))
async def next_cmd(m: Message, scheduler, settings: Settings) -> None:
    nxt = scheduler.next_crawl_at()
    await m.answer(f"Следующий подход: {nxt.strftime('%d.%m в %H:%M')}" if nxt
                   else "Сбор идёт сейчас; следующий подход назначится после него.")


@router.message(Command("pause"))
async def pause_cmd(m: Message, scheduler) -> None:
    scheduler.set_paused(True)
    await m.answer("⏸ Автоматические сборы приостановлены. /resume — возобновить. Дайджест в 12:00 по-прежнему придёт.")


@router.message(Command("resume"))
async def resume_cmd(m: Message, scheduler) -> None:
    scheduler.set_paused(False)
    await m.answer("▶️ Автоматические сборы возобновлены.")


@router.message(Command("stats"))
async def stats_cmd(m: Message, command: CommandObject, settings: Settings, conn: sqlite3.Connection) -> None:
    """What the letters bought: by score band (the calibration the threshold rests on), then by feature.

    Both tables come from the same rows, the same maturity rule and the same meaning of «ответ» (v9.11).
    """
    days = min(int(command.args), 365) if command.args and command.args.strip().isdigit() else 30
    mature_days = settings.outcome_mature_days
    rows = repo.outcome_rows(conn, repo.iso_utc(datetime.now(TZ) - timedelta(days=days)))
    if not rows:
        await m.answer(f"За {days} дн. писем ещё не отправлено — считать нечего.")
        return
    bands = outcomes.outcome_stats(rows, mature_days)
    written = sum(int(b["written"]) for b in bands)
    young = outcomes.maturing(rows, mature_days)
    lines = [f"<b>Что ответили компании за {days} дн.</b> (письма старше {mature_days} дн.)",
             "<pre>полоса  писем  ответ  пригл.  отказ</pre>"]
    for b in bands:
        lines.append(f"<pre>{str(b['band']):<7} {b['written']:>5}  {b['answered']:>5}  {b['invited']:>6}  {b['refused']:>5}</pre>")
    blind = sum(int(b["blind"]) for b in bands)
    lines.append(f"Всего писем {written} · ответов {sum(int(b['answered']) for b in bands)} · "
                 f"приглашений {outcomes.invited_count(rows)}")
    if young:
        lines.append(f"Дозревает: {young} — письма моложе {mature_days} дн., их молчание пока ничего не значит.")
    if blind:
        lines.append(f"Без канала измерения: {blind} — письмо ушло мимо hh.ru, ответ компании нам не виден.")
    await m.answer("\n".join(lines))
    blocks = [(title, outcomes.outcome_by(rows, key, mature_days)) for title, key in outcomes.DIMENSIONS]
    if any(rows_ for _, rows_ in blocks):
        await m.answer(format_outcome_dimensions(blocks, outcomes.reply_delay_curve(rows), mature_days))


@router.message(Command("skipped"))
async def skipped_cmd(m: Message, command: CommandObject, conn: sqlite3.Connection) -> None:
    n = int(command.args) if command.args and command.args.isdigit() else 15
    rows = repo.recent_skipped(conn, min(n, 50))
    if not rows:
        await m.answer("Отсеянных пока нет.")
        return
    lines = [f"• <b>{r['title'][:60]}</b> — {r['employer'] or '—'}: {r['status']}/{r['skip_reason'] or ''}"
             + (f" ({r['triage_note']})" if r['triage_note'] and r['skip_reason'] == 'triage' else "") for r in rows]
    await m.answer("Последние отсеянные:\n" + "\n".join(lines))


@router.message(Command("inbox"))
async def inbox_cmd(m: Message, conn: sqlite3.Connection) -> None:
    for part in format_inbox(repo.open_leads(conn)):
        await m.answer(part)


@router.message(Command("done"))
async def done_cmd(m: Message, command: CommandObject, conn: sqlite3.Connection) -> None:
    hh_id = (command.args or "").strip()
    if not _valid_id(hh_id):
        await m.answer("Использование: /done &lt;id&gt; — число из ссылки hh.ru/vacancy/… или profi:&lt;номер заказа&gt;")
        return
    row = repo.lead_by_hh_id(conn, hh_id)
    if row is None:
        await m.answer("Такой вакансии нет среди оценённых.")
        return
    with conn:
        repo.add_feedback(conn, row["id"], +1)
    if await collapse_lead(m.bot, conn, m.chat.id, row["id"], "responded"):
        await m.answer(f"✅ Отмечено: написал — {row['employer'] or ''} · {row['title'][:60]}")
    else:
        await m.answer("Этот лид уже закрыт или ещё не отправлялся.")


@router.message(Command("cleanup"))
async def cleanup_cmd(m: Message, command: CommandObject, conn: sqlite3.Connection) -> None:
    days = int(command.args) if command.args and command.args.strip().isdigit() else 14
    n = await cleanup_stale(m.bot, conn, m.chat.id, days)
    await m.answer(f"⌛ Свёрнуто как устаревшие: {n} (открытые лиды старше {days} дн.)")


@router.message(Command("letter"))
async def letter_cmd(m: Message, command: CommandObject, settings: Settings, conn: sqlite3.Connection) -> None:
    hh_id, _, hint = (command.args or "").strip().partition(" ")
    hint = hint.strip()
    if not _valid_id(hh_id):
        await m.answer("Использование: /letter &lt;id&gt; [пожелание] — число из ссылки hh.ru/vacancy/… "
                       "или profi:&lt;номер заказа&gt;.\nНапример: <code>/letter 137256632 больше про SCADA</code>")
        return
    row = repo.lead_by_hh_id(conn, hh_id)
    if row is None:
        await m.answer("Такой оценённой вакансии нет в базе.")
        return
    writer = CoverLetterWriter(settings, conn)
    answered = writer.answered_employer(row)
    if answered is not None:   # письмо уходит кадровику всей организации — второе на тот же стол не нужно
        await m.answer(f"Вы уже откликались в «{row['employer'] or '—'}»: «{answered['title'] or '—'}» "
                       f"({answered['hh_id']}), {(answered['answered_at'] or '')[:10]} — письмо не переписываю.")
        return
    await m.answer("Пишу отклик…" + (f" С учётом: {hint}" if hint else ""))
    try:
        text = await asyncio.to_thread(writer.write_for, row, None, hint or None)
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не удалось: {e}")
        return
    if not text:
        await m.answer("ИИ вернул текст неподходящей длины, попробуйте ещё раз.")
        return
    row = repo.lead_by_hh_id(conn, hh_id)
    card = await m.answer(format_card(1, row, row), reply_markup=vote_kb(row["id"]))
    letter = await m.answer(format_letter(row["employer"], text, letter_key(row)))
    # a queue lead the owner just received is a sent lead from here on: closable, counted, never re-sent
    record_manual_letter(conn, settings, row, getattr(card, "message_id", None), getattr(letter, "message_id", None))


MAX_REASON_CHARS = 300


@router.message(F.reply_to_message, F.text)
async def reason_reply(m: Message, conn: sqlite3.Connection) -> None:
    """The owner's own words on a 👎 (v9.11): a reply to the bot's question after «✍️ своими словами»."""
    pending = awaiting_reason(conn)
    if pending is None or m.reply_to_message.message_id != pending[1]:
        return
    vacancy_id, _ = pending
    text = (m.text or "").strip()[:MAX_REASON_CHARS]
    if not text:
        return
    with conn:
        repo.update_feedback_reason(conn, vacancy_id, text)
    set_awaiting_reason(conn, None)
    row = repo.vacancy_by_id(conn, vacancy_id)
    card_id, _ = repo.lead_messages(conn, vacancy_id)
    if row is not None and card_id:
        from hh_scout.pipeline.ranker import format_collapsed
        try:
            await m.bot.edit_message_text(format_collapsed("disliked", row, reason=text), chat_id=m.chat.id, message_id=card_id)
        except Exception as e:  # noqa: BLE001 — the card may already be gone; the reason is stored anyway
            log.warning("Не удалось дописать причину в карточку %s: %s", vacancy_id, e)
    await m.answer("Записал — оценщик учтёт при следующих оценках.")


_ID_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")


def _valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value))
