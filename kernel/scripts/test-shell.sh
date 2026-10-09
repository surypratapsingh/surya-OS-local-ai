#!/usr/bin/env bash
# Interactive shell oracle: drive the K2 diagnostic shell from a script
# (work-order K2 scope "small diagnostic shell" + K3 scope "CMOS RTC",
# exposed by the `time` verb) and reproduce the manual sendkey procedure
# of docs/logs/k4a-time-verb.log in CI.
#
# The driver (tools/drive-shell.py) boots the REGULAR image (nova.hdd, not
# the selftest image -- it must exercise the input path, not an autotest),
# connects QEMU's serial and monitor chardevs to its own listeners, and
# types every verb found in shell.rs's dispatch() arms via the monitor's
# `sendkey` command.  No fixed sleeps: each keystroke is awaited by its
# own console echo, each command by the next `nova> ` prompt, and the VM
# starts only after both chardevs are connected, so no output can be
# missed by construction.
#
# Oracles, none of them the code under test:
#   - the verb list to type is extracted from shell.rs by grep, so a verb
#     added or renamed in the kernel must survive its own drive;
#   - every reply is checked inside the post-Enter response window
#     (echoes excluded): help must start `commands: ` and list the typed
#     verbs, ver must print the nucleus version, mem must count entries,
#     time must print `utc: YYYY-MM-DDTHH:MM:SS`;
#   - the `time` stamp is bracketed between two HOST-UTC samples taken
#     around the session (QEMU seeds the guest RTC from the host clock;
#     same rationale as test-rtc.sh, +/-120 s tolerance);
#   - a `zz` control must produce `unknown command: zz`, proving the
#     keystrokes really round-trip through the i8042 -> poller -> console
#     path.
#
# Output: driver transcript plus per-check verdicts; exit 0 = pass, 1 =
# failure, 2 = harness could not run (missing image / qemu / python --
# a loud SKIP in check.sh, never a pass).
set -uo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
BUILD="$REPO_ROOT/build"
IMAGE="${NOVA_SHELL_IMAGE:-$BUILD/nova.hdd}"
SLOG="$BUILD/shell-drive-serial.log"

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi
[ -n "$QEMU_BIN" ] || { echo "SKIP: qemu-system-x86_64 not installed - shell oracle not run"; exit 2; }
[ -f "$QEMU_BIN" ] || command -v "$QEMU_BIN" >/dev/null 2>&1 || { echo "SKIP: QEMU_BIN=$QEMU_BIN not found"; exit 2; }
if [ ! -f "$IMAGE" ]; then
  echo "SKIP: $IMAGE missing - run: bash scripts/build-disk.sh release"
  exit 2
fi

PY=()
if command -v py >/dev/null 2>&1; then PY=(py -3)
elif command -v python3 >/dev/null 2>&1; then PY=(python3)
elif [ -f "/c/Windows/py.exe" ]; then PY=(/c/Windows/py.exe -3)
fi
[ ${#PY[@]} -gt 0 ] || { echo "SKIP: no usable python launcher found"; exit 2; }

# The verb list comes from the kernel source's dispatch() arms -- the
# oracle's own extraction, not a hand-maintained list.  Verbs that would
# end the session (novatest exits CI-style, reboot/halt stop the CPU) are
# excluded from typing and the exclusion is printed, never silent.
ALL_ARMS="$(grep -oE '^        "[a-z]*" =>' "$KERNEL_DIR/src/shell.rs" \
  | grep -v '""' | sed 's/.*"\([a-z]*\)".*/\1/' | paste -sd, -)"
if [ -z "$ALL_ARMS" ]; then
  echo "FAIL: no dispatch arms found in src/shell.rs (grep anchor moved?)"
  exit 1
fi
VERBS=""
for v in ${ALL_ARMS//,/ }; do
  case "$v" in
    help) ;; # typed first, always
    novatest|reboot|halt)
      echo "note: verb '$v' not typed (would end the session)"
      ;;
    *) VERBS="${VERBS:+$VERBS,}$v"
      ;;
  esac
done
echo "dispatch arms in src/shell.rs: $ALL_ARMS"
echo "verbs to type: help $VERBS (plus control zz)"

"${PY[@]}" "$REPO_ROOT/tools/drive-shell.py" \
  --qemu "$QEMU_BIN" \
  --image "$IMAGE" \
  --expect-verbs "$VERBS" \
  --serial-log "$SLOG" \
  --bracket 120
RC=$?

if [ "$RC" -eq 2 ]; then
  echo "SKIP: shell oracle could not run (missing inputs) - not a pass"
  exit 2
fi
if [ "$RC" -ne 0 ]; then
  echo "FAIL: interactive shell oracle (see transcript above; serial log: $SLOG)"
  exit 1
fi
echo "interactive shell oracle: PASS (serial log: $SLOG)"
exit 0
