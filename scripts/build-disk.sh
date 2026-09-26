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

# C4: Compute bootchain hashes early and update Limine config before make-esp.py
# This is done before make-esp.py so the config with hash verification is embedded in the image
LIMINE_EFI="$LIMINE_BIN_DIR/BOOTX64.EFI"
if [ -f "$LIMINE_EFI" ]; then
  # Compute hashes using both sha256sum (Linux) and shasum (macOS fallback)
  KERNEL_HASH=$(sha256sum "$KERNEL_ELF" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$KERNEL_ELF" 2>/dev/null | cut -d' ' -f1 || echo "")

  if [ -n "$KERNEL_HASH" ]; then
    # Create a config file with the kernel hash filled in
    CONF_OUT="$REPO_ROOT/build/limine.conf"
    cp "$CONF" "$CONF_OUT"
    # Replace PLACEHOLDER_KERNEL_HASH with the actual kernel hash
    if command -v sed >/dev/null 2>&1; then
      sed -i "s/PLACEHOLDER_KERNEL_HASH/$KERNEL_HASH/" "$CONF_OUT"
    fi
    CONF="$CONF_OUT"
    echo "build-disk: bootchain kernel hash: $KERNEL_HASH"
    echo "build-disk: updated $CONF with kernel hash verification"
  fi
fi

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
elif command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1; then
  # No host tool (Linux runners ship none) but a C compiler exists: build the
  # tool from the vendored single-file source, exactly as the vendored
  # Makefile does. limine.c and limine-bios-hdd.h are pinned in
  # tools/manifest/bootchain.sha256, so the compiled tool inherits the
  # boot-chain trust pins.
  CC_BIN=cc
  command -v cc >/dev/null 2>&1 || { CC_BIN=gcc; command -v gcc >/dev/null 2>&1 || CC_BIN=clang; }
  TOOL="$REPO_ROOT/build/limine-tool"
  mkdir -p "$(dirname "$TOOL")"
  (cd "$LIMINE_BIN_DIR" && "$CC_BIN" -O2 -pipe -std=c99 -D_FILE_OFFSET_BITS=64 limine.c -o "$TOOL")
  "$TOOL" bios-install "$OUT"
  echo "build-disk: BIOS stages installed with $TOOL (built from vendored limine.c with $CC_BIN)"
else
  echo "build-disk: WARNING: limine tool not found and no C compiler; image is UEFI-only" >&2
  echo "build-disk:          (install limine or cc/gcc/clang, then rerun: limine bios-install $OUT)" >&2
fi

echo "build-disk: wrote $OUT ($(wc -c < "$OUT") bytes)"

# C4: Also compute and display Limine hash for the manifest
LIMINE_SIZE=$(stat -c%s "$LIMINE_EFI" 2>/dev/null || stat -f%z "$LIMINE_EFI" 2>/dev/null || wc -c < "$LIMINE_EFI")
LIMINE_HASH=$(sha256sum "$LIMINE_EFI" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$LIMINE_EFI" 2>/dev/null | cut -d' ' -f1 || echo "")
KERNEL_SIZE=$(stat -c%s "$KERNEL_ELF" 2>/dev/null || stat -f%z "$KERNEL_ELF" 2>/dev/null || wc -c < "$KERNEL_ELF")

if [ -n "$LIMINE_HASH" ] && [ -n "$KERNEL_HASH" ]; then
  echo "build-disk: bootchain hashes (for manifest when signing release):"
  echo "  Limine:  $LIMINE_HASH ($LIMINE_SIZE bytes)"
  echo "  kernel:  $KERNEL_HASH ($KERNEL_SIZE bytes)"
  echo "build-disk: when ready to sign, pass these to create-manifest.py:"
  echo "  --limine-hash '$LIMINE_HASH' --limine-size $LIMINE_SIZE"
  echo "  --kernel-hash '$KERNEL_HASH' --kernel-size $KERNEL_SIZE"
fi
