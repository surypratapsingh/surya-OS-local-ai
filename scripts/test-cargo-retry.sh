#!/usr/bin/env bash
# Harness for scripts/cargo-retry.sh (the SAC retry wrapper). Proves, with
# a fake cargo on PATH, the three behaviours the wrapper promises:
#
#   1. RETRY: a SAC-signature failure ("os error 4551" / rust-lld) is
#      retried, and success on a later attempt yields exit 0 (the observed
#      real-world behaviour: the block clears on retry).
#   2. NO-RETRY: a NON-signature failure (a plain compile error) is passed
#      through with its ORIGINAL exit code, exactly once -- a wrapper that
#      retried real breakage would hide it (AGENTS.md rule 8).
#   3. PERSISTENT-SAC: a SAC-signature failure on every attempt exhausts
#      the attempt budget and exits 1 with a named verdict, never 0.
#
# The fake cargo is a committed script invoked through PATH, so nothing
# here needs a real toolchain; the wrapper is exercised exactly as
# check.sh invokes it. Exit 0 = all scenarios pass, 1 = a scenario failed
# (this harness has no SKIP case: it needs only bash itself).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAKEBIN="$(mktemp -d)"
trap 'rm -rf "$FAKEBIN"' EXIT

FAILED=0
note() { printf '  %s\n' "$1"; }
check() { # check <label> <expected-rc> <actual-rc>
  if [ "$2" -eq "$3" ]; then
    note "ok    $1 (rc=$3)"
  else
    note "FAIL  $1 (expected rc=$2, got rc=$3)"
    FAILED=1
  fi
}

make_fake_cargo() { # make_fake_cargo <script-body>
  cat > "$FAKEBIN/cargo"
  chmod +x "$FAKEBIN/cargo"
}

run_wrapper() { # run_wrapper <attempts> <backoff> ; rest from $SCRIPT_ARGS
  PATH="$FAKEBIN:$PATH" \
  SAC_MAX_ATTEMPTS="$1" SAC_BACKOFF_SECONDS="$2" \
    bash "$REPO_ROOT/scripts/cargo-retry.sh" build --release
}

# --- 1. SAC block on attempt 1, clean build on attempt 2 -> rc 0, retried
STATE="$FAKEBIN/state1"; echo 0 > "$STATE"
make_fake_cargo <<FAKE
#!/usr/bin/env bash
N=\$(cat "$STATE")
echo \$((N + 1)) > "$STATE"
if [ "\$N" -eq 0 ]; then
  echo "error: linking with \`rust-lld\` failed: exit code: 0xc0000135" >&2
  echo "note: os error 4551" >&2
  exit 101
fi
echo "    Finished \`release\` profile [optimized] target(s)"
exit 0
FAKE
OUT="$(run_wrapper 3 0 2>&1)"; RC=$?
check "SAC block then success is retried to rc 0" 0 "$RC"
printf '%s\n' "$OUT" | grep -q "attempt 1/3 failed, retrying" \
  && note "ok    retry is announced with the SAC verdict" \
  || { note "FAIL  retry was not announced: $OUT"; FAILED=1; }

# --- 2. Non-SAC compile error -> passthrough of the ORIGINAL code, once
make_fake_cargo <<'FAKE'
#!/usr/bin/env bash
echo "error[E0425]: cannot find value `NOVA` in this scope" >&2
exit 42
FAKE
OUT="$(run_wrapper 3 0 2>&1)"; RC=$?
check "non-SAC failure passes through with its own code" 42 "$RC"
ATTEMPTS=$(printf '%s\n' "$OUT" | grep -c "cargo-retry:"); 
check "non-SAC failure is not retried (one diagnostic line)" 1 "$ATTEMPTS"

# --- 3. SAC block on every attempt -> budget exhausted, rc 1
STATE="$FAKEBIN/state3"; echo 0 > "$STATE"
make_fake_cargo <<FAKE
#!/usr/bin/env bash
N=\$(cat "$STATE"); echo \$((N + 1)) > "$STATE"
echo "error: linking with rust-lld failed ... os error 4551" >&2
exit 101
FAKE
OUT="$(run_wrapper 3 0 2>&1)"; RC=$?
check "persistent SAC block exhausts the budget (rc 1)" 1 "$RC"
CALLS=$(cat "$STATE")
check "persistent SAC block was attempted exactly 3 times" 3 "$CALLS"
printf '%s\n' "$OUT" | grep -q "signature persisted after 3 attempt" \
  && note "ok    exhaustion is named as such (not a silent pass)" \
  || { note "FAIL  exhaustion verdict missing: $OUT"; FAILED=1; }

if [ "$FAILED" -eq 0 ]; then
  echo "cargo-retry harness: PASS (3 scenarios)"
else
  echo "cargo-retry harness: FAIL"
fi
exit "$FAILED"
