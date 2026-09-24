#!/usr/bin/env bash
# Headless boot smoke test for build/nova.iso (cdrom) and/or build/nova.hdd
# (raw disk), under SeaBIOS and OVMF when available. Asserts NOVA_BOOT_OK on
# serial and a clean isa-debug-exit (code 33). Exits 0 with SKIP if no QEMU.
set -uo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
BUILD="$REPO_ROOT/build"
LOG="$BUILD/qemu-serial.log"
QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi

command -v "$QEMU_BIN" >/dev/null 2>&1 || [ -f "$QEMU_BIN" ] || {
  echo "SKIP: qemu-system-x86_64 not installed — boot tests skipped (install qemu-system-x86)" >&2
  exit 0
}

OVMF=""
for C in /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd /usr/share/qemu/OVMF_CODE.fd; do
  [ -f "$C" ] && OVMF="$C" && break
done
# If the expected firmware file is absent, show what the runner actually has
# so the gap is diagnosable from the log instead of a silent skip.
if [ -z "$OVMF" ] && [ -d /usr/share/OVMF ]; then
  echo "--- /usr/share/OVMF contents:"
  ls -la /usr/share/OVMF || true
fi

# UEFI always runs via pflash: -bios mishandles Windows paths and is the
# wrong interface for the OVMF_CODE.fd code/vars split on Linux anyway.
UEFI_ARGS=()
if [ -n "$OVMF" ]; then
  # Linux paths, passed as-is: this branch only runs where /usr/share/OVMF
  # exists (the Windows fallback below handles cygpath conversion itself).
  UEFI_ARGS=(-drive if=pflash,format=raw,readonly=on,file="$OVMF")
  case "$OVMF" in
    */OVMF_CODE.fd)
      if [ -f /usr/share/OVMF/OVMF_VARS.fd ]; then
        cp /usr/share/OVMF/OVMF_VARS.fd "$BUILD/OVMF_VARS.fd"
        UEFI_ARGS+=(-drive if=pflash,format=raw,file="$BUILD/OVMF_VARS.fd")
      fi
      ;;
  esac
else
  WIN_FW=""
  for C in "/c/Program Files/qemu/share/edk2-x86_64-code.fd" \
           "$LOCALAPPDATA/Programs/qemu/share/edk2-x86_64-code.fd"; do
    [ -f "$C" ] && WIN_FW="$C" && break
  done
  if [ -n "$WIN_FW" ]; then
    [ -f "$BUILD/edk2-code.fd" ] || cp "$WIN_FW" "$BUILD/edk2-code.fd"
    W="$(cygpath -w "$BUILD/edk2-code.fd" 2>/dev/null || echo "$BUILD/edk2-code.fd")"
    UEFI_ARGS=(-drive if=pflash,format=raw,readonly=on,file="$W")
  fi
fi

# Case matrix: image:media:expect_rc:grep_pattern
#  - interactive images boot, print NOVA_BOOT_OK, then sit at the shell — the
#    timeout kill (rc 124) is the pass condition there.
#  - the selftest image has `novatest` on its command line and exits cleanly
#    (rc 33) after NOVA_SELFTEST_OK.
CASES=()
[ -f "$BUILD/nova.iso" ] && CASES+=("iso:$BUILD/nova.iso:media=cdrom:124:NOVA_BOOT_OK")
[ -f "$BUILD/nova.hdd" ] && CASES+=("hdd:$BUILD/nova.hdd:format=raw:124:NOVA_BOOT_OK")
[ -f "$BUILD/nova-selftest.hdd" ] && CASES+=("selftest:$BUILD/nova-selftest.hdd:format=raw:33:NOVA_SELFTEST_OK")
if [ ${#CASES[@]} -eq 0 ]; then
  echo "SKIP: no boot images in build/ (run: make disk)" >&2
  exit 0
fi

FAILED=0

# Windows QEMU can't read MSYS-style /c/... paths; convert when possible.
to_win() {
  command -v cygpath >/dev/null 2>&1 && cygpath -w "$1" 2>/dev/null || echo "$1"
}

run_case() {
  local name="$1" image="$2" media="$3" expect_rc="$4" pattern="$5"; shift 5
  # Per-case QEMU stderr capture: without it an empty-serial failure is
  # undiagnosable (the CI-1 lesson — 4 CI failures had zero evidence).
  local ERRLOG="$BUILD/qemu-stderr-${name//\//-}.log"
  rm -f "$LOG" "$ERRLOG"
  local img_arg="$(to_win "$image")"
  timeout 60 "$QEMU_BIN" -M q35 -m 512M "$@" \
    -drive "file=$img_arg,$media" \
    -serial "file:$(to_win "$LOG")" -display none -no-reboot \
    -device isa-debug-exit,iobase=0x501,iosize=0x02 >"$ERRLOG" 2>&1
  local rc=$?
  echo "--- $name: exit=$rc serial tail:"
  tail -6 "$LOG" 2>/dev/null | sed 's/^/    /'
  if grep -q "$pattern" "$LOG" && [ "$rc" -eq "$expect_rc" ]; then
    echo "PASS ($name)"
  else
    echo "FAIL ($name) — QEMU stderr ($ERRLOG):"
    sed 's/^/    | /' "$ERRLOG" 2>/dev/null
    FAILED=1
  fi
}

for ENTRY in "${CASES[@]}"; do
  IFS=':' read -r KIND IMG MEDIA EXPECT_RC PATTERN <<< "$ENTRY"
  # Windows absolute paths contain colons; rejoin what IFS split.
  case "$KIND" in
    iso|hdd|selftest) ;;
    *) continue ;;
  esac
  IMG="${ENTRY#*:}"; IMG="${IMG%:$MEDIA:$EXPECT_RC:$PATTERN}"
  run_case "SeaBIOS/$KIND" "$IMG" "$MEDIA" "$EXPECT_RC" "$PATTERN"
  if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
    run_case "OVMF/$KIND" "$IMG" "$MEDIA" "$EXPECT_RC" "$PATTERN"
  else
    echo "--- OVMF/EDK2 firmware not found; UEFI case skipped"
  fi
done

exit $FAILED
