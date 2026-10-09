#!/usr/bin/env bash
# K4b ELF64 loader oracle: the host-side, outside-the-binary check for the
# supervisor loader (work order K4, "ELF64 loader"; check stage 13).
#
# Layers, all independent of the kernel's code:
#
#   A. Fixture determinism: the committed fixtures (kernel/fixtures/) must
#      be byte-identical to what the committed generator
#      (tools/gen-elf64-fixture.py) produces into a fresh directory. A
#      hand-edited or drifted fixture fails here before anything else.
#
#   B. Spec oracle: tools/verify-elf64.py (written from the gABI sections
#      cited in its header) must ACCEPT the positive fixture and REJECT
#      every negative fixture. The oracle's live staged digest is diffed
#      against the gate's committed STAGED_SHA256 constant (parsed out of
#      elfselftest.rs source): if the fixture, the oracle's staging rule or
#      the gate's expectation drifts, the diff fails BEFORE the guest can
#      agree with a stale value.
#
#   C. Guest gate: the selftest image is booted (rc 33 = clean, 35 = a
#      gate failed) and its ELF_KERNEL_PROBE lines are re-derived, not
#      trusted: the guest's staged-region digest (computed by the kernel's
#      own FIPS 180-4 sha256) must equal the host oracle's hashlib digest
#      from layer B, seg1's page base must sit exactly SEGMENT_SLOT (2 MiB)
#      above seg0's, and the documented deltas/entry must appear verbatim.
#
# Output: per-layer verdicts; exit 0 = all pass, 1 = failure, 2 = harness
# could not run (missing image / qemu / python - a loud SKIP in check.sh).
set -uo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
BUILD="$REPO_ROOT/build"
IMAGE="$BUILD/nova-selftest.hdd"
LOG="$BUILD/qemu-serial-elf.log"

PY=()
if command -v py >/dev/null 2>&1; then PY=(py -3)
elif command -v python3 >/dev/null 2>&1; then PY=(python3)
elif [ -f "/c/Windows/py.exe" ]; then PY=(/c/Windows/py.exe -3)
fi
[ ${#PY[@]} -gt 0 ] || { echo "SKIP: no usable python launcher found"; exit 2; }

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi
[ -n "$QEMU_BIN" ] && { command -v "$QEMU_BIN" >/dev/null 2>&1 || [ -f "$QEMU_BIN" ]; } || { echo "SKIP: qemu-system-x86_64 not installed - elf oracle not run"; exit 2; }
[ -f "$IMAGE" ] || { echo "SKIP: $IMAGE missing - run: NOVA_TEST=1 bash scripts/build-disk.sh release"; exit 2; }

to_win() {
  command -v cygpath >/dev/null 2>&1 && cygpath -w "$1" 2>/dev/null || echo "$1"
}

FAILED=0

echo "--- elf oracle A: committed fixtures are byte-identical to the generator"
TMPFIX="$BUILD/elf64-fixture-check"
rm -rf "$TMPFIX"
mkdir -p "$TMPFIX"
"${PY[@]}" "$REPO_ROOT/tools/gen-elf64-fixture.py" "$TMPFIX" >/dev/null || FAILED=1
"${PY[@]}" - "$REPO_ROOT/kernel/fixtures" "$TMPFIX" <<'PYEOF' || FAILED=1
import hashlib
import sys
from pathlib import Path

committed, fresh = Path(sys.argv[1]), Path(sys.argv[2])

def digest_map(root):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            # as_posix: Windows Path separators would break the negative/
            # prefix test below.
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out

a, b = digest_map(committed), digest_map(fresh)
if "elf64-mini" not in a:
    print("  FAIL  committed fixture elf64-mini missing")
    sys.exit(1)
neg_a = {k: v for k, v in a.items() if k.startswith("negative/")}
if len(neg_a) != 10:
    print(f"  FAIL  expected 10 negative fixtures, found {len(neg_a)}")
    sys.exit(1)
if a == b:
    print(f"  ok    {len(a)} fixture files byte-identical to generator output")
    sys.exit(0)
for k in sorted(set(a) | set(b)):
    if a.get(k) != b.get(k):
        print(f"  FAIL  fixture drift: {k}")
sys.exit(1)
PYEOF

echo "--- elf oracle B: spec oracle accepts the positive, rejects every negative"
# The POSITIVE run's output is kept in its own file: layers B2 and C diff
# against the live oracle digest, which must come from the positive image,
# not from the last negative this loop writes.
ORACLE_POS="$BUILD/elf64-oracle-positive.out"
ORACLE_OUT="$BUILD/elf64-oracle.out"
"${PY[@]}" "$REPO_ROOT/tools/verify-elf64.py" "$REPO_ROOT/kernel/fixtures/elf64-mini" > "$ORACLE_POS" 2>&1
if [ $? -ne 0 ]; then
  echo "  FAIL  oracle rejected the valid fixture:"
  sed 's/^/        /' "$ORACLE_POS"
  FAILED=1
else
  grep -q "VERDICT valid" "$ORACLE_POS" \
    && echo "  ok    oracle: positive fixture accepted (VERDICT valid)" \
    || { echo "  FAIL  oracle output missing 'VERDICT valid'"; FAILED=1; }
fi

NMATCH=0
for f in "$REPO_ROOT"/kernel/fixtures/negative/*.elf; do
  name="$(basename "$f" .elf)"
  "${PY[@]}" "$REPO_ROOT/tools/verify-elf64.py" "$f" > "$ORACLE_OUT" 2>&1
  rc=$?
  if [ $rc -eq 1 ] && grep -q "VERDICT invalid" "$ORACLE_OUT"; then
    NMATCH=$((NMATCH + 1))
  else
    echo "  FAIL  negative $name not rejected as invalid (rc=$rc)"
    sed 's/^/        /' "$ORACLE_OUT"
    FAILED=1
  fi
done
if [ "$NMATCH" -eq 10 ]; then
  echo "  ok    oracle: all 10 negative fixtures rejected (VERDICT invalid)"
fi

echo "--- elf oracle B2: live oracle digest equals the gate's committed constant"
"${PY[@]}" - "$ORACLE_POS" "$REPO_ROOT/kernel/src/elfselftest.rs" <<'PYEOF' || FAILED=1
import re
import sys
from pathlib import Path

oracle_out = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
gate_src = Path(sys.argv[2]).read_text(encoding="utf-8")

m_live = re.search(r"ELFORACLE staged_sha256 ([0-9a-f]{64})", oracle_out)
if m_live is None:
    print("  FAIL  no staged_sha256 in oracle output (run the oracle on the positive first)")
    sys.exit(1)
m_gate = re.search(r'const STAGED_SHA256: &str = "([0-9a-f]{64})"', gate_src)
if m_gate is None:
    print("  FAIL  STAGED_SHA256 constant not found in elfselftest.rs")
    sys.exit(1)
if m_live.group(1) == m_gate.group(1):
    print(f"  ok    digest cross-check {m_live.group(1)[:16]}... equals the gate constant")
    sys.exit(0)
print(f"  FAIL  oracle digest {m_live.group(1)} != gate constant {m_gate.group(1)}")
sys.exit(1)
PYEOF

echo "--- elf oracle C: boot the selftest image and re-derive the probe lines"
rm -f "$LOG"
timeout 60 "$QEMU_BIN" -M q35 -m 512M \
  -rtc base=utc \
  -drive "file=$(to_win "$IMAGE"),format=raw" \
  -serial "file:$(to_win "$LOG")" -display none -no-reboot \
  -device isa-debug-exit,iobase=0x501,iosize=0x02
QEMU_RC=$?
if [ "$QEMU_RC" -ne 33 ]; then
  echo "  FAIL  selftest boot did not exit cleanly (rc=$QEMU_RC; 35 = a gate failed)"
  tail -5 "$LOG" 2>/dev/null | sed 's/^/      /'
  exit 1
fi

"${PY[@]}" - "$LOG" <<'PYEOF' || FAILED=1
import re
import sys

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()

m = re.search(r"elf gate: (\d+) passed, (\d+) failed", log)
if m is None:
    print("  FAIL  no 'elf gate: N passed, N failed' line in the serial log")
    sys.exit(1)
passed, failed = int(m.group(1)), int(m.group(2))
if failed != 0 or passed == 0:
    print(f"  FAIL  elf gate reported {passed} passed, {failed} failed")
    sys.exit(1)
print(f"  ok    elf gate: {passed} passed, {failed} failed (as printed by the gate)")

layout = re.search(
    r"ELF_KERNEL_PROBE layout seg0_base (0x[0-9a-f]+) seg0_delta (0x[0-9a-f]+) "
    r"seg1_base (0x[0-9a-f]+) seg1_delta (0x[0-9a-f]+) entry (0x[0-9a-f]+)", log)
verdict = re.search(r"ELF_KERNEL_PROBE verdict staged_sha256 ([0-9a-f]{64})", log)
if layout is None or verdict is None:
    print("  FAIL  ELF_KERNEL_PROBE layout/verdict lines missing from the serial log")
    sys.exit(1)
seg0_base, seg0_delta, seg1_base, seg1_delta, entry = (int(g, 16) for g in layout.groups())

SLOT = 2 << 20  # elf.rs SEGMENT_SLOT (documented staging rule)
ok = True
if seg1_base - seg0_base != SLOT:
    print(f"  FAIL  seg1_base - seg0_base = {seg1_base - seg0_base:#x}, want {SLOT:#x} (SEGMENT_SLOT)")
    ok = False
if seg0_delta != 0x100 or seg1_delta != 0x108:
    print(f"  FAIL  deltas {seg0_delta:#x}/{seg1_delta:#x}, want 0x100/0x108 (documented layout)")
    ok = False
if entry != 0x400100:
    print(f"  FAIL  entry {entry:#x}, want 0x400100 (documented layout)")
    ok = False
if ok:
    print(f"  ok    layout probe: bases {seg0_base:#x}/{seg1_base:#x} differ by SEGMENT_SLOT, "
          f"deltas 0x100/0x108, entry 0x400100")
if not ok:
    sys.exit(1)
sys.exit(0)
PYEOF

# The guest's digest must equal the HOST ORACLE's live digest (layer B),
# not just the gate's constant - this is the kernel-vs-oracle agreement.
"${PY[@]}" - "$LOG" "$ORACLE_POS" <<'PYEOF' || FAILED=1
import re
import sys

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
oracle_out = open(sys.argv[2], encoding="utf-8", errors="replace").read()
m_guest = re.search(r"ELF_KERNEL_PROBE verdict staged_sha256 ([0-9a-f]{64})", log)
m_oracle = re.search(r"ELFORACLE staged_sha256 ([0-9a-f]{64})", oracle_out)
if m_guest is None or m_oracle is None:
    print("  FAIL  guest or oracle digest line missing")
    sys.exit(1)
if m_guest.group(1) == m_oracle.group(1):
    print(f"  ok    guest digest equals host oracle digest ({m_guest.group(1)[:16]}...)")
    sys.exit(0)
print(f"  FAIL  guest staged digest {m_guest.group(1)} != oracle {m_oracle.group(1)}")
sys.exit(1)
PYEOF

rm -rf "$TMPFIX"
if [ "$FAILED" -eq 0 ]; then
  echo "elf oracle: PASS"
  exit 0
fi
echo "elf oracle: FAIL"
exit 1
