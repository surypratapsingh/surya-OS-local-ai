#!/usr/bin/env bash
# K4 capability oracle: the host-side, outside-the-binary check for the
# kernel capability table (work order K4, check stage 11).
#
# Three independent layers, all derived from sources the compiled kernel
# cannot influence:
#
#   A. Grammar: the vocabulary the kernel claims (parsed out of cap.rs's
#      CAP_REFS table) must be DECLARABLE in a real signed manifest. The
#      subject package is signed with the REAL C2 signing path and verified
#      with the REAL C2 verifier (tools/verify-manifest.py) against a
#      fixed-seed throwaway test key (never the owner's root key), plus
#      fail-closed negatives for wildcard and out-of-grammar rows.
#
#   B. Kernel probes: the guest selftest prints one CAP_KERNEL_PROBE line
#      per (subject, resource) attempt. The oracle recomputes the expected
#      value of EVERY line from its own parse of cap.rs plus the spec
#      sentence ("a process gets what its manifest declares and nothing
#      else") and diff-checks each one - it does not trust the gate's
#      ok/FAIL grades, and it shares no compiled constant with the binary.
#
#   C. Denial evidence: the done-when line must be present: an fs:read-only
#      subject attempting the camera is denied (probe value 0).
#
# Output: per-layer verdicts; exit 0 = all pass, 1 = failure, 2 = harness
# could not run (missing image / qemu / python - a loud SKIP in check.sh).
set -uo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
BUILD="$REPO_ROOT/build"
ORACLE_DIR="$BUILD/cap-oracle"
IMAGE="$BUILD/nova-selftest.hdd"
LOG="$BUILD/qemu-serial-rtc.log"
STATE="$ORACLE_DIR/state.json"

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi

PY=()
if command -v py >/dev/null 2>&1; then PY=(py -3)
elif command -v python3 >/dev/null 2>&1; then PY=(python3)
elif [ -f "/c/Windows/py.exe" ]; then PY=(/c/Windows/py.exe -3)
fi
[ ${#PY[@]} -gt 0 ] || { echo "SKIP: no usable python launcher found"; exit 2; }

[ -f "$QEMU_BIN" ] || { echo "SKIP: qemu-system-x86_64 not installed - cap oracle not run"; exit 2; }
[ -f "$IMAGE" ] || { echo "SKIP: $IMAGE missing - run: NOVA_TEST=1 bash scripts/build-disk.sh release"; exit 2; }

FAILED=0

echo "--- cap oracle A: the vocabulary is declarable in a signed C2 manifest"
"${PY[@]}" "$REPO_ROOT/tools/gen-cap-manifest.py" --out "$ORACLE_DIR" || FAILED=1
printf '{"release_sequence": 0}' > "$STATE"
"${PY[@]}" "$REPO_ROOT/tools/verify-manifest.py" "$ORACLE_DIR/manifest.json" \
  --public-key "$ORACLE_DIR/root.pub" --files "$ORACLE_DIR" --state "$STATE" || FAILED=1

echo "--- cap oracle A-negative: wildcards and out-of-grammar rows are refused"
"${PY[@]}" - "$REPO_ROOT/tools" "$ORACLE_DIR" <<'PYEOF' || FAILED=1
import subprocess
import sys
from pathlib import Path

tools_dir, oracle_dir = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(tools_dir))
import nova_trust as nt

secret = nt.parse_private_key_pem((oracle_dir / "root.priv").read_text(encoding="ascii"))
public = nt.public_key(secret)
# No bootchain section here either, matching tools/gen-cap-manifest.py:
# docs/manifest-format.md:116 calls it optional, and these are daily-driver
# software manifests, not boot chains. The dummy bootchain that used to sit
# here existed only to satisfy a verifier bug removed in 344344a.
pkg = oracle_dir / "oracle-package.bin"

def build(caps):
    m = nt.build_manifest(
        "0.1.0", 1, "2026-09-28T00:00:00Z",
        [("cap-oracle", "0.1.0", pkg, caps)], public,
        description="cap-oracle negative")
    return nt.sign_manifest(m, secret)

def end_to_end_rc(signed):
    path = oracle_dir / "neg.json"
    state = oracle_dir / "state.json"
    state.write_text('{"release_sequence": 0}', encoding="utf-8")
    nt.write_text_atomic(path, nt.dump_manifest(signed))
    r = subprocess.run(
        [sys.executable, str(tools_dir / "verify-manifest.py"), str(path),
         "--public-key", str(oracle_dir / "root.pub"), "--files", str(oracle_dir),
         "--state", str(state)],
        capture_output=True, text=True)
    return r.returncode

# Rows that break the C2 grammar must be refused by the verifier's own
# structural rules - a wildcard can never reach a signature.
for row in ["device:camera:*", "device:*", "*:*", "*", "fs:read:extra",
            "Camera", "device:Camera", "not-a-capability", ""]:
    m = nt.build_manifest(
        "0.1.0", 1, "2026-09-28T00:00:00Z",
        [("cap-oracle", "0.1.0", pkg, [row])], public,
        description="cap-oracle negative")
    errs = nt.structure_errors(m, require_signature=False)
    if errs:
        print(f"  ok    refused unsigned: {row!r}")
    else:
        print(f"  FAIL  {row!r}: accepted by the C2 structural verifier")
        sys.exit(1)

# A grammar-VALID but unknown capability ("fs:readx") is accepted by the C2
# verifier end-to-end - by design: C2 checks grammar and signature, not
# vocabulary. This is exactly why the vocabulary check lives in the kernel
# (cap gate: "refused: boot:cpu"). The tampered-signature control proves the
# same pipeline really does reject a bad signature.
probe_signed = build(["fs:readx"])
if end_to_end_rc(probe_signed) == 0:
    print("  ok    grammar-valid unknown row verifies end-to-end "
          "(vocabulary enforcement lives in the kernel)")
else:
    print("  FAIL  grammar-valid unknown row should verify (C2 has no vocabulary check)")
    sys.exit(1)
import base64
sig = probe_signed["signature"]
tampered = dict(probe_signed)
tampered["signature"] = sig[:-4] + "AAAA"
if end_to_end_rc(tampered) == 1:
    print("  ok    tampered signature rejected end-to-end (rc=1)")
else:
    print("  FAIL  tampered signature was not rejected")
    sys.exit(1)
PYEOF

echo "--- cap oracle B+C: boot the selftest image and recompute every probe"
rm -f "$LOG"
timeout 60 "$QEMU_BIN" -M q35 -m 512M \
  -drive "file=$(cygpath -w "$IMAGE" 2>/dev/null || echo "$IMAGE"),format=raw" \
  -serial "file:$(cygpath -w "$LOG" 2>/dev/null || echo "$LOG")" \
  -display none -no-reboot \
  -device isa-debug-exit,iobase=0x501,iosize=0x02
QEMU_RC=$?
if [ "$QEMU_RC" -ne 33 ]; then
  echo "  FAIL  selftest boot did not exit cleanly (rc=$QEMU_RC; 35 = a gate failed)"
  tail -5 "$LOG" 2>/dev/null | sed 's/^/      /'
  exit 1
fi

"${PY[@]}" - "$LOG" "$REPO_ROOT" <<'PYEOF' || FAILED=1
import re
import sys
from pathlib import Path

log_path, repo = sys.argv[1], Path(sys.argv[2])
cap_rs = (repo / "kernel" / "src" / "cap.rs").read_text(encoding="utf-8")
m = re.search(r"CAP_REFS[^=]*=\s*&\[(.*?)\];", cap_rs, re.DOTALL)
if m is None:
    print("  FAIL  CAP_REFS table not found in cap.rs")
    sys.exit(1)
raw = m.group(1)
if not raw.rstrip().endswith("RESERVED_NAME,"):
    print("  FAIL  CAP_REFS no longer ends with RESERVED_NAME")
    sys.exit(1)
vocab = re.findall(r'"([^"]+)"', raw)
grantable = [v for v in vocab if v != "reserved"]
RESERVED = "reserved"

if len(grantable) != 7:
    print(f"  FAIL  unexpected vocabulary: {grantable}")
    sys.exit(1)
for name in grantable:
    if not re.fullmatch(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*", name):
        print(f"  FAIL  {name!r} violates the C2 manifest grammar "
              "(namespace:permission, lowercase)")
        sys.exit(1)

# The oracle's own model of the policy, from the spec sentence only: a
# subject is granted exactly the resources its manifest rows name - no
# more, no fewer - and the reserved resource is never grantable.
def expected(subject_rows, probe):
    if probe == RESERVED:
        return 0
    return 1 if probe in subject_rows else 0

SUBJECTS = {
    "fs-read": ["fs:read"],
    "empty": [],
    "full": grantable,
}

probes = 0
bad = 0
denial_seen = False
for line in open(log_path, encoding="utf-8", errors="replace"):
    line = line.rstrip("\r\n")
    parts = line.split(" ")
    if len(parts) != 4 or parts[0] != "CAP_KERNEL_PROBE":
        continue
    tag, name, val = parts[1], parts[2], parts[3]
    if tag.startswith("row:"):
        rows = [tag[4:]]
    elif tag in SUBJECTS:
        rows = SUBJECTS[tag]
    else:
        print(f"  FAIL  unknown probe subject tag {tag!r}")
        sys.exit(1)
    probes += 1
    if val != str(expected(rows, name)):
        bad += 1
        print(f"  FAIL  {tag} {name}: kernel says {val}, oracle says "
              f"{expected(rows, name)}")
    if tag == "fs-read" and name == "device:camera" and val == "0":
        denial_seen = True

if probes == 0:
    print("  FAIL  no CAP_KERNEL_PROBE lines in the serial log")
    sys.exit(1)
if bad:
    print(f"  FAIL  {bad} of {probes} probe lines disagree with the oracle model")
    sys.exit(1)
print(f"  ok    {probes} probe lines recomputed by the host oracle, all agree")
if denial_seen:
    print("  ok    done-when evidence: fs:read-only subject denied device:camera")
else:
    print("  FAIL  done-when evidence line missing or wrong (fs-read device:camera 0)")
    sys.exit(1)
PYEOF

if [ $FAILED -eq 0 ]; then
  echo "cap oracle: PASS (all layers)"
else
  echo "cap oracle: FAIL"
fi
exit $FAILED
