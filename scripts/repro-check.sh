#!/usr/bin/env bash
# R15 Reproducible Builds Check (C5).
#
# Builds the committed tree twice from two independent `git archive HEAD`
# extractions in two different mktemp directories, then hashes the kernel ELF
# and build/nova.hdd from each.  Both files must exist, be non-empty, and hash
# identically; any difference is printed with `cmp -l | head` and the script
# exits non-zero.
#
# Nothing here refers to a host path: the repo root comes from this script's
# own location and every build happens inside its own extraction directory.
#
# Usage:
#   bash scripts/repro-check.sh           # two clean builds must match
#   bash scripts/repro-check.sh --plant   # change one input; the check must fire
#
# SOURCE_DATE_EPOCH must be an integer (seconds since the epoch).  It defaults
# to the commit time of HEAD; an explicit value is honoured.
#
# Exit codes: 0 identical, 1 different or build failure, 2 usage/environment.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLANT=0
case "${1:-}" in
  "") ;;
  --plant) PLANT=1 ;;
  *) echo "usage: bash scripts/repro-check.sh [--plant]" >&2; exit 2 ;;
esac

SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct)}"
case "$SOURCE_DATE_EPOCH" in
  ''|*[!0-9]*)
    echo "FAIL: SOURCE_DATE_EPOCH must be an integer, got '$SOURCE_DATE_EPOCH'" >&2
    exit 2
    ;;
esac
export SOURCE_DATE_EPOCH

DIR1="$(mktemp -d "${TMPDIR:-/tmp}/nova-repro-a-XXXXXX")"
DIR2="$(mktemp -d "${TMPDIR:-/tmp}/nova-repro-b-XXXXXX")"
cleanup() { rm -rf "$DIR1" "$DIR2"; }
trap cleanup EXIT

hash_file() {
  # $1 = path, $2 = label on stderr
  if [ ! -f "$1" ]; then
    echo "FAIL: $2 missing: $1" >&2
    return 1
  fi
  if [ ! -s "$1" ]; then
    echo "FAIL: $2 is empty: $1" >&2
    return 1
  fi
  local digest
  if command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum "$1" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then
    digest=$(shasum -a 256 "$1" | awk '{print $1}')
  else
    echo "FAIL: no sha256sum or shasum on PATH" >&2
    return 1
  fi
  case "$digest" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) echo "FAIL: $2 produced a non-hex digest '$digest'" >&2; return 1 ;;
  esac
  if [ "${#digest}" -ne 64 ]; then
    echo "FAIL: $2 digest length ${#digest}, expected 64" >&2
    return 1
  fi
  printf '%s\n' "$digest"
}

echo "=== R15 reproducible builds check ==="
echo "repo:              $ROOT"
echo "SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH (integer)"
echo "build 1 dir:       $DIR1"
echo "build 2 dir:       $DIR2"
echo "planted input:     $PLANT"
echo

echo "=== extracting HEAD twice (git archive) ==="
git -C "$ROOT" archive HEAD | tar -x -C "$DIR1"
git -C "$ROOT" archive HEAD | tar -x -C "$DIR2"
if [ ! -f "$DIR1/kernel/src/main.rs" ] || [ ! -f "$DIR2/kernel/src/main.rs" ]; then
  echo "FAIL: extraction produced no kernel/src/main.rs" >&2
  exit 2
fi

if [ "$PLANT" = 1 ]; then
  STAMP="$(date +%s)"
  echo "=== plant: writing const _P: u64 = $STAMP into copy 1 kernel/src/main.rs ==="
  printf '\nconst _P: u64 = %s;\n' "$STAMP" >> "$DIR1/kernel/src/main.rs"
  # Use the constant so it cannot be dropped by the optimizer: the boot banner
  # prints it.  Only copy 1 is changed.
  sed -i 's/sprintln!("NOVA_BOOT_OK");/sprintln!("NOVA_BOOT_OK {}", _P);/' \
    "$DIR1/kernel/src/main.rs"
  if ! grep -q 'NOVA_BOOT_OK {}"\, _P' "$DIR1/kernel/src/main.rs"; then
    echo "FAIL: the plant did not take (boot banner line not found)" >&2
    exit 2
  fi
  grep -n 'const _P\|NOVA_BOOT_OK' "$DIR1/kernel/src/main.rs" | tail -2
  echo
fi

build_copy() {
  local dir="$1" label="$2" log
  log="$dir/build.log"
  echo "=== build $label ==="
  if ! ( cd "$dir" && NOVA_TEST=0 bash scripts/build-disk.sh ) >"$log" 2>&1; then
    echo "FAIL: build $label failed; tail of $log:" >&2
    tail -25 "$log" >&2
    return 1
  fi
  tail -6 "$log"
  echo
}

build_copy "$DIR1" 1 || exit 1
build_copy "$DIR2" 2 || exit 1

ELF1="$DIR1/kernel/target/x86_64-unknown-none/release/nucleus"
ELF2="$DIR2/kernel/target/x86_64-unknown-none/release/nucleus"
IMG1="$DIR1/build/nova.hdd"
IMG2="$DIR2/build/nova.hdd"

echo "=== hashing ==="
ELF_SHA1="$(hash_file "$ELF1" 'build 1 kernel ELF')" || exit 1
ELF_SHA2="$(hash_file "$ELF2" 'build 2 kernel ELF')" || exit 1
IMG_SHA1="$(hash_file "$IMG1" 'build 1 nova.hdd')" || exit 1
IMG_SHA2="$(hash_file "$IMG2" 'build 2 nova.hdd')" || exit 1

printf 'kernel ELF 1 %s  %s\n' "$ELF_SHA1" "$(wc -c <"$ELF1") bytes"
printf 'kernel ELF 2 %s  %s\n' "$ELF_SHA2" "$(wc -c <"$ELF2") bytes"
printf 'nova.hdd   1 %s  %s\n' "$IMG_SHA1" "$(wc -c <"$IMG1") bytes"
printf 'nova.hdd   2 %s  %s\n' "$IMG_SHA2" "$(wc -c <"$IMG2") bytes"
echo

STATUS=0
if [ "$ELF_SHA1" != "$ELF_SHA2" ]; then
  echo "DIFFER: kernel ELF"
  echo '$ cmp -l <elf1> <elf2> | head'
  cmp -l "$ELF1" "$ELF2" | head || true
  STATUS=1
else
  echo "IDENTICAL: kernel ELF"
fi
if [ "$IMG_SHA1" != "$IMG_SHA2" ]; then
  echo "DIFFER: nova.hdd"
  echo '$ cmp -l <hdd1> <hdd2> | head'
  cmp -l "$IMG1" "$IMG2" | head || true
  STATUS=1
else
  echo "IDENTICAL: nova.hdd"
fi
echo

if [ "$STATUS" -eq 0 ]; then
  echo "RESULT: reproducible (both kernel ELF and nova.hdd match)"
  exit 0
fi

if [ "$PLANT" = 1 ]; then
  echo "RESULT: planted build-input change detected (expected non-zero exit)"
else
  echo "RESULT: builds differ; non-determinism is a finding, not a pass"
fi
exit 1
