#!/usr/bin/env bash
# RTC bracket oracle: run the NOVA selftest image once and require the
# guest's `RTC_READ UTC <epoch>` line to sit between two host-UTC samples
# taken immediately before and after the run.
#
# Why this is an independent oracle: QEMU seeds the guest MC146818 RTC from
# the host clock, and the host clock comes from the operating system, not
# from any NOVA code. A kernel that decodes the RTC wrong — wrong century
# register, BCD read as binary, 12h hour, month off by one — produces an
# epoch seconds outside the bracket, which is a FAIL regardless of what the
# kernel's own gates claim. Tolerance is ±120 s to cover boot time and
# second-rounding in both directions. The guest clock base is pinned
# explicitly to UTC (-rtc base=utc): some hosts (Windows) default the RTC
# base to local time, which is not what the kernel's UTC interpretation
# expects, and the pin keeps the oracle host-independent.
#
# Output: prints the bracket and verdict; exit 0 = in bracket, 1 = out,
# 2 = harness could not run (missing image / qemu / python / unparsable).
set -uo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
BUILD="$REPO_ROOT/build"
IMAGE="$BUILD/nova-selftest.hdd"
LOG="$BUILD/qemu-serial-rtc.log"
TOLERANCE=120

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi
if [ -z "$QEMU_BIN" ] || { ! command -v "$QEMU_BIN" >/dev/null 2>&1 && [ ! -f "$QEMU_BIN" ]; }; then
  echo "SKIP: qemu-system-x86_64 not installed - RTC bracket oracle not run"
  exit 2
fi
if [ ! -f "$IMAGE" ]; then
  echo "SKIP: $IMAGE missing - run: NOVA_TEST=1 bash scripts/build-disk.sh release"
  exit 2
fi

to_win() {
  command -v cygpath >/dev/null 2>&1 && cygpath -w "$1" 2>/dev/null || echo "$1"
}

# Host UTC anchor (epoch seconds) taken IMMEDIATELY BEFORE the QEMU run.
# QEMU's -rtc base=utc seeds the guest clock from host UTC at guest power-on;
# the after-anchor below covers the seconds that elapse while it boots.
HOST_BEFORE="$(py -3 -c 'import time; print(int(time.time()))' 2>/dev/null)" \
  || HOST_BEFORE="$(python3 -c 'import time; print(int(time.time()))' 2>/dev/null)" \
  || HOST_BEFORE="$(python -c 'import time; print(int(time.time()))' 2>/dev/null)"
if [ -z "${HOST_BEFORE:-}" ]; then
  echo "SKIP: no host python for the UTC anchor"
  exit 2
fi

# The selftest image exits by itself (rc 33) after all gates pass; a gate
# failure exits 35 and the serial log then says which check failed.
timeout 60 "$QEMU_BIN" -M q35 -m 512M \
  -rtc base=utc \
  -drive "file=$(to_win "$IMAGE"),format=raw" \
  -serial "file:$(to_win "$LOG")" -display none -no-reboot \
  -device isa-debug-exit,iobase=0x501,iosize=0x02
QEMU_RC=$?

HOST_AFTER="$(py -3 -c 'import time; print(int(time.time()))' 2>/dev/null || echo "")"
[ -n "${HOST_AFTER:-}" ] || HOST_AFTER="$HOST_BEFORE"

if [ "$QEMU_RC" -ne 33 ]; then
  echo "FAIL: selftest boot did not exit cleanly (rc=$QEMU_RC; 35 = a gate failed)"
  tail -5 "$LOG" 2>/dev/null | sed 's/^/    /'
  exit 1
fi

HOST_AFTER="$(py -3 -c 'import time; print(int(time.time()))' 2>/dev/null || python3 -c 'import time; print(int(time.time()))' 2>/dev/null || python -c 'import time; print(int(time.time()))' 2>/dev/null || echo "")"
[ -n "${HOST_AFTER:-}" ] || HOST_AFTER="$HOST_BEFORE"

# Pick the python launcher BEFORE opening the heredoc. An if/elif cascade
# around a single heredoc body is broken by construction: bash attaches the
# body to the first `<<'PYEOF'`, so the `elif`/`else` lines are fed to python
# as code. Reproduced live 2026-10-05: rc=1, "SyntaxError: invalid syntax"
# from `<stdin>` line 1. (CI stage 10 failed in runs 37253989609 and
# 37257749947 with no retained stage output; the traceback did not survive
# in the logs this host can fetch.) Same probe-then-invoke pattern as
# QEMU_BIN above and PYCMD in scripts/check.sh.
PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
fi
if [ ${#PYCMD[@]} -eq 0 ]; then
  echo "SKIP: no python launcher - RTC bracket oracle not run"
  exit 2
fi
"${PYCMD[@]}" - "$LOG" "$HOST_BEFORE" "$HOST_AFTER" "$TOLERANCE" <<'PYEOF'
import re
import sys

log, before, after, tol = (sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
guest = None
stamp = None
for line in open(log, encoding="utf-8", errors="replace"):
    line = line.rstrip("\r\n")
    if line.startswith("RTC_READ UTC "):
        try:
            guest = int(line.split()[2])
        except (IndexError, ValueError):
            pass
    elif re.match(r"RTC_READ \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} ", line):
        # `"T" in line` matched every RTC_READ line ("RTC_READ" has a T), so
        # the later `RTC_READ render_len=19` line overwrote the stamp.
        stamp = line.split(" ", 2)[1]

if guest is None:
    print("FAIL: no parsable 'RTC_READ UTC <epoch>' line in the serial log")
    sys.exit(1)

lo, hi = before - tol, after + tol
print(f"host UTC before : {before}")
print(f"host UTC after  : {after}   (accepted bracket: {lo} .. {hi})")
print(f"guest RTC epoch : {guest}   ({stamp or '?'})")
print(f"guest - before  : {guest - before:+d} s")
if lo <= guest <= hi:
    print("RTC bracket oracle: IN BRACKET - guest clock agrees with host UTC")
    sys.exit(0)
print("RTC bracket oracle: OUT OF BRACKET - decoded clock does not match host UTC")
sys.exit(1)
PYEOF
