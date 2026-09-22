#!/usr/bin/env bash
# Build build/nova.hdd: GPT + FAT16 ESP carrying the kernel, Limine UEFI
# loaders, limine.conf, and limine-bios.sys — then patch BIOS boot stages
# with `limine bios-install`. Works on Linux and Windows Git Bash; needs no
# xorriso/mtools (the FAT image is built by tools/make-esp.py).
#
# Result boots on BOTH SeaBIOS (legacy BIOS) and UEFI firmware.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KERNEL_DIR="$REPO_ROOT/kernel"
PROFILE="${1:-release}"

# shellcheck disable=SC1091
source "$KERNEL_DIR/scripts/fetch-limine.sh"

KERNEL_ELF="$KERNEL_DIR/target/x86_64-unknown-none/$PROFILE/nucleus"
if [ ! -f "$KERNEL_ELF" ]; then
  echo "build-disk: kernel ELF missing at $KERNEL_ELF (run: make build)" >&2
  exit 1
fi

OUT="$REPO_ROOT/build/nova.hdd"
CONF="$KERNEL_DIR/limine.conf"
if [ "${NOVA_TEST:-0}" = "1" ]; then
  CONF="$KERNEL_DIR/limine-selftest.conf"
  OUT="$REPO_ROOT/build/nova-selftest.hdd"
fi
mkdir -p "$REPO_ROOT/build"

# Resolve a working Python launcher (Windows Store stubs make `python3`
# unreliable; `py -3` is the real launcher there).
PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1 && python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
elif [ -f "/c/Windows/py.exe" ]; then PYCMD=(/c/Windows/py.exe -3)
fi
[ ${#PYCMD[@]} -gt 0 ] || { echo "build-disk: no usable python launcher found" >&2; exit 1; }
echo "build-disk: python launcher: ${PYCMD[*]}"

"${PYCMD[@]}" "$REPO_ROOT/tools/make-esp.py" "$KERNEL_DIR" "$LIMINE_BIN_DIR" "$OUT" "$KERNEL_ELF" "$CONF"

# Find the host `limine` tool: vendored Windows exe, vendored binary, or PATH.
TOOL=""
for C in "$LIMINE_BIN_DIR/limine-tool-windows-x86/limine.exe" \
         "$LIMINE_BIN_DIR/limine.exe" \
         "$LIMINE_BIN_DIR/limine-tool-windows-x86/limine-tool-windows-x86" \
         "$LIMINE_BIN_DIR/limine-tool-windows-x86" \
         "$LIMINE_BIN_DIR/limine-tool" \
         limine; do
  if [ -n "${C##*/*}" ] && command -v "$C" >/dev/null 2>&1; then TOOL="$C"; break; fi
  if [ -f "$C" ] && [ -x "$C" ]; then TOOL="$C"; break; fi
done

if [ -n "$TOOL" ]; then
  "$TOOL" bios-install "$OUT"
  echo "build-disk: BIOS stages installed with $TOOL"
else
  echo "build-disk: WARNING: limine tool not found; image is UEFI-only" >&2
  echo "build-disk:          (install limine and run: limine bios-install $OUT)" >&2
fi

echo "build-disk: wrote $OUT ($(wc -c < "$OUT") bytes)"
