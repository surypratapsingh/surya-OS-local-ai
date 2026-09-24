#!/usr/bin/env bash
# Full verification pass: kernel build → disk images → structural verify →
# QEMU boot tests (SeaBIOS + OVMF). Skips QEMU cases cleanly when absent.
# A skipped external check is counted in the final verdict, never treated as a pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0
mkdir -p "$ROOT/build"
ORACLE_OUT="$ROOT/build/check-stage7-oracle.log"
BOOTTEST_OUT="$ROOT/build/check-stage8-boottest.log"

PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
elif [ -f "/c/Windows/py.exe" ]; then PYCMD=(/c/Windows/py.exe -3)
fi
[ ${#PYCMD[@]} -gt 0 ] || { echo "check: no usable python launcher found" >&2; exit 1; }

echo "=== check 1/8: boot-chain trust"
bash "$ROOT/scripts/trust.sh" verify || FAILED=1

echo
echo "=== check 2/8: kernel build"
(
  cd "$ROOT/kernel" &&
  if command -v cargo >/dev/null 2>&1; then cargo build --release
  elif [ -f "$HOME/.cargo/bin/cargo" ]; then "$HOME/.cargo/bin/cargo" build --release
  else echo "check: cargo not found (install rustup)" >&2; exit 1
  fi
) || FAILED=1

echo
echo "=== check 3/8: disk images"
export PATH="$HOME/.cargo/bin:$PATH"
bash "$ROOT/scripts/build-disk.sh" release || FAILED=1
NOVA_TEST=1 bash "$ROOT/scripts/build-disk.sh" release >/dev/null || FAILED=1

echo
echo "=== check 4/8: host tests"
"${PYCMD[@]}" "$ROOT/tests/test_font_ref.py" || FAILED=1

echo
echo "=== check 5/8: structural verification (regular image)"
"${PYCMD[@]}" "$ROOT/tools/verify-disk.py" \
  "$ROOT/build/nova.hdd" \
  "$ROOT/kernel" \
  "$ROOT/.freebuff/ref/limine/limine-binary" \
  "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" || FAILED=1

echo
echo "=== check 6/8: fuzz the verifier (${FUZZ_ITERS:-10000} mutations)"
"${PYCMD[@]}" "$ROOT/tools/fuzz-disk.py" "${FUZZ_ITERS:-10000}" \
  --image "$ROOT/build/nova.hdd" \
  --kernel-dir "$ROOT/kernel" \
  --limine-bin "$ROOT/.freebuff/ref/limine/limine-binary" \
  --kernel-elf "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" || FAILED=1

echo
echo "=== check 7/8: external oracles (sgdisk / fsck.fat / mdir)"
"${PYCMD[@]}" "$ROOT/tools/oracle-disk.py" \
  "$ROOT/build/nova.hdd" \
  "$ROOT/kernel" \
  "$ROOT/.freebuff/ref/limine/limine-binary" \
  "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" 2>&1 | tee "$ORACLE_OUT" || FAILED=1

echo
echo "=== check 8/8: QEMU boot tests (SeaBIOS + OVMF)"
bash "$ROOT/kernel/scripts/test-boot.sh" 2>&1 | tee "$BOOTTEST_OUT" || FAILED=1

echo
if [ "$FAILED" -ne 0 ]; then
  echo "check: FAILURES (see above)"
  exit 1
fi

# A skipped check is not a pass: count the SKIP lines the stages emitted
# (oracles without their tools, boot tests without QEMU or firmware) and
# say so in the verdict instead of claiming ALL PASSED.
SKIPPED=$(grep -hE '^[[:space:]]*SKIP |UEFI case skipped' "$ORACLE_OUT" "$BOOTTEST_OUT" 2>/dev/null | wc -l)
if [ "$SKIPPED" -gt 0 ]; then
  echo "check: PASSED WITH $SKIPPED SKIPPED (skips are not passes - install the missing tools for full coverage)"
  exit 0
fi
echo "check: ALL PASSED"
exit 0
