"""profi.ru specialist cabinet: the orders feed (`/backoffice/n.php`) parsed from rendered HTML.

The cabinet is a JS application without an embedded JSON state, so we read the DOM as the owner's Firefox rendered it.
One feed card (`<a data-testid="<id>_order-snippet" …>`) already carries everything we need — title, full client text,
budget, format + city, preferred dates, client's first name, posting time — so no order page is ever opened.
Fixtures: tests/fixtures/profi_orders.html (anonymised snapshot of 2026-09-10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from hh_scout.browser.hh_pages import clean_text, strip_html
from hh_scout.config import TZ

ORDERS_URL = "https://profi.ru/backoffice/n.php"
ORDER_URL = "https://profi.ru/backoffice/n.php?o={order_id}"
DESCRIPTION_PREFIX = "Пожелания и особенности:"

# markers of the logged-in cabinet layout (any of them) vs. a login/captcha page
_LOGGED_IN_MARKERS = ("_order-snippet", "Вы посмотрели все новые заказы", ">Анкета<", "backoffice/build/")
_BLOCK_MARKERS = ("captcha", "Подтвердите, что вы не робот", "Войти или зарегистрироваться", "Введите номер телефона")

_CARD_START_RE = re.compile(r'<a\b[^>]*data-testid="(\d+)_order-snippet"[^>]*>', re.S)
_ARIA_LABEL_RE = re.compile(r'aria-label="([^"]*)"')
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_P_RE = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.S)  # `<p>` only — never `<path>` inside svg icons
_LI_RE = re.compile(r'<li\b[^>]*aria-label="([^"]*)"[^>]*>(.*?)</li>', re.S)
_HIDDEN_SPAN_RE = re.compile(r'<span[^>]*aria-hidden="true"[^>]*>(.*?)</div>', re.S)
_SPAN_TEXT_RE = re.compile(r"<span[^>]*>([^<]+)</span>", re.S)
_UL_END_RE = re.compile(r"</ul>", re.S)
_MONEY_RE = re.compile(r"(\d[\d\s ]*)\s*₽")

_WORK_FORMAT = {"дистанционно": "remote", "у клиента": "field", "у специалиста": "office"}
_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10,
           "ноя": 11, "дек": 12}


class ProfiBlocked(RuntimeError):
    """The feed page came back without the cabinet: login expired, captcha, or a changed layout."""


@dataclass
class OrderCard:
    order_id: str
    title: str
    description: str
    budget_text: str | None      # "до 5000 ₽", "30 000 ₽", None
    budget_from: int | None
    budget_to: int | None
    work_format: str             # remote / field / office / unknown
    city: str | None
    when: str | None             # "9 сен. (Ср)", "7 сен. (Пн) - 13 сен. (Вс)"
    client: str | None           # client's first name as shown
    posted_text: str | None      # "Вчера в 12:09", "7 сентября"
    published_at: str | None     # ISO with TZ, best effort from posted_text

    @property
    def ext_id(self) -> str:
        return f"profi:{self.order_id}"

    @property
    def url(self) -> str:
        return ORDER_URL.format(order_id=self.order_id)


def _card_html(html: str, start: int) -> str:
    """The <a …>…</a> element starting at `start`, honouring nested <a> tags."""
    depth = 0
    for m in re.finditer(r"<a\b|</a>", html[start:]):
        depth += 1 if m.group(0) == "<a" else -1
        if depth == 0:
            return html[start:start + m.end()]
    return html[start:]


def parse_budget(text: str | None) -> tuple[int | None, int | None]:
    """'до 5000 ₽' -> (None, 5000); '30 000 ₽' -> (30000, 30000); 'от 10 000 ₽' -> (10000, None)."""
    if not text or "₽" not in text:
        return None, None
    nums = [int(digits) for digits in (re.sub(r"\D", "", n) for n in re.findall(r"\d[\d\s ]*", text)) if digits]
    if not nums:
        return None, None
    low = text.casefold()
    if low.startswith("до"):
        return None, nums[0]
    if low.startswith("от"):
        return nums[0], None
    if len(nums) >= 2:
        return nums[0], nums[1]
    return nums[0], nums[0]


def parse_posted(text: str | None, now: datetime | None = None) -> str | None:
    """'Вчера в 12:09' / 'Сегодня в 9:05' / '7 сентября' / '7 сентября в 10:00' / '12:09' -> ISO (Europe/Moscow)."""
    if not text:
        return None
    now = now or datetime.now(TZ)
    low = clean_text(text).casefold()
    tm = re.search(r"(\d{1,2}):(\d{2})", low)
    hour, minute = (int(tm.group(1)), int(tm.group(2))) if tm else (12, 0)
    day = now.date()
    if "вчера" in low:
        day = day - timedelta(days=1)
    elif "сегодня" in low or (tm and not re.search(r"[а-я]{3}", low.replace("в ", ""))):
        pass
    else:
        dm = re.search(r"(\d{1,2})\s+([а-я]+)", low)
        if not dm:
            return None
        month = next((n for k, n in _MONTHS.items() if dm.group(2).startswith(k)), None)
        if month is None:
            return None
        year = now.year - 1 if month > now.month + 1 else now.year
        try:
            day = day.replace(year=year, month=month, day=int(dm.group(1)))
        except ValueError:
            return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).isoformat()


def _parse_card(card: str, now: datetime | None) -> OrderCard | None:
    head = _CARD_START_RE.match(card)
    if not head:
        return None
    order_id = head.group(1)
    label = _ARIA_LABEL_RE.search(head.group(0))
    title = clean_text(label.group(1)) if label and label.group(1).strip() else ""
    if not title:
        h3 = _H3_RE.search(card)
        title = clean_text(strip_html(h3.group(1))) if h3 else f"Заказ {order_id}"

    budget_text = None
    hidden = _HIDDEN_SPAN_RE.search(card)
    if hidden:
        parts = [clean_text(p) for p in _SPAN_TEXT_RE.findall(hidden.group(1))]
        budget_text = " ".join(p for p in parts if p) or None
    if not budget_text:
        for txt in _SPAN_TEXT_RE.findall(card):
            t = clean_text(txt.replace("false", "").replace("true", ""))
            if "₽" in t:
                budget_text = t
                break
    b_from, b_to = parse_budget(budget_text)

    p = _P_RE.search(card)
    description = clean_text(strip_html(p.group(1))) if p else ""
    if description.startswith(DESCRIPTION_PREFIX):
        description = description[len(DESCRIPTION_PREFIX):].strip()

    work_format, city, when = "unknown", None, None
    for li_label, inner in _LI_RE.findall(card):
        text = clean_text(strip_html(inner))
        key = li_label.rstrip(":").strip().casefold()
        if key in _WORK_FORMAT:
            work_format = _WORK_FORMAT[key]
            tail = text.split("·", 1)
            city = clean_text(tail[1]) if len(tail) > 1 else None
        elif "время" in key or "когда" in key:
            when = text or None

    client = posted = None
    ul_end = None
    for m in _UL_END_RE.finditer(card):
        ul_end = m.end()
    if ul_end is not None:
        spans = [clean_text(s) for s in _SPAN_TEXT_RE.findall(card[ul_end:])]
        spans = [s for s in spans if s]
        if spans:
            client = spans[0]
        if len(spans) > 1:
            posted = spans[1]

    return OrderCard(order_id=order_id, title=title, description=description, budget_text=budget_text,
                     budget_from=b_from, budget_to=b_to, work_format=work_format, city=city, when=when,
                     client=client, posted_text=posted, published_at=parse_posted(posted, now))


def is_logged_in(html: str) -> bool:
    return any(m in html for m in _LOGGED_IN_MARKERS)


def looks_blocked(html: str) -> bool:
    low = html.casefold()
    return any(m.casefold() in low for m in _BLOCK_MARKERS)


def parse_orders(html: str, now: datetime | None = None) -> list[OrderCard]:
    """All order cards on the feed page, in page order. Raises ProfiBlocked if this is not the cabinet."""
    if not is_logged_in(html):
        if looks_blocked(html):
            raise ProfiBlocked("profi.ru просит войти или показывает проверку — откройте profi.ru в этом Firefox и войдите")
        raise ProfiBlocked("страница profi.ru без кабинета (нет карточек заказов и меню специалиста) — вход или разметка")
    cards: list[OrderCard] = []
    seen: set[str] = set()
    for m in _CARD_START_RE.finditer(html):
        card = _parse_card(_card_html(html, m.start()), now)
        if card and card.order_id not in seen:
            seen.add(card.order_id)
            cards.append(card)
    return cards
