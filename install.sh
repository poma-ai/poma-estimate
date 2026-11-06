#!/bin/sh
# POMA estimate installer for macOS and Linux.
#   curl -fsSL https://raw.githubusercontent.com/poma-ai/poma-estimate/main/install.sh | sh
# Downloads one file into ~/.poma-estimate, verifies its checksum, then asks
# which folder to scan. Nothing is uploaded; it only reads the folder you give it.
set -eu

BASE="${POMA_ESTIMATE_BASE:-https://github.com/poma-ai/poma-estimate/releases/latest/download}"
DIR="$HOME/.poma-estimate"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  NAME=poma-estimate-macos-arm64 ;;
  Darwin-x86_64) NAME=poma-estimate-macos-x86_64 ;;
  Linux-x86_64)  NAME=poma-estimate-linux-x86_64 ;;
  *) echo "Sorry, no build for $(uname -s) $(uname -m)." >&2; exit 1 ;;
esac

mkdir -p "$DIR"
TMP="$(mktemp -d "$DIR/.dl.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading $NAME ..."
curl -fSL --progress-bar "$BASE/$NAME" -o "$TMP/bin"
curl -fsSL "$BASE/SHA256SUMS" -o "$TMP/sums" || { echo "Could not download the checksum list. Please retry." >&2; exit 1; }

want="$(awk -v n="$NAME" '$2==n || $2=="*"n {print $1}' "$TMP/sums")"
case "$want" in
  *[!0-9a-fA-F]*|"") echo "No valid checksum for $NAME. Aborting." >&2; exit 1 ;;
esac
[ "${#want}" -eq 64 ] || { echo "No valid checksum for $NAME. Aborting." >&2; exit 1; }
got="$( (shasum -a 256 "$TMP/bin" 2>/dev/null || sha256sum "$TMP/bin") | awk '{print $1}')"
if [ "$want" != "$got" ]; then
  echo "Download is corrupt (checksum mismatch). Nothing was installed." >&2; exit 1
fi

chmod +x "$TMP/bin"
mv -f "$TMP/bin" "$DIR/poma-estimate"
[ "$(uname -s)" = Darwin ] && xattr -c "$DIR/poma-estimate" 2>/dev/null || true

rm -rf "$TMP"
if [ $# -gt 0 ]; then
  exec "$DIR/poma-estimate" "$@"
fi
if folder_in="$( (printf 'Drag the folder to scan into this window, then press Enter: ' >/dev/tty; IFS= read -r l </dev/tty && printf '%s' "$l") 2>/dev/null)" && [ -n "$folder_in" ]; then
  folder="$(printf '%s' "$folder_in" | sed "s/^[\"']//; s/[\"']\$//; s/\\\\\(.\)/\1/g")"
  case "$folder" in "~"*) folder="$HOME${folder#\~}" ;; esac
  exec "$DIR/poma-estimate" "$folder"
fi
echo "Installed. Run:  $DIR/poma-estimate <folder>"
