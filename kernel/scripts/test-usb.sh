#!/usr/bin/env bash
# K5a USB external oracle: run the NOVA selftest image with xHCI and USB mass storage,
# asserting clean exit 33, checking all usb gate probes, and diffing directory listings
# and file bytes read over USB against mdir and mcopy.
set -uo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
BUILD="$REPO_ROOT/build"
IMAGE="$BUILD/nova-selftest.hdd"
CORPUS="$REPO_ROOT/kernel/src/fat32_corpus.img"
LOG="$BUILD/qemu-serial-usb.log"

QEMU_BIN="${QEMU_BIN:-}"
if [ -z "$QEMU_BIN" ]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then QEMU_BIN=qemu-system-x86_64
  elif [ -f "/c/Program Files/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="/c/Program Files/qemu/qemu-system-x86_64.exe"
  elif [ -f "${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe" ]; then QEMU_BIN="${LOCALAPPDATA:-}/Programs/qemu/qemu-system-x86_64.exe"
  fi
fi
if [ -z "$QEMU_BIN" ] || { ! command -v "$QEMU_BIN" >/dev/null 2>&1 && [ ! -f "$QEMU_BIN" ]; }; then
  echo "SKIP: qemu-system-x86_64 not installed - USB tests skipped"
  exit 0
fi

if [ ! -f "$IMAGE" ]; then
  echo "SKIP: $IMAGE missing - run: NOVA_TEST=1 bash scripts/build-disk.sh release"
  exit 2
fi

if [ ! -f "$CORPUS" ]; then
  echo "SKIP: $CORPUS missing - run: py -3 tools/gen-fat32-corpus.py"
  exit 2
fi

to_win() {
  command -v cygpath >/dev/null 2>&1 && cygpath -w "$1" 2>/dev/null || echo "$1"
}

PYCMD=()
if command -v py >/dev/null 2>&1; then PYCMD=(py -3)
elif command -v python3 >/dev/null 2>&1; then PYCMD=(python3)
elif command -v python >/dev/null 2>&1; then PYCMD=(python)
fi

rm -f "$LOG"

echo "=== running NOVA selftest with qemu-xhci and usb-storage ==="
timeout 60 "$QEMU_BIN" -M q35 -m 512M \
  -drive "file=$(to_win "$IMAGE"),format=raw" \
  -device qemu-xhci,id=xhci \
  -drive "if=none,id=pendrive,format=raw,file=$(to_win "$CORPUS")" \
  -device usb-storage,bus=xhci.0,drive=pendrive \
  -serial "file:$(to_win "$LOG")" -display none -no-reboot \
  -device isa-debug-exit,iobase=0x501,iosize=0x02
QEMU_RC=$?

if [ "$QEMU_RC" -ne 33 ]; then
  echo "FAIL: selftest boot did not exit cleanly (rc=$QEMU_RC; 35 = gate failed)"
  tail -20 "$LOG" 2>/dev/null | sed 's/^/    /'
  exit 1
fi

echo "QEMU exit code: $QEMU_RC (PASS)"

# Verify usb gate checks in log
USB_OK_COUNT=$(grep -c "^  ok    .*usb" "$LOG" || true)
echo "USB checks passed in guest: $USB_OK_COUNT"
if [ "$USB_OK_COUNT" -lt 6 ]; then
  echo "FAIL: expected at least 6 usb-gate checks passed, found $USB_OK_COUNT"
  exit 1
fi

# If mdir is installed, diff USBLIST blocks against mdir
if command -v mdir >/dev/null 2>&1 && [ ${#PYCMD[@]} -gt 0 ]; then
  MDIR_OUT="$BUILD/test-usb-mdir.log"
  : > "$MDIR_OUT"
  for D in "::/" "::/projects" "::/projects/2026 january notes"; do
    echo "FATLIST $D" >> "$MDIR_OUT"
    mdir -i "$CORPUS" "$D" 2>&1 \
      | grep -vE "^ Volume in drive |^ Volume Serial Number |^Directory for |bytes free$|^[[:space:]]*$" >> "$MDIR_OUT"
  done
  echo "FATLIST END" >> "$MDIR_OUT"

  "${PYCMD[@]}" - "$LOG" "$MDIR_OUT" <<'PYEOF'
import sys
kpath, mpath = sys.argv[1], sys.argv[2]

def blocks(path, tag):
    out, cur = {}, None
    for line in open(path, encoding='utf-8', errors='replace').read().splitlines():
        line = line.rstrip('\r')
        if line.startswith(tag + ' ') and line != tag + ' END':
            cur = line[len(tag)+1:].strip()
            out[cur] = []
        elif line == tag + ' END':
            cur = None
        elif cur is not None and line.strip():
            out[cur].append(line)
    return out

k = blocks(kpath, 'USBLIST')
m = blocks(mpath, 'FATLIST')
if not k or len(k) != len(m):
    print(f'  USBLIST block count: kernel {len(k)} vs mdir {len(m)}')
    print('mdir-vs-usb: DIFFER')
    sys.exit(1)

keys_k = list(k.keys())
keys_m = list(m.keys())
ok = True
for i, kk in enumerate(keys_k):
    km = keys_m[i] if i < len(keys_m) else None
    if km is None or k[kk] != m[km]:
        ok = False
        print(f'  MISMATCH usb {kk!r} vs mdir {km!r}:')
        for a, b in zip(k[kk], (m.get(km) or [])):
            if a != b:
                print(f'    usb : {a!r}')
                print(f'    mdir: {b!r}')
print('mdir-vs-usb:', 'IDENTICAL' if ok else 'DIFFER')
sys.exit(0 if ok else 1)
PYEOF
  if [ $? -ne 0 ]; then
    echo "FAIL: USBLIST vs mdir diff failed"
    exit 1
  fi
else
  echo "SKIP: mdir not available on host - CI covers mdir oracle"
fi

# If mcopy is installed, diff USBMCOPY blocks against mcopy
if command -v mcopy >/dev/null 2>&1 && [ ${#PYCMD[@]} -gt 0 ]; then
  "${PYCMD[@]}" - "$LOG" "$CORPUS" <<'PYEOF'
import subprocess
import sys
kpath, corpus = sys.argv[1], sys.argv[2]

def mcopy_blocks(path):
    out, cur = {}, None
    for line in open(path, encoding='utf-8', errors='replace').read().splitlines():
        line = line.rstrip('\r')
        if line.startswith('USBMCOPY ') and line != 'USBMCOPY END':
            cur = line[9:].strip()
            out[cur] = []
        elif line == 'USBMCOPY END':
            cur = None
        elif cur is not None:
            out[cur].append(line)
    return out

k = mcopy_blocks(kpath)
if not k:
    print('  USBMCOPY: no blocks found in kernel log')
    sys.exit(1)

ok = True
for target, klines in k.items():
    proc = subprocess.run(['mcopy', '-i', corpus, target, '-'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print(f'  mcopy failed for {target}: {proc.stderr.decode("utf-8", "replace")}')
        ok = False
        continue
    m_content = proc.stdout.decode('utf-8', 'replace').splitlines()
    if klines != m_content:
        print(f'  MISMATCH USBMCOPY vs mcopy for {target}:')
        print(f'    kernel: {klines!r}')
        print(f'    mcopy : {m_content!r}')
        ok = False

print('mcopy-vs-usb:', 'IDENTICAL' if ok else 'DIFFER')
sys.exit(0 if ok else 1)
PYEOF
  if [ $? -ne 0 ]; then
    echo "FAIL: USBMCOPY vs mcopy diff failed"
    exit 1
  fi
else
  echo "SKIP: mcopy not available on host - CI covers mcopy oracle"
fi

echo "PASS: all K5a USB tests verified"
exit 0
