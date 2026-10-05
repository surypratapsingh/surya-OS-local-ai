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

# C4: the Limine config's kernel path carries a blake2b-512 digest that
# Limine verifies before loading the kernel. Limine's URI parser (vendored
# limine-12.9.0 common/lib/uri.c lines 65-90) takes everything after `#` as
# the hash and panics unless it is exactly 128 hex characters — NO algorithm
# prefix (the earlier `#blake2b:<sha256>` form panicked every regular boot;
# see docs/logs/k3c-c4-boot-regression-diagnosis.log). The digest is computed
# over the same file make-esp.py embeds at /NUCLEUS (same $KERNEL_ELF path,
# same run, no rebuild in between), and the substituted config is written
# per-image (build/limine.conf.<image>) so a selftest build can never leak
# its config into the next regular build; verify-disk.py / oracle-disk.py /
# fuzz-disk.py compare the ESP config against this exact file.

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

KERNEL_HASH=""
LIMINE_EFI="$LIMINE_BIN_DIR/BOOTX64.EFI"
if [ -f "$LIMINE_EFI" ]; then
  # blake2b-512 (128 hex chars) for the Limine path; sha256 for the release
  # manifest hints printed below (the manifest schema is sha256-based).
  DIGESTS="$("${PYCMD[@]}" - "$KERNEL_ELF" <<'PYEOF'
import hashlib, sys
data = open(sys.argv[1], "rb").read()
print(hashlib.blake2b(data, digest_size=64).hexdigest())
print(hashlib.sha256(data).hexdigest())
PYEOF
)"  KERNEL_B2B="$(printf '%s\n' "$DIGESTS" | sed -n '1p')"
  KERNEL_HASH="$(printf '%s\n' "$DIGESTS" | sed -n '2p')"
  # py.exe on Windows writes CRLF to pipes; strip it before use.
  DIGESTS="${DIGESTS//$'\r'/}"
  KERNEL_B2B="${KERNEL_B2B//$'\r'/}"
  KERNEL_HASH="${KERNEL_HASH//$'\r'/}"
  # Limine panics on anything but exactly 128 hex chars after `#` (uri.c
  # lines 78-88), so refuse to substitute anything else — a corrupted
  # digest here would otherwise surface only as a boot panic.
  if ! printf '%s' "$KERNEL_B2B" | grep -qE '^[0-9a-f]{128}$'; then
    echo "build-disk: ERROR: kernel blake2b-512 digest is not 128 lowercase hex chars: '$KERNEL_B2B'" >&2
    exit 1
  fi
  if [ -z "$KERNEL_B2B" ] || [ -z "$KERNEL_HASH" ]; then
    echo "build-disk: ERROR: could not hash $KERNEL_ELF (python hashlib unavailable?)" >&2
    exit 1
  fi
  # Every image gets a sidecar next to it (build/limine.conf.<image name>),
  # holding exactly the config bytes embedded on its ESP — this is the file
  # verify-disk.py / fuzz-disk.py / oracle-disk.py must compare against.
  CONF_OUT="$REPO_ROOT/build/limine.conf.$(basename "$OUT")"
  cp "$CONF" "$CONF_OUT"
  if grep -q "KERNEL_BLAKE2B_512_PLACEHOLDER" "$CONF_OUT"; then
    if sed -i "s/KERNEL_BLAKE2B_512_PLACEHOLDER/$KERNEL_B2B/" "$CONF_OUT" \
       && ! grep -q "KERNEL_BLAKE2B_512_PLACEHOLDER" "$CONF_OUT" \
       && grep -q "$KERNEL_B2B" "$CONF_OUT"; then
      CONF="$CONF_OUT"
      echo "build-disk: bootchain kernel blake2b-512: $KERNEL_B2B"
      echo "build-disk: kernel path hash embedded via $CONF_OUT"
    else
      rm -f "$CONF_OUT"
      echo "build-disk: ERROR: failed to substitute KERNEL_BLAKE2B_512_PLACEHOLDER in $CONF" >&2
      echo "build-disk:          (sed missing or substitution incomplete); refusing to" >&2
      echo "build-disk:          embed a config whose kernel hash Limine would reject" >&2
      exit 1
    fi
  else
    CONF="$CONF_OUT"
    echo "build-disk: note: $CONF has no KERNEL_BLAKE2B_512_PLACEHOLDER;"
    echo "build-disk:       embedding as-is (sidecar still written for the verifiers)"
  fi
fi

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

PORT="$REPO_ROOT/tools/bios_install.py"

# tools/bios_install.py ports the pinned upstream tool's bios-install (GPT
# path) byte-for-byte: same limine.c write list, same install image
# extracted from the pinned limine-bios-hdd.h. Parity is enforced by
# tests/test_bios_install.py against the vendored source as the oracle;
# anything the port cannot handle byte-identically is refused loudly and
# writes nothing. It exists for hosts where limine.exe cannot execute at
# all (Smart App Control exec denial, rc=126, docs/sac-build-blocks.md)
# and no C compiler is available.
run_port() {
  "$PYCMD" "$PORT" bios-install "$OUT"
  echo "build-disk: BIOS stages installed with tools/bios_install.py (pinned limine.c/limine-bios-hdd.h; see docs/sac-build-blocks.md)"
}

if [ -n "$TOOL" ]; then
  if "$TOOL" bios-install "$OUT"; then
    echo "build-disk: BIOS stages installed with $TOOL"
  else
    TOOL_RC=$?
    if [ "$TOOL_RC" -eq 126 ] && [ -f "$PORT" ]; then
      # rc=126 means the tool never ran (exec refused), so it rendered no
      # verdict on the image; the port may speak for it. Any other failure
      # is a real verdict from the real tool and is propagated unchanged.
      echo "build-disk: $TOOL could not be executed (rc=126; Smart App Control on" >&2
      echo "build-disk:   some Windows hosts - docs/sac-build-blocks.md); using the port" >&2
      run_port
    else
      echo "build-disk: $TOOL bios-install failed (rc=$TOOL_RC); not falling back" >&2
      exit "$TOOL_RC"
    fi
  fi
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
  if [ -f "$PORT" ]; then
    echo "build-disk: no limine tool and no C compiler; using the port" >&2
    run_port
  else
    echo "build-disk: WARNING: limine tool not found and no C compiler; image is UEFI-only" >&2
    echo "build-disk:          (install limine or cc/gcc/clang, then rerun: limine bios-install $OUT)" >&2
  fi
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
