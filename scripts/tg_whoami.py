"""One-time Telegram setup: find the owner's chat_id and write it into .env.

Usage: .venv/bin/python scripts/tg_whoami.py [--timeout 90]
Reads TG_BOT_TOKEN from .env, checks the bot with getMe, then waits for the owner to send
/start to the bot, prints the chat_id, stores TG_OWNER_CHAT_ID in .env and sends a confirmation.
No aiogram here — plain Bot API over httpx so it works before the bot itself exists.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


def read_env(key: str) -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def write_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    if re.search(rf"^{key}=.*$", text, re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text += f"\n{key}={value}\n"
    ENV.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=90, help="seconds to wait for /start")
    args = ap.parse_args()

    token = read_env("TG_BOT_TOKEN")
    if not token:
        print("В .env нет TG_BOT_TOKEN — впишите токен от @BotFather и запустите снова.")
        return 1
    api = f"https://api.telegram.org/bot{token}"
    me = httpx.get(f"{api}/getMe", timeout=15).json()
    if not me.get("ok"):
        print("Telegram отверг токен:", me)
        return 1
    bot = me["result"]
    print(f"Бот найден: @{bot['username']} ({bot.get('first_name')})")

    existing = read_env("TG_OWNER_CHAT_ID")
    if existing:
        print(f"TG_OWNER_CHAT_ID уже задан: {existing} — перезаписываю, если получу /start.")

    print(f"Напишите боту @{bot['username']} команду /start. Жду до {args.timeout} с…")
    offset = None
    deadline = time.time() + args.timeout
    chat = None
    while time.time() < deadline and chat is None:
        params = {"timeout": 10}
        if offset is not None:
            params["offset"] = offset
        try:
            upd = httpx.get(f"{api}/getUpdates", params=params, timeout=20).json()
        except httpx.HTTPError as e:
            print("сеть:", e)
            time.sleep(2)
            continue
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if msg and msg.get("chat"):
                chat = msg["chat"]
                sender = msg.get("from", {})
                print(f"Получено сообщение {msg.get('text')!r} от {sender.get('first_name')} @{sender.get('username')}")
                break
    if chat is None:
        print("Сообщений не пришло. Проверьте, что написали именно этому боту, и запустите снова.")
        return 1

    chat_id = str(chat["id"])
    write_env("TG_OWNER_CHAT_ID", chat_id)
    print(f"chat_id = {chat_id} — записан в .env как TG_OWNER_CHAT_ID")
    r = httpx.post(f"{api}/sendMessage", json={
        "chat_id": chat_id,
        "text": "HH-Scout подключён ✅\nЭтот чат будет получать ежедневную подборку вакансий в 12:00.",
    }, timeout=15).json()
    print("Подтверждение отправлено." if r.get("ok") else f"Не удалось отправить: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
