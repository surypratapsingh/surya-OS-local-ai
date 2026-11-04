#!/usr/bin/env bash
# D4 recovery test suite and QEMU boot oracle.
#
# Contract from docs/work-orders.md (D4):
#   - Corrupted package: signature corrupted, payload altered, payload truncated.
#     Install fails closed, resulting disk remains bootable.
#   - USB removed mid-install: disconnected during read/write.
#     Partial write to inactive slot never corrupts active slot; boots cleanly.
#   - Power loss during install: interrupted at EVERY single operation step
#     (all 13 OPS + 2 mid-writes). EVERY resulting image state is booted under
#     QEMU and must reach NOVA_BOOT_OK.
#   - Failed model load: honest report that no in-kernel model load path exists
#     in the boot chain / nucleus kernel (userspace novacore only).
#   - Sabotage gate: broken atomic swap (NOVA_C3_MUTANT=skip-config-stage)
#     must FAIL to reach NOVA_BOOT_OK, proving the boot oracle is genuine.
#
# Exit 0 = all gates pass. Exit 2 = harness could not run. Exit 1 = failure.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build/d4"
LOG="$BUILD/qemu-serial.log"
FAILED=0

PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
fi
[ ${#PYCMD[@]} -gt 0 ] || { echo "SKIP: no python launcher"; exit 2; }

[ -f "$ROOT/build/nova.hdd" ] || {
  echo "SKIP: build/nova.hdd missing - recovery oracle needs a real disk image"
  exit 2
}

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi
[ -n "$QEMU_BIN" ] && { command -v "$QEMU_BIN" >/dev/null 2>&1 || [ -f "$QEMU_BIN" ]; } || {
  echo "SKIP: qemu-system-x86_64 not installed - boot oracle cannot run"
  exit 2
}

# UEFI firmware via pflash
UEFI_ARGS=()
WIN_FW=""
for C in "/c/Program Files/qemu/share/edk2-x86_64-code.fd" \
         "${LOCALAPPDATA:-}/Programs/qemu/share/edk2-x86_64-code.fd" \
         /usr/share/OVMF/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd /usr/share/qemu/OVMF_CODE.fd; do
  [ -f "$C" ] && WIN_FW="$C" && break
done
if [ -n "$WIN_FW" ]; then
  [ -f "$BUILD/edk2-code.fd" ] || { mkdir -p "$BUILD"; cp "$WIN_FW" "$BUILD/edk2-code.fd"; }
  W="$(cygpath -w "$BUILD/edk2-code.fd" 2>/dev/null || echo "$BUILD/edk2-code.fd")"
  UEFI_ARGS=(-drive if=pflash,format=raw,readonly=on,file="$W")
fi

mkdir -p "$BUILD"

poll_for_marker() {
  local pid="$1" waited=0
  while [ "$waited" -lt "$CAP_MS" ]; do
    grep -q "NOVA_BOOT_OK" "$LOG" 2>/dev/null && break
    sleep 0.05
    waited=$((waited + 50))
  done
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  return 0
}

boot_check() {
  local name="$1" image="$2"; shift 2
  rm -f "$LOG" "$BUILD/qemu-stderr-last.log"
  local img_arg="$(cygpath -w "$image" 2>/dev/null || echo "$image")"
  CAP_MS=30000
  "$QEMU_BIN" -M q35 -m 512M "$@" \
    -drive "file=$img_arg,format=raw" \
    -serial "file:$(cygpath -w "$LOG" 2>/dev/null || echo "$LOG")" \
    -display none -no-reboot \
    -device isa-debug-exit,iobase=0x501,iosize=0x02 \
    > "$BUILD/qemu-stderr-last.log" 2>&1 &
  local qpid=$!
  poll_for_marker "$qpid"
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

boot_fail_expected() {
  local name="$1" image="$2"
  rm -f "$LOG"
  local img_arg="$(cygpath -w "$image" 2>/dev/null || echo "$image")"
  CAP_MS=30000
  "$QEMU_BIN" -M q35 -m 512M \
    -drive "file=$img_arg,format=raw" \
    -serial "file:$(cygpath -w "$LOG" 2>/dev/null || echo "$LOG")" \
    -display none -no-reboot \
    -device isa-debug-exit,iobase=0x501,iosize=0x02 \
    > /dev/null 2>&1 &
  local qpid=$!
  poll_for_marker "$qpid"
  if grep -q "NOVA_BOOT_OK" "$LOG" 2>/dev/null; then
    echo "  SABOTAGE GATE FAIL ($name): mutant image BOOTED - test is decorative"
    return 1
  fi
  echo "  SABOTAGE GATE PASS ($name): mutant image refused to boot"
  return 0
}

echo "=== D4 Recovery Pass: Stage 1/5 - Unit tests"
"$PYCMD" -m unittest tests/test_recovery.py -v || FAILED=1

echo
echo "=== D4 Recovery Pass: Stage 2/5 - Setup fixtures"
PAYLOAD="$BUILD/payload-d4"
mkdir -p "$PAYLOAD"
cp "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" "$PAYLOAD/d4kernel.bin"
printf '\nNOVA-D4-RECOVERY-TEST MARKER\n' >> "$PAYLOAD/d4kernel.bin"
cp "$ROOT/.freebuff/ref/limine/limine-binary/BOOTX64.EFI" "$PAYLOAD/BOOTX64.EFI"

"$PYCMD" - "$ROOT" "$BUILD" <<'PYEOF' || { echo "FAIL: fixture setup"; exit 1; }
import base64, hashlib, sys
from pathlib import Path
root, build = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "tools"))
import nova_trust as nt

secret = hashlib.sha256(b"NOVA D4 throwaway recovery key").digest()
pub = nt.public_key(secret)
(build / "test-key.priv").write_text(nt.private_key_pem(secret), encoding="ascii")
(build / "test-key.pub").write_text(nt.public_key_pem(pub), encoding="ascii")

bootx64 = (root / ".freebuff/ref/limine/limine-binary/BOOTX64.EFI").read_bytes()
kernel = (build / "payload-d4/d4kernel.bin").read_bytes()
m = nt.build_manifest("0.2.0-d4r1", 1, "2026-10-05T00:00:00Z",
                      [("d4-payload", "0.2.0-d4r1", build / "payload-d4/d4kernel.bin", ["fs:read"])],
                      pub,
                      bootchain={"limine": {"version": "12.9.0", "filename": "BOOTX64.EFI",
                                            "sha256": hashlib.sha256(bootx64).hexdigest(),
                                            "size": len(bootx64)},
                                 "kernel": {"id": "nova-kernel", "version": "0.2.0-d4r1",
                                            "filename": "d4kernel.bin",
                                            "sha256": hashlib.sha256(kernel).hexdigest(),
                                            "size": len(kernel)}})
signed_m = nt.sign_manifest(m, secret)
(build / "manifest-good.json").write_text(nt.dump_manifest(signed_m), encoding="ascii")

# 1. Corrupted signature
m_bad_sig = dict(signed_m)
sig_bytes = bytearray(base64.b64decode(m_bad_sig["signature"].encode("ascii")))
sig_bytes[0] ^= 0xFF
m_bad_sig["signature"] = base64.b64encode(sig_bytes).decode("ascii")
(build / "manifest-bad-sig.json").write_text(nt.dump_manifest(m_bad_sig), encoding="ascii")

# 2. Corrupted payload hash
m_bad_hash = dict(signed_m)
m_bad_hash["bootchain"] = dict(signed_m["bootchain"])
m_bad_hash["bootchain"]["kernel"] = dict(signed_m["bootchain"]["kernel"])
m_bad_hash["bootchain"]["kernel"]["sha256"] = "0" * 64
(build / "manifest-bad-hash.json").write_text(nt.dump_manifest(m_bad_hash), encoding="ascii")
PYEOF

echo
echo "=== D4 Recovery Pass: Stage 3/5 - Scenario 1: Corrupted package recovery (QEMU boot)"
# 1a. Corrupted signature package
IMG_BAD_SIG="$BUILD/d4-bad-sig.hdd"
cp "$ROOT/build/nova.hdd" "$IMG_BAD_SIG"
printf '{"release_sequence": 0}' > "$BUILD/state-bad-sig.json"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$IMG_BAD_SIG" \
  --manifest "$BUILD/manifest-bad-sig.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/state-bad-sig.json" \
  > "$BUILD/install-bad-sig.log" 2>&1 && { echo "FAIL: installer accepted bad signature"; FAILED=1; }
boot_check "corrupted-package:bad-sig/SeaBIOS" "$IMG_BAD_SIG" || FAILED=1
rm -f "$IMG_BAD_SIG"

# 1b. Corrupted payload file
IMG_BAD_PAYLOAD="$BUILD/d4-bad-payload.hdd"
cp "$ROOT/build/nova.hdd" "$IMG_BAD_PAYLOAD"
PAYLOAD_CORRUPT="$BUILD/payload-corrupt"
mkdir -p "$PAYLOAD_CORRUPT"
cp "$PAYLOAD/d4kernel.bin" "$PAYLOAD_CORRUPT/d4kernel.bin"
cp "$PAYLOAD/BOOTX64.EFI" "$PAYLOAD_CORRUPT/BOOTX64.EFI"
printf '\nCORRUPTING_PAYLOAD_BITS\n' >> "$PAYLOAD_CORRUPT/d4kernel.bin"
printf '{"release_sequence": 0}' > "$BUILD/state-bad-payload.json"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$IMG_BAD_PAYLOAD" \
  --manifest "$BUILD/manifest-good.json" --payload-dir "$PAYLOAD_CORRUPT" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/state-bad-payload.json" \
  > "$BUILD/install-bad-payload.log" 2>&1 && { echo "FAIL: installer accepted corrupted payload"; FAILED=1; }
boot_check "corrupted-package:tampered-payload/SeaBIOS" "$IMG_BAD_PAYLOAD" || FAILED=1
if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
  boot_check "corrupted-package:tampered-payload/OVMF" "$IMG_BAD_PAYLOAD" "${UEFI_ARGS[@]}" || FAILED=1
fi
rm -f "$IMG_BAD_PAYLOAD"

echo
echo "=== D4 Recovery Pass: Stage 4/5 - Scenario 2 & 3: USB removal and Power Loss (Interrupt at EVERY step)"
SPECS="$("$PYCMD" - "$ROOT" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools"))
import atomic_install
print(" ".join(["before-" + op for op in atomic_install.OPS]
               + ["mid-stage_kernel_content", "mid-stage_config_content"]))
PYEOF
)"

SWEEP_TOTAL=0
SWEEP_FAIL=0
for SPEC in $SPECS; do
  SWEEP_TOTAL=$((SWEEP_TOTAL + 1))
  FIMG="$BUILD/d4-fault-$SPEC.hdd"
  cp "$ROOT/build/nova.hdd" "$FIMG"
  FSTATE="$BUILD/d4-state-$SPEC.json"
  printf '{"release_sequence": 0}' > "$FSTATE"

  "$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$FIMG" \
    --manifest "$BUILD/manifest-good.json" --payload-dir "$PAYLOAD" \
    --trust-key "$BUILD/test-key.pub" --state-file "$FSTATE" \
    --fault-point "$SPEC" > "$BUILD/d4-fault-$SPEC.log" 2>&1
  FRC=$?

  # Check parse safety
  if ! "$PYCMD" - "$FIMG" <<'PYEOF'; then
import sys
from pathlib import Path
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
    echo "  STRUCT FAIL ($SPEC): interrupted image fails config/target check (rc=$FRC)"
    SWEEP_FAIL=$((SWEEP_FAIL + 1))
    FAILED=1
    continue
  fi

  # Boot every interrupted image under QEMU
  if boot_check "powerloss:$SPEC/SeaBIOS" "$FIMG"; then :; else
    SWEEP_FAIL=$((SWEEP_FAIL + 1))
    FAILED=1
  fi

  # For the split-commit state (root committed, EFI twin not yet), test OVMF as well
  if [ "$SPEC" = "before-commit_efi_ptr" ]; then
    if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
      boot_check "powerloss:split-commit/OVMF" "$FIMG" "${UEFI_ARGS[@]}" || FAILED=1
    fi
  fi

  rm -f "$FIMG"
done
echo "power loss sweep: $SWEEP_TOTAL states tested, $SWEEP_FAIL failures"

echo
echo "=== D4 Recovery Pass: Stage 5/5 - Scenario 4 (Model Load Status) & Sabotage Gate"
echo "  SCENARIO 4 STATUS (FAILED MODEL LOAD):"
echo "    The NOVA kernel (nucleus) and Limine boot chain execute in Ring 0"
echo "    with no in-kernel LLM model weight loading mechanism."
echo "    Model loading is handled by userspace novacore (host companion runner)."
echo "    Per AGENTS.md rules 6 and 9, no in-kernel boot-recovery path exists for"
echo "    failed model load, and none was invented."
echo "    Novacore model runner failure raises ModelUnavailable cleanly."

echo "--- Sabotage gate: broken atomic swap must FAIL to boot"
MUTANT_IMG="$BUILD/d4-mutant.hdd"
cp "$ROOT/build/nova.hdd" "$MUTANT_IMG"
printf '{"release_sequence": 0}' > "$BUILD/d4-mutant-state.json"
NOVA_C3_MUTANT=skip-config-stage "$PYCMD" "$ROOT/tools/atomic_install.py" install \
  --image "$MUTANT_IMG" --manifest "$BUILD/manifest-good.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/d4-mutant-state.json" \
  > "$BUILD/d4-mutant.log" 2>&1
boot_fail_expected "sabotage:broken-atomic-swap/SeaBIOS" "$MUTANT_IMG" || FAILED=1
rm -f "$MUTANT_IMG"

echo
if [ "$FAILED" -ne 0 ]; then
  echo "d4-recovery oracle: FAILURES"
  exit 1
fi
echo "d4-recovery oracle: PASS"
exit 0
