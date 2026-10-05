#!/usr/bin/env bash
# C3 atomic-install boot oracle. The work order's gate is "a failed or
# interrupted install always leaves a bootable machine", so every state the
# installer can be interrupted in is BOOTED, not merely parsed:
#
#   1. a real install of a release whose kernel is the built ELF plus a
#      marker suffix (different sha256/blake2b, same loadable segments),
#      then QEMU/SeaBIOS must print NOVA_BOOT_OK;
#   2. a fault-point sweep: the install process is killed at every logical
#      operation boundary (and mid-content-write), and EVERY resulting
#      image must boot under SeaBIOS with NOVA_BOOT_OK;
#   3. the crash state between the two pointer commits (root done, EFI twin
#      not) must boot on BOTH firmwares;
#   4. a second release into slot B, then a rollback, each booted;
#   5. the sabotage gate: NOVA_C3_MUTANT=skip-config-stage commits the
#      pointer at a chain that was never written - the resulting image must
#      FAIL to boot (proving the boot oracle catches a premature commit);
#   6. a corrupted rollback target is refused without touching the image.
#
# Exit 0 = all gates pass. Exit 2 = harness could not run (counted as a
# skip, never a pass). Exit 1 = failure.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build/c3"
LOG="$BUILD/qemu-serial.log"
FAILED=0

PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
fi
[ ${#PYCMD[@]} -gt 0 ] || { echo "SKIP: no python launcher"; exit 2; }

[ -f "$ROOT/build/nova.hdd" ] || {
  echo "SKIP: build/nova.hdd missing (stage 3 builds it) - atomic-install oracle needs a real image"
  exit 2
}

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="$LOCALAPPDATA/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi
[ -n "$QEMU_BIN" ] && { command -v "$QEMU_BIN" >/dev/null 2>&1 || [ -f "$QEMU_BIN" ]; } || {
  echo "SKIP: qemu-system-x86_64 not installed - boot oracle cannot run (unit tests still apply)"
  exit 2
}

# UEFI firmware via pflash (same policy as kernel/scripts/test-boot.sh).
UEFI_ARGS=()
WIN_FW=""
for C in "/c/Program Files/qemu/share/edk2-x86_64-code.fd" \
         "$LOCALAPPDATA/Programs/qemu/share/edk2-x86_64-code.fd" \
         /usr/share/OVMF/OVMF_CODE.fd /usr/share/qemu/OVMF_CODE.fd; do
  [ -f "$C" ] && WIN_FW="$C" && break
done
if [ -n "$WIN_FW" ]; then
  [ -f "$BUILD/edk2-code.fd" ] || { mkdir -p "$BUILD"; cp "$WIN_FW" "$BUILD/edk2-code.fd"; }
  W="$(cygpath -w "$BUILD/edk2-code.fd" 2>/dev/null || echo "$BUILD/edk2-code.fd")"
  UEFI_ARGS=(-drive if=pflash,format=raw,readonly=on,file="$W")
fi

mkdir -p "$BUILD"
IMG="$BUILD/c3-ab.hdd"

boot_check() { # <name> <image> <extra qemu args...>
  local name="$1" image="$2"; shift 2
  rm -f "$LOG" "$BUILD/qemu-stderr-last.log"
  local img_arg="$(cygpath -w "$image" 2>/dev/null || echo "$image")"
  # NOVA_BOOT_OK lands within seconds of SeaBIOS+Limine; 12 s is ample and
  # keeps the fault-point sweep (one boot per interruption state) affordable.
  timeout 12 "$QEMU_BIN" -M q35 -m 512M "$@" \
    -drive "file=$img_arg,format=raw" \
    -serial "file:$(cygpath -w "$LOG" 2>/dev/null || echo "$LOG")" \
    -display none -no-reboot \
    -device isa-debug-exit,iobase=0x501,iosize=0x02 \
    > "$BUILD/qemu-stderr-last.log" 2>&1
  local rc=$?
  if grep -q "NOVA_BOOT_OK" "$LOG" 2>/dev/null; then
    echo "  BOOT PASS ($name)"
    return 0
  fi
  echo "  BOOT FAIL ($name) rc=$rc serial tail:"
  tail -4 "$LOG" 2>/dev/null | sed 's/^/    /'
  sed 's/^/    | /' "$BUILD/qemu-stderr-last.log" 2>/dev/null | tail -4
  return 1
}

boot_fail_expected() { # <name> <image> - must NOT reach NOVA_BOOT_OK
  local name="$1" image="$2"
  rm -f "$LOG"
  local img_arg="$(cygpath -w "$image" 2>/dev/null || echo "$image")"
  timeout 12 "$QEMU_BIN" -M q35 -m 512M \
    -drive "file=$img_arg,format=raw" \
    -serial "file:$(cygpath -w "$LOG" 2>/dev/null || echo "$LOG")" \
    -display none -no-reboot \
    -device isa-debug-exit,iobase=0x501,iosize=0x02 \
    > /dev/null 2>&1
  if grep -q "NOVA_BOOT_OK" "$LOG" 2>/dev/null; then
    echo "  SABOTAGE GATE FAIL ($name): mutant image BOOTED - the suite is decorative"
    return 1
  fi
  echo "  SABOTAGE GATE PASS ($name): mutant image refused to boot"
  return 0
}

# --- fixture: factory image copy, throwaway key, marker-suffix release -----
cp "$ROOT/build/nova.hdd" "$IMG"
STATE="$BUILD/state.json"
printf '{"release_sequence": 0}' > "$STATE"
PAYLOAD="$BUILD/payload-r1"
mkdir -p "$PAYLOAD"
cp "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" "$PAYLOAD/c3r1.bin"
printf '\nNOVA-C3-RELEASE-1 MARKER\n' >> "$PAYLOAD/c3r1.bin"
cp "$ROOT/.freebuff/ref/limine/limine-binary/BOOTX64.EFI" "$PAYLOAD/BOOTX64.EFI"

"$PYCMD" - "$ROOT" "$BUILD" <<'PYEOF' || { echo "FAIL: fixture setup"; exit 1; }
import hashlib, sys
from pathlib import Path
root, build = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "tools"))
import nova_trust as nt

secret = hashlib.sha256(b"NOVA C3 stage-14 throwaway key (not the owner key)").digest()
pub = nt.public_key(secret)
(build / "test-key.priv").write_text(nt.private_key_pem(secret), encoding="ascii")
(build / "test-key.pub").write_text(nt.public_key_pem(pub), encoding="ascii")

bootx64 = (root / ".freebuff/ref/limine/limine-binary/BOOTX64.EFI").read_bytes()
kernel = (build / "payload-r1/c3r1.bin").read_bytes()
m = nt.build_manifest("0.2.0-c3r1", 1, "2026-10-05T00:00:00Z",
                      [("c3-payload", "0.2.0-c3r1", build / "payload-r1/c3r1.bin", ["fs:read"])],
                      pub,
                      bootchain={"limine": {"version": "12.9.0", "filename": "BOOTX64.EFI",
                                            "sha256": hashlib.sha256(bootx64).hexdigest(),
                                            "size": len(bootx64)},
                                 "kernel": {"id": "nova-kernel", "version": "0.2.0-c3r1",
                                            "filename": "c3r1.bin",
                                            "sha256": hashlib.sha256(kernel).hexdigest(),
                                            "size": len(kernel)}})
(build / "manifest-r1.json").write_text(nt.dump_manifest(nt.sign_manifest(m, secret)), encoding="ascii")
PYEOF

echo "--- install release 1 (slot A) on a real factory image"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$IMG" \
  --manifest "$BUILD/manifest-r1.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$STATE" || FAILED=1

echo "--- installed image must boot (SeaBIOS)"
boot_check "installed-r1/SeaBIOS" "$IMG" || FAILED=1

echo "--- fault-point sweep: kill the installer at every operation, boot each state"
SPECS="$("$PYCMD" - "$ROOT" <<'PYEOF'
import sys
sys.path.insert(0, str(Path := __import__("pathlib").Path(sys.argv[1]) / "tools"))
import atomic_install
print(" ".join(["before-" + op for op in atomic_install.OPS]
               + ["mid-stage_kernel_content", "mid-stage_config_content"]))
PYEOF
)"
SWEEP=0
SWEEP_FAIL=0
for SPEC in $SPECS; do
  SWEEP=$((SWEEP + 1))
  FIMG="$BUILD/fault-$SPEC.hdd"
  cp "$ROOT/build/nova.hdd" "$FIMG"
  FSTATE="$BUILD/fault-state.json"
  printf '{"release_sequence": 0}' > "$FSTATE"
  "$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$FIMG" \
    --manifest "$BUILD/manifest-r1.json" --payload-dir "$PAYLOAD" \
    --trust-key "$BUILD/test-key.pub" --state-file "$FSTATE" \
    --fault-point "$SPEC" > "$BUILD/fault-$SPEC.log" 2>&1
  FRC=$?
  # Structural safety: active config parses and its target verifies.
  if ! "$PYCMD" - "$FIMG" <<'PYEOF'; then
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent if False else "tools"))
sys.path.insert(0, "tools")
import atomic_install as ai
img = ai.Fat16Image(Path(sys.argv[1]))
try:
    st = ai.active_state(img)
    cfg = ai.parse_config(img.follow_chain(st["config_chain"], st["config_size"]))
    ai.verify_boot_target(img, cfg)
finally:
    img.close()
PYEOF
    echo "  STRUCT FAIL ($SPEC): interrupted image does not parse (rc=$FRC)"
    SWEEP_FAIL=$((SWEEP_FAIL + 1))
    FAILED=1
    continue
  fi
  if [ "$FRC" -eq 137 ]; then
    :
  elif [ "$FRC" -eq 0 ]; then
    echo "  note ($SPEC): mid-point not reachable (single-cluster content); install completed"
  else
    echo "  FAIL ($SPEC): installer rc=$FRC (expected 137 or 0)"
    sed 's/^/    /' "$BUILD/fault-$SPEC.log" | tail -3
    SWEEP_FAIL=$((SWEEP_FAIL + 1))
    FAILED=1
    continue
  fi
  if boot_check "fault:$SPEC/SeaBIOS" "$FIMG"; then :; else
    SWEEP_FAIL=$((SWEEP_FAIL + 1)); FAILED=1
  fi
  rm -f "$FIMG"
done
echo "fault-point sweep: $SWEEP states checked, $SWEEP_FAIL failed"

echo "--- crash between the two commits must boot on BOTH firmwares"
SIMG="$BUILD/split.hdd"
cp "$ROOT/build/nova.hdd" "$SIMG"
printf '{"release_sequence": 0}' > "$BUILD/split-state.json"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$SIMG" \
  --manifest "$BUILD/manifest-r1.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/split-state.json" \
  --fault-point before-commit_efi_ptr > "$BUILD/split.log" 2>&1
[ $? -eq 137 ] || { echo "FAIL: split-state install did not die at the fault point"; FAILED=1; }
boot_check "split-root-committed/SeaBIOS" "$SIMG" || FAILED=1
if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
  boot_check "split-root-committed/OVMF" "$SIMG" "${UEFI_ARGS[@]}" || FAILED=1
else
  echo "  SKIP: no UEFI firmware - OVMF half of the split state untested here"
fi

echo "--- second release (slot B), then rollback; boot each"
PAYLOAD2="$BUILD/payload-r2"
mkdir -p "$PAYLOAD2"
cp "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" "$PAYLOAD2/c3r2.bin"
printf '\nNOVA-C3-RELEASE-2 DIFFERENT MARKER\n' >> "$PAYLOAD2/c3r2.bin"
cp "$ROOT/.freebuff/ref/limine/limine-binary/BOOTX64.EFI" "$PAYLOAD2/BOOTX64.EFI"
"$PYCMD" - "$ROOT" "$BUILD" <<'PYEOF' || { echo "FAIL: r2 manifest setup"; FAILED=1; }
import hashlib, sys
from pathlib import Path
root, build = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "tools"))
import nova_trust as nt
secret = nt.parse_private_key_pem((build / "test-key.priv").read_text(encoding="ascii"))
pub = nt.public_key(secret)
bootx64 = (root / ".freebuff/ref/limine/limine-binary/BOOTX64.EFI").read_bytes()
kernel = (build / "payload-r2/c3r2.bin").read_bytes()
m = nt.build_manifest("0.3.0-c3r2", 2, "2026-10-05T00:00:00Z",
                      [("c3-payload", "0.3.0-c3r2", build / "payload-r2/c3r2.bin", ["fs:read"])],
                      pub,
                      bootchain={"limine": {"version": "12.9.0", "filename": "BOOTX64.EFI",
                                            "sha256": hashlib.sha256(bootx64).hexdigest(),
                                            "size": len(bootx64)},
                                 "kernel": {"id": "nova-kernel", "version": "0.3.0-c3r2",
                                            "filename": "c3r2.bin",
                                            "sha256": hashlib.sha256(kernel).hexdigest(),
                                            "size": len(kernel)}})
(build / "manifest-r2.json").write_text(nt.dump_manifest(nt.sign_manifest(m, secret)), encoding="ascii")
PYEOF
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$IMG" \
  --manifest "$BUILD/manifest-r2.json" --payload-dir "$PAYLOAD2" \
  --trust-key "$BUILD/test-key.pub" --state-file "$STATE" || FAILED=1
boot_check "installed-r2/SeaBIOS" "$IMG" || FAILED=1
if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
  boot_check "installed-r2/OVMF" "$IMG" "${UEFI_ARGS[@]}" || FAILED=1
else
  echo "  SKIP: no UEFI firmware - OVMF boot of the installed image untested here"
fi

"$PYCMD" "$ROOT/tools/atomic_install.py" rollback --image "$IMG" || FAILED=1
"$PYCMD" "$ROOT/tools/atomic_install.py" status --image "$IMG" | tee "$BUILD/status-after-rollback.log"
grep -q "sequence=1" "$BUILD/status-after-rollback.log" \
  || { echo "FAIL: rollback did not return to sequence 1"; FAILED=1; }
boot_check "rolled-back/SeaBIOS" "$IMG" || FAILED=1

echo "--- sabotage gate: premature commit must NOT boot"
MIMG="$BUILD/mutant.hdd"
cp "$ROOT/build/nova.hdd" "$MIMG"
printf '{"release_sequence": 0}' > "$BUILD/mutant-state.json"
NOVA_C3_MUTANT=skip-config-stage "$PYCMD" "$ROOT/tools/atomic_install.py" install \
  --image "$MIMG" --manifest "$BUILD/manifest-r1.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/mutant-state.json" \
  > "$BUILD/mutant.log" 2>&1
[ $? -eq 0 ] || { echo "note: mutant install rc=$? (nonzero)"; }
boot_fail_expected "mutant/SeaBIOS" "$MIMG" || FAILED=1
rm -f "$MIMG"

echo "--- corrupted rollback target must be refused without writing"
# Fresh image, one install (active = slot A, seq 1). The rollback target is
# the FACTORY chain recorded in the slotmap; corrupt THAT, snapshot the
# hash AFTER the corruption, then the refusal must not touch a single byte.
RIMG="$BUILD/refusal.hdd"
cp "$ROOT/build/nova.hdd" "$RIMG"
printf '{"release_sequence": 0}' > "$BUILD/refusal-state.json"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$RIMG" \
  --manifest "$BUILD/manifest-r1.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/refusal-state.json" \
  > "$BUILD/refusal-install.log" 2>&1 || { echo "FAIL: refusal fixture install"; FAILED=1; }
"$PYCMD" - "$RIMG" <<'PYEOF' || { echo "FAIL: could not corrupt target chain"; FAILED=1; }
import sys
from pathlib import Path
sys.path.insert(0, "tools")
import atomic_install as ai
img = ai.Fat16Image(Path(sys.argv[1]))
try:
    row = ai.read_slotmap(img)["factory_config"]
    off = img.cluster_off(int(row["chain"])) + 10
    b = img.pread(off, 1)
    img.pwrite(off, b"\x00" if b != b"\x00" else b"\x01")
    img.flush()
finally:
    img.close()
PYEOF
BEFORE_SHA="$("$PYCMD" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$RIMG")"
"$PYCMD" "$ROOT/tools/atomic_install.py" rollback --image "$RIMG" \
  > "$BUILD/rollback-refusal.log" 2>&1 && { echo "FAIL: rollback accepted a corrupted target"; FAILED=1; }
AFTER_SHA="$("$PYCMD" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$RIMG")"
[ "$BEFORE_SHA" = "$AFTER_SHA" ] || { echo "FAIL: refused rollback modified the image"; FAILED=1; }
grep -q "does not match the slotmap record" "$BUILD/rollback-refusal.log" \
  && echo "  REFUSAL PASS: rollback refused the corrupted target, image byte-identical"
rm -f "$RIMG"

echo
if [ "$FAILED" -ne 0 ]; then
  echo "c3-atomic-install oracle: FAILURES"
  exit 1
fi
echo "c3-atomic-install oracle: PASS"
exit 0
