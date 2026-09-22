#!/usr/bin/env bash
# Boot-chain trust anchor: hash and pin every third-party binary that ends up
# in the boot image. `trust.sh verify` fails the build if anything drifted;
# `trust.sh update` re-pins after an intentional, reviewed upgrade.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/tools/manifest/bootchain.sha256"

FILES=(
  ".freebuff/ref/limine/limine-binary/BOOTX64.EFI"
  ".freebuff/ref/limine/limine-binary/limine-bios.sys"
  ".freebuff/ref/limine/limine-binary/limine-bios-cd.bin"
  ".freebuff/ref/limine/limine-binary/limine-tool-windows-x86/limine.exe"
  ".freebuff/ref/font8x8_basic.h"
)

mode="${1:-verify}"

case "$mode" in
  update)
    mkdir -p "$(dirname "$MANIFEST")"
    {
      echo "# NOVA boot-chain pins (sha256). Managed by scripts/trust.sh update."
      echo "# Any drift between builds is a supply-chain incident: verify fails."
      for f in "${FILES[@]}"; do
        printf '%s  %s\n' "$(sha256sum "$ROOT/$f" | cut -d' ' -f1)" "$f"
      done
    } > "$MANIFEST"
    echo "trust: wrote $MANIFEST (${#FILES[@]} pins)"
    ;;
  verify)
    [ -f "$MANIFEST" ] || { echo "trust: manifest missing — run: scripts/trust.sh update" >&2; exit 1; }
    bad=0
    while IFS= read -r line; do
      case "$line" in ''|'#'*) continue ;; esac
      want="$(printf '%s' "$line" | cut -d' ' -f1)"
      file="$(printf '%s' "$line" | cut -d' ' -f3-)"
      [ -f "$ROOT/$file" ] || { echo "trust: MISSING $file"; bad=1; continue; }
      got="$(sha256sum "$ROOT/$file" | cut -d' ' -f1)"
      if [ "$got" = "$want" ]; then
        echo "trust: OK   $file"
      else
        echo "trust: DRIFT $file (pinned $want, found $got)"
        bad=1
      fi
    done < "$MANIFEST"
    if [ "$bad" -ne 0 ]; then
      echo "trust: VERIFY FAILED" >&2
      exit 1
    fi
    echo "trust: all pins verified"
    ;;
  *)
    echo "usage: scripts/trust.sh [verify|update]" >&2
    exit 2
    ;;
esac
