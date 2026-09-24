"""Deterministic checks on a cover letter's text — the rules no model is trusted to keep on its own.

Used on the way in (the bridge answer, and again the editor's answer) and on the way out (`ranker.format_letter`
sends a letter written days ago from the database verbatim). `CODE_RULES` is folded into the letter's rules
stamp (`cover_letter.rules_hash`), so tightening a regex here makes stored letters stale the same way a prompt
edit does — decision #46 was born from a code rule that stored letters violated.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# The letter must never quote money — neither the owner's figure nor the vacancy's (decisions #22-24).
# 1) a figure next to a currency word; 2) a five-or-six-digit figure (with or without a thousands space) next to
# money context; 3) «ставка от 2500», «2500 в час»; 4) shorthand like 300k / 300к; 5) a percentage under 100.
# A phone (+7 …, digits with dashes), a vacancy id, a year, «100 %» and a project size («SCADA на 2500 тегов»)
# are not money and must not trip the rule.
_CONTEXT = r"(?:вилк|ставк|бюджет|оклад|доход|зарплат|ориентир|сумм|стоимост|оплат|гонорар|тариф|смет)"
_SPACE = "[   ]?"
_NOT_A_SIZE = r"(?!\s*(?:тег|точ|сигнал|параметр|переменн))"                 # 2500 тегов / точек / сигналов
_FIGURE = r"(?<![\d+\-()])\d{2,3}" + _SPACE + r"\d{3}(?![\d\-])" + _NOT_A_SIZE   # 120 000 / 120000, not a phone or an id
MONEY_RE = re.compile(
    r"\d[\d   ]{2,}\s*(?:₽|руб|р\.|тыс|на руки)"
    r"|(?:₽|руб|тыс)\s*\d"
    r"|" + _CONTEXT + r"[^\n.]{0,40}?" + _FIGURE +
    r"|" + _FIGURE + r"[^\n.]{0,40}?" + _CONTEXT +
    r"|" + _CONTEXT + r"[^\n.]{0,25}?(?<![\d+\-()])\d{4,6}(?![\d\-])" + _NOT_A_SIZE +
    r"|(?<![\d+\-()])\d{3,6}\s*(?:в|за)\s*(?:час|день|смену|месяц|мес\b)"
    r"|(?<![\w+\-])\d{2,3}\s?[kк](?![\w-])"
    r"|(?<!\d)[1-9]\d?\s?%",
    re.IGNORECASE)

# Phrases that make the letter read as a mailshot or as flattery of the recruiter.
BANNED = ("помогу закрыть", "закрыть позицию", "закрыть вакансию", "уникальн", "инновацион", "уважаемые",
          "динамично развивающ", "выполните kpi", "сэкономите на зарплате", "сэкономите бюджет", "сэкономить бюджет")

# The letter must never sort its reader by job title: "Для отдела кадров: подряд не требует…" reads as a mailshot,
# and the owner deleted that label by hand from every letter that had it (v9.6, decision #38). Cutting the label is
# cheaper and surer than regenerating the whole letter, so it is cut, not rejected.
# Three shapes: «(Отдельно) для <кого-то>:», «<Отделу|Службе|Менеджеру …> <кадров|подбора…>:» and
# «Кадровой службе:» / «HR-отделу:» opening a sentence.
ROLE_ADDRESS_RE = re.compile(
    r"(?:\A|\n|(?<=\.)[ \t])[ \t]*(?:"
    r"(?:отдельно\s+)?для\s+[^:\n]{0,60}?(?:кадр|подбор|персонал|hr|рекрут|руководител|директор|начальник|менеджер)[^:\n]{0,20}"
    r"|(?:отдел[уа]?|служб[еа]|менеджер[уа]?|специалист[уа]?|руководител[юя]|директор[уа]?|коллег[иа]м?)\s+[^:\n]{0,40}?"
    r"(?:кадр|подбор|персонал|hr|рекрут|проект|производств)[^:\n]{0,20}"
    r"|(?:кадров\w*\s+|hr[- ]?)(?:служб|отдел|част|специалист|менеджер)\w*"
    r"):[ \t]*",
    re.IGNORECASE)

PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# The stamp is the text of the rules themselves, so any edit above changes it without anyone remembering to bump it.
# The signature rule lives in `signature_problem`, not in a regex, so its shape is stamped by hand (decision #58).
CODE_RULES = "\n".join((MONEY_RE.pattern, "|".join(BANNED), ROLE_ADDRESS_RE.pattern, "signature:4-lines/ИП/phone/email"))


def strip_role_address(text: str) -> str:
    """Drop a "Для отдела кадров:" label, keeping the sentence it introduced (decision #38).

    The owner did exactly this by hand before sending: the thought is right, naming the reader's job is not.
    """
    out: list[str] = []
    cuts: list[int] = []   # where in the result the sentence that lost its label now begins
    last = pos = 0
    for m in ROLE_ADDRESS_RE.finditer(text):
        log.info("Убрал из письма обращение по должности: «%s»", m.group(0).strip())
        lead = m.group(0)[0]
        chunk = text[last:m.start()] + (lead if lead.isspace() else "")   # keep the break, drop the label
        out.append(chunk)
        pos += len(chunk)
        cuts.append(pos)
        last = m.end()
    if not cuts:
        return text
    out.append(text[last:])
    chars = list("".join(out))
    for i in cuts:                       # the label carried the capital letter — give it back
        if i < len(chars):
            chars[i] = chars[i].upper()
    return "".join(chars)


def money_problem(text: str) -> str:
    return "в тексте есть денежная сумма — ни одной цифры про деньги быть не должно" if MONEY_RE.search(text) else ""


def cliche_problem(text: str) -> str:
    low = text.lower()
    for phrase in BANNED:
        if phrase in low:
            return f"запрещённый оборот «{phrase}» — письмо должно быть деловым, без штампов и лести"
    return ""


def signature_problem(text: str) -> str:
    """The last four non-empty lines must be: name, «… работаю по договору (ИП)», phone, e-mail (prompt §7).

    The one structural rule no regex could *repair* (decision #46) — but it can be *checked*, and a letter
    without the format line is the one the reader cannot place months later. The e-mail line came with
    decision #58: a customer often finds it easier to write or to send documents than to call.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 4:
        return "подпись не найдена — в конце должны быть четыре строки: имя, формат работы, телефон, e-mail"
    name, fmt, phone, email = lines[-4], lines[-3], lines[-2], lines[-1]
    if not EMAIL_RE.search(email) or len(email) > 60:
        return "последняя строка подписи должна быть e-mail из резюме"
    if not PHONE_RE.search(phone) or len(phone) > 40:
        return "предпоследняя строка подписи должна быть телефоном из резюме"
    if "ип" not in fmt.lower() or len(fmt) > 120:
        return "вторая строка подписи должна называть формат работы: «… работаю по договору (ИП)»"
    if len(name) > 60 or name.endswith((".", "!", "?")):
        return "первая строка подписи должна быть именем из резюме, без предложений"
    return ""
