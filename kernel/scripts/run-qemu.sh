#!/usr/bin/env bash
# Boot nova.iso in QEMU. Serial lands in build/qemu-serial.log.
#   QEMU_UEFI=1   use OVMF firmware (default SeaBIOS)
#   QEMU_EXTRA    extra args passed through
set -euo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
ISO="$REPO_ROOT/build/nova.iso"
LOG="$REPO_ROOT/build/qemu-serial.log"

if [ "${QEMU_DISK:-0}" = "1" ]; then
  IMG="$REPO_ROOT/build/nova.hdd"
  [ -f "$IMG" ] || { echo "run-qemu: $IMG missing (run: make disk)" >&2; exit 1; }
  DRIVE_ARGS=(-drive "file=$(to_win "$IMG"),format=raw")
else
  [ -f "$ISO" ] || { echo "run-qemu: $ISO missing (run: make iso)" >&2; exit 1; }
  DRIVE_ARGS=(-drive "file=$(to_win "$ISO"),media=cdrom")
fi
mkdir -p "$REPO_ROOT/build"; : > "$LOG"

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe"
  else QEMU_BIN=qemu-system-x86_64
  fi
fi

# Windows QEMU can't read MSYS-style /c/... paths; convert when possible.
to_win() {
  command -v cygpath >/dev/null 2>&1 && cygpath -w "$1" 2>/dev/null || echo "$1"
}
FW_ARGS=()
if [ "${QEMU_UEFI:-0}" = "1" ]; then
  for C in /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd /usr/share/qemu/OVMF_CODE.fd; do
    [ -f "$C" ] && FW_ARGS=(-bios "$C") && break
  done
  if [ ${#FW_ARGS[@]} -eq 0 ]; then
    echo "run-qemu: OVMF firmware not found; using default firmware" >&2
  fi
fi

exec "$QEMU_BIN" \
  -M q35 -m 512M \
  "${FW_ARGS[@]}" \
  "${DRIVE_ARGS[@]}" \
  -serial "file:$(to_win "$LOG")" \
  -display gtk \
  -device isa-debug-exit,iobase=0x501,iosize=0x02 \
  ${QEMU_EXTRA:-}
