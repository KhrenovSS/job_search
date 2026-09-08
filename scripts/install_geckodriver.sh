#!/usr/bin/env bash
# Downloads geckodriver (WebDriver proxy for Firefox) into ~/.local/bin.
# Usage: bash scripts/install_geckodriver.sh [version]   (default: latest release)
set -euo pipefail
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION=$(curl -fsSL https://api.github.com/repos/mozilla/geckodriver/releases/latest | python3 -c 'import sys,json; print(json.load(sys.stdin)["tag_name"])')
fi
URL="https://github.com/mozilla/geckodriver/releases/download/${VERSION}/geckodriver-${VERSION}-linux64.tar.gz"
DEST="${HOME}/.local/bin"
mkdir -p "$DEST"
TMP=$(mktemp -d)
echo "Downloading $URL"
curl -fsSL "$URL" -o "$TMP/gd.tar.gz"
tar -xzf "$TMP/gd.tar.gz" -C "$TMP"
install -m 755 "$TMP/geckodriver" "$DEST/geckodriver"
rm -rf "$TMP"
"$DEST/geckodriver" --version | head -1
