#!/usr/bin/env bash
# Installs the exact Fe release this project is pinned to (FE_VERSION) into
# ${FE_INSTALL_DIR:-$HOME/.local/bin}, verifying the binary's sha256.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(cat "$ROOT/FE_VERSION")"
DEST="${FE_INSTALL_DIR:-$HOME/.local/bin}"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  ASSET=fe_mac_arm64;   SHA=de045cf2ccd5a4052d92cde7745ff052747235b9c98e745a0995a8ad40a0c78e ;;
  Darwin-x86_64) ASSET=fe_mac_amd64;   SHA=d9330215915347c95e1fb8f6c0bf2f54e76faeb90db57176d4fe2a57c9f25267 ;;
  Linux-x86_64)  ASSET=fe_linux_amd64; SHA=697081cb9265bd1be7a5d33e6bb0c8559ee5ff542abab9a046540fc151b4edaa ;;
  Linux-aarch64|Linux-arm64) ASSET=fe_linux_arm64; SHA=011caa8a297763b3c1540048beb92bea047f0868eabe0ea1b4822e99d945fe26 ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
[ "$VERSION" = "26.6.0" ] || { echo "checksums in this script are for 26.6.0; update them" >&2; exit 1; }
mkdir -p "$DEST"
TMP="$(mktemp)"
curl -fsSL -o "$TMP" "https://github.com/argotorg/fe/releases/download/v${VERSION}/${ASSET}"
GOT="$( (command -v sha256sum >/dev/null && sha256sum "$TMP" || shasum -a 256 "$TMP") | cut -d' ' -f1)"
[ "$GOT" = "$SHA" ] || { echo "checksum mismatch: $GOT" >&2; rm -f "$TMP"; exit 1; }
install -m 0755 "$TMP" "$DEST/fe"; rm -f "$TMP"
xattr -d com.apple.quarantine "$DEST/fe" 2>/dev/null || true
"$DEST/fe" --version
