#!/usr/bin/env bash
# Makes the owner's Firefox ESR start with Marionette enabled (port 2828, localhost only),
# so hh-scout can drive it. Creates a per-user .desktop override; the system file stays untouched.
set -euo pipefail
SRC=/usr/share/applications/firefox-esr.desktop
DST="$HOME/.local/share/applications/firefox-esr.desktop"
mkdir -p "$(dirname "$DST")"
sed 's|^Exec=/usr/lib/firefox-esr/firefox-esr|Exec=/usr/lib/firefox-esr/firefox-esr --marionette|' "$SRC" > "$DST"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Создан $DST (все Exec= получили флаг --marionette)."
echo
echo "Дальше:"
echo "  1. Полностью закройте Firefox (все окна)."
echo "  2. Запустите его из меню приложений (не через x-www-browser в терминале)."
echo "     Если запускаете из терминала: firefox-esr --marionette &"
echo "  3. Проверка: .venv/bin/python scripts/check_browser.py"
echo
echo "Порт Marionette (2828) слушает только 127.0.0.1; в адресной строке about:remote-agent не нужен."
