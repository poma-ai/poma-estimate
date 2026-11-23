#!/usr/bin/env bash
# Build a single-file PyInstaller binary for the current OS.
# Mac (arm64) → dist/poma-estimate-macos-arm64
# Linux (x86_64) → dist/poma-estimate-linux-x86_64
# Windows → dist/poma-estimate-windows.exe  (run via CI, not this script)
set -euo pipefail

cd "$(dirname "$0")"

# uv handles venv + lockfile + group install in one step.
uv sync --extra dev || uv sync --group dev

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$OS" in
    darwin) PLATFORM="macos-${ARCH}" ;;
    linux)  PLATFORM="linux-${ARCH}" ;;
    *)      PLATFORM="$OS-$ARCH" ;;
esac

NAME="poma-estimate-${PLATFORM}"

uv run pyinstaller \
    --onefile \
    --name "${NAME}" \
    --hidden-import tiktoken_ext.openai_public \
    --hidden-import tiktoken_ext \
    --collect-data tiktoken_ext \
    --add-data "src/poma_estimate/data:poma_estimate/data" \
    --optimize 2 \
    --noconfirm \
    src/poma_estimate/__main__.py

echo ""
echo "Built: dist/${NAME}"
ls -lh "dist/${NAME}"
