#!/usr/bin/env bash
# Full verification pass: kernel build → disk images → structural verify →
# QEMU boot tests (SeaBIOS + OVMF). Skips QEMU cases cleanly when absent.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
elif [ -f "/c/Windows/py.exe" ]; then PYCMD=(/c/Windows/py.exe -3)
fi
[ ${#PYCMD[@]} -gt 0 ] || { echo "check: no usable python launcher found" >&2; exit 1; }

echo "=== check 1/6: boot-chain trust"
bash "$ROOT/scripts/trust.sh" verify || FAILED=1

echo
echo "=== check 2/6: kernel build"
(
  cd "$ROOT/kernel" &&
  if command -v cargo >/dev/null 2>&1; then cargo build --release
  elif [ -f "$HOME/.cargo/bin/cargo" ]; then "$HOME/.cargo/bin/cargo" build --release
  else echo "check: cargo not found (install rustup)" >&2; exit 1
  fi
) || FAILED=1

echo
echo "=== check 3/6: disk images"
export PATH="$HOME/.cargo/bin:$PATH"
bash "$ROOT/scripts/build-disk.sh" release || FAILED=1
NOVA_TEST=1 bash "$ROOT/scripts/build-disk.sh" release >/dev/null || FAILED=1

echo
echo "=== check 4/6: host tests"
"${PYCMD[@]}" "$ROOT/tests/test_font_ref.py" || FAILED=1

echo
echo "=== check 5/6: structural verification (regular image)"
"${PYCMD[@]}" "$ROOT/tools/verify-disk.py" \
  "$ROOT/build/nova.hdd" \
  "$ROOT/kernel" \
  "$ROOT/.freebuff/ref/limine/limine-binary" \
  "$ROOT/kernel/target/x86_64-unknown-none/release/nucleus" || FAILED=1

echo
echo "=== check 6/6: QEMU boot tests (SeaBIOS + OVMF)"
bash "$ROOT/kernel/scripts/test-boot.sh" || FAILED=1

echo
if [ "$FAILED" -eq 0 ]; then
  echo "check: ALL PASSED"
else
  echo "check: FAILURES (see above)"
fi
exit $FAILED
