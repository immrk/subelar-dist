#!/usr/bin/env sh
# Subelar CLI One-Line Installer & Updater (macOS / Linux)
# Usage: curl -fsSL https://raw.githubusercontent.com/immrk/subelar-dist/main/install.sh | sh
set -eu

DIST_REPO="${SUBELAR_DIST_REPO:-immrk/subelar-dist}"
INSTALL_DIR="${SUBELAR_INSTALL_DIR:-$HOME/.local/bin}"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$OS" in
  darwin) PLATFORM="darwin" ;;
  linux)  PLATFORM="linux" ;;
  *)
    echo "❌ Unsupported OS: $OS (Use install.ps1 on Windows)" >&2
    exit 1
    ;;
esac

case "$ARCH" in
  x86_64|amd64) ARCH_TAG="amd64" ;;
  arm64|aarch64) ARCH_TAG="arm64" ;;
  *)
    echo "❌ Unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

ASSET_NAME="subelar-${PLATFORM}-${ARCH_TAG}"
DOWNLOAD_URL="https://github.com/${DIST_REPO}/releases/latest/download/${ASSET_NAME}"

echo "▶ Installing Subelar CLI (${ASSET_NAME}) from ${DIST_REPO}..."
mkdir -p "$INSTALL_DIR"

TMP_BIN="$(mktemp)"
trap 'rm -f "$TMP_BIN"' EXIT

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$DOWNLOAD_URL" -o "$TMP_BIN"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMP_BIN" "$DOWNLOAD_URL"
else
  echo "❌ Neither curl nor wget found." >&2
  exit 1
fi

chmod +x "$TMP_BIN"
mv "$TMP_BIN" "${INSTALL_DIR}/subelar"

echo "✅ Subelar CLI installed to ${INSTALL_DIR}/subelar"
case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    echo "💡 Add ${INSTALL_DIR} to your PATH:"
    echo "   export PATH=\"${INSTALL_DIR}:\$PATH\""
    ;;
esac

echo "🚀 Run 'subelar init' or 'subelar doctor' to get started."
