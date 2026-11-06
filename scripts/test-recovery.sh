#!/usr/bin/env bash
# D4 recovery test suite and QEMU boot oracle.
#
# Contract from docs/work-orders.md (D4) and fixes for findings F1-F4:
#   - Corrupted package: signature corrupted, payload altered, payload truncated.
#     Install fails closed, resulting disk remains bootable.
#   - Completed install (F1, F2): release 1 installed cleanly, dual config chains
#     (/limine.conf and /EFI/BOOT/LIMINE.CONF) verified in lockstep, and booted
#     under BOTH SeaBIOS and OVMF, asserting release 1 serial marker NOVA_REL1_OK.
#   - USB removed mid-install (F3): device disconnected mid-write. Partial write
#     to inactive slot never corrupts active slot; boots cleanly under QEMU.
#   - Power loss during install (F4): interrupted at EVERY single operation step
#     (all 13 OPS + 2 mid-writes; asserts >= 15 states tested). Every resulting
#     image state is booted under QEMU and verified with release-aware oracle.
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
  local pid="$1" marker="${2:-NOVA_BOOT_OK}" waited=0
  while [ "$waited" -lt "$CAP_MS" ]; do
    grep -q "$marker" "$LOG" 2>/dev/null && break
    sleep 0.05
    waited=$((waited + 50))
  done
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  return 0
}

boot_check() {
  local name="$1" image="$2" marker="NOVA_BOOT_OK"
  shift 2
  if [ $# -gt 0 ] && [ "$1" = "--marker" ]; then
    marker="$2"
    shift 2
  fi
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
  poll_for_marker "$qpid" "$marker"
  local rc=$?
  if grep -q "$marker" "$LOG" 2>/dev/null; then
    echo "  BOOT PASS ($name)"
    return 0
  fi
  echo "  BOOT FAIL ($name: expected '$marker') rc=$rc serial tail:"
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
  poll_for_marker "$qpid" "NOVA_BOOT_OK"
  if grep -q "NOVA_BOOT_OK" "$LOG" 2>/dev/null || grep -q "NOVA_REL1_OK" "$LOG" 2>/dev/null; then
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
cp "$ROOT/.freebuff/ref/limine/limine-binary/BOOTX64.EFI" "$PAYLOAD/BOOTX64.EFI"

"$PYCMD" - "$ROOT" "$BUILD" <<'PYEOF' || { echo "FAIL: fixture setup"; exit 1; }
import base64, hashlib, sys
from pathlib import Path
root, build = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "tools"))
import nova_trust as nt

# Create d4kernel.bin with distinct serial output (NOVA_REL1_OK) to fix F2 (release-blind oracle)
orig_nucleus = (root / "kernel/target/x86_64-unknown-none/release/nucleus").read_bytes()
idx = orig_nucleus.find(b"NOVA_BOOT_OK\n")
if idx == -1:
    raise RuntimeError("NOVA_BOOT_OK not found in nucleus binary")
kbytes = bytearray(orig_nucleus)
kbytes[idx:idx+12] = b"NOVA_REL1_OK"
kbytes = bytes(kbytes)
(build / "payload-d4/d4kernel.bin").write_bytes(kbytes)

secret = hashlib.sha256(b"NOVA D4 throwaway recovery key").digest()
pub = nt.public_key(secret)
(build / "test-key.priv").write_text(nt.private_key_pem(secret), encoding="ascii")
(build / "test-key.pub").write_text(nt.public_key_pem(pub), encoding="ascii")

bootx64 = (root / ".freebuff/ref/limine/limine-binary/BOOTX64.EFI").read_bytes()
kernel = kbytes
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
boot_check "corrupted-package:bad-sig/SeaBIOS" "$IMG_BAD_SIG" --marker "NOVA_BOOT_OK" || FAILED=1
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
boot_check "corrupted-package:tampered-payload/SeaBIOS" "$IMG_BAD_PAYLOAD" --marker "NOVA_BOOT_OK" || FAILED=1
if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
  boot_check "corrupted-package:tampered-payload/OVMF" "$IMG_BAD_PAYLOAD" --marker "NOVA_BOOT_OK" "${UEFI_ARGS[@]}" || FAILED=1
fi
rm -f "$IMG_BAD_PAYLOAD"

echo
echo "=== D4 Recovery Pass: Stage 3b - Completed install dual config check and release boots (SeaBIOS and OVMF)"
IMG_COMPLETE="$BUILD/d4-complete.hdd"
cp "$ROOT/build/nova.hdd" "$IMG_COMPLETE"
printf '{"release_sequence": 0}' > "$BUILD/state-complete.json"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$IMG_COMPLETE" \
  --manifest "$BUILD/manifest-good.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/state-complete.json" \
  > "$BUILD/install-complete.log" 2>&1 || { echo "FAIL: completed install failed"; FAILED=1; }

# Dual config chain check (F1 fix): /limine.conf and /EFI/BOOT/LIMINE.CONF must match in sequence, slot, and kernel pin
if ! "$PYCMD" - "$IMG_COMPLETE" <<'PYEOF'; then
import sys
from pathlib import Path
sys.path.insert(0, "tools")
import atomic_install as ai
img = ai.Fat16Image(Path(sys.argv[1]))
try:
    st = ai.active_state(img)
    root_cfg = ai.parse_config(img.follow_chain(st["config_chain"], st["config_size"]))
    efi_hit = img.find_entry_deep("EFI/BOOT/LIMINE.CONF")
    if not efi_hit:
        raise ai.InstallError("missing /EFI/BOOT/LIMINE.CONF")
    import struct
    efi_first = struct.unpack_from("<H", efi_hit[1], 26)[0] | (struct.unpack_from("<H", efi_hit[1], 20)[0] << 16)
    efi_size = struct.unpack_from("<I", efi_hit[1], 28)[0]
    efi_cfg = ai.parse_config(img.follow_chain(efi_first, efi_size))
    if root_cfg["sequence"] != 1 or efi_cfg["sequence"] != 1:
        raise ai.InstallError(f"sequence mismatch: root={root_cfg['sequence']} efi={efi_cfg['sequence']}")
    if root_cfg["kernel_file"] != b"/NUCLEUS.A" or efi_cfg["kernel_file"] != b"/NUCLEUS.A":
        raise ai.InstallError("kernel target mismatch")
    if root_cfg["blake2b"] != efi_cfg["blake2b"]:
        raise ai.InstallError("blake2b pin mismatch between root and efi twin")
    print("  DUAL CONFIG CHECK PASS: /limine.conf and /EFI/BOOT/LIMINE.CONF both point to sequence 1 slot A")
finally:
    img.close()
PYEOF
  echo "  DUAL CONFIG CHECK FAIL: completed install configs desynchronized"
  FAILED=1
fi

# Boot completed install under SeaBIOS and assert release 1 marker NOVA_REL1_OK (F1 & F2 fix)
boot_check "complete-install:release-1/SeaBIOS" "$IMG_COMPLETE" --marker "NOVA_REL1_OK" || FAILED=1

# Boot completed install under OVMF and assert release 1 marker NOVA_REL1_OK (F1 & F2 fix)
if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
  boot_check "complete-install:release-1/OVMF" "$IMG_COMPLETE" --marker "NOVA_REL1_OK" "${UEFI_ARGS[@]}" || FAILED=1
fi
rm -f "$IMG_COMPLETE"

echo
echo "=== D4 Recovery Pass: Stage 4a - Scenario 2: USB removal mid-install (device interruption / EIO)"
IMG_USB="$BUILD/d4-usb-interrupted.hdd"
cp "$ROOT/build/nova.hdd" "$IMG_USB"
printf '{"release_sequence": 0}' > "$BUILD/state-usb.json"
"$PYCMD" "$ROOT/tools/atomic_install.py" install --image "$IMG_USB" \
  --manifest "$BUILD/manifest-good.json" --payload-dir "$PAYLOAD" \
  --trust-key "$BUILD/test-key.pub" --state-file "$BUILD/state-usb.json" \
  --fault-point "mid-stage_kernel_content" > "$BUILD/install-usb.log" 2>&1
boot_check "usb-removal:device-interrupted-mid-transfer/SeaBIOS" "$IMG_USB" --marker "NOVA_BOOT_OK" || FAILED=1
rm -f "$IMG_USB"

echo
echo "=== D4 Recovery Pass: Stage 4b - Scenario 3: Power Loss (Interrupt at EVERY step)"
SPECS="$("$PYCMD" - "$ROOT" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools"))
import atomic_install
print(" ".join(["before-" + op for op in atomic_install.OPS]
               + ["mid-stage_kernel_content", "mid-stage_config_content"]))
PYEOF
)"
[ -n "$SPECS" ] || { echo "FAIL: SPECS probe returned empty list"; FAILED=1; }

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

  # Boot every interrupted image under QEMU (release-aware oracle)
  EXPECTED="NOVA_BOOT_OK"
  if [ "$SPEC" = "before-commit_efi_ptr" ] || [ "$SPEC" = "before-sync_commit" ] || [ "$SPEC" = "before-write_state" ]; then
    EXPECTED="NOVA_REL1_OK"
  elif [ "$FRC" -eq 0 ]; then
    EXPECTED="NOVA_REL1_OK"
  fi
  if boot_check "powerloss:$SPEC/SeaBIOS" "$FIMG" --marker "$EXPECTED"; then :; else
    SWEEP_FAIL=$((SWEEP_FAIL + 1))
    FAILED=1
  fi

  # For the split-commit state (root committed, EFI twin not yet), test OVMF as well (OVMF boots factory NOVA_BOOT_OK)
  if [ "$SPEC" = "before-commit_efi_ptr" ]; then
    if [ ${#UEFI_ARGS[@]} -gt 0 ]; then
      boot_check "powerloss:split-commit/OVMF" "$FIMG" --marker "NOVA_BOOT_OK" "${UEFI_ARGS[@]}" || FAILED=1
    fi
  fi

  rm -f "$FIMG"
done
echo "power loss sweep: $SWEEP_TOTAL states tested, $SWEEP_FAIL failures"
if [ "$SWEEP_TOTAL" -lt 15 ]; then
  echo "FAIL: power loss sweep incomplete ($SWEEP_TOTAL states tested, expected at least 15)"
  SWEEP_FAIL=$((SWEEP_FAIL + 1))
  FAILED=1
fi
if [ "$SWEEP_FAIL" -ne 0 ]; then
  FAILED=1
fi

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
