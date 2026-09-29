#!/usr/bin/env bash
# Retry wrapper for the intermittent Windows Smart App Control block of
# rust-lld (K4a follow-on; the block was first seen 2026-09-28, see
# progress.md and docs/sac-build-blocks.md).
#
# What SAC does: Smart App Control (state On) intermittently blocks
# rust-lld.exe with CodeIntegrity events 3033/3077/3118. Cargo surfaces it
# as "linking with rust-lld failed ... os error 4551" (ERROR_POSSIBLE_LOCK,
# Win32 error 4551 -- the same code the observed failures printed). The
# block is transient on this machine: the identical command succeeded on
# retry, and SAC's blocking verdicts are not sticky for unchanged binaries.
#
# What this wrapper does: runs the given cargo command; if the COMBINED
# stdout+stderr matches the SAC signature (rust-lld failed / os error 4551
# / CodeIntegrity), it waits SAC_BACKOFF_SECONDS (default 5) and retries,
# up to SAC_MAX_ATTEMPTS (default 3) attempts total. Any other failure is
# re-emitted immediately with the original exit code -- a genuine compile
# error or a hard linker failure must fail this wrapper exactly like the
# bare command would (a wrapper that retries everything would stretch
# real breakage into timeouts and hide it).
#
# Usage: scripts/cargo-retry.sh <cargo args...>   (run from the crate dir)
# Env:   SAC_MAX_ATTEMPTS (default 3, min 1), SAC_BACKOFF_SECONDS (default 5)
# Exit:  0 = built; 1 = SAC signature persisted after all attempts;
#        <cargo's code> = non-SAC failure passed through unchanged.
set -uo pipefail

MAX="${SAC_MAX_ATTEMPTS:-3}"
BACKOFF="${SAC_BACKOFF_SECONDS:-5}"
case "$MAX" in ''|*[!0-9]*) MAX=3;; esac
[ "$MAX" -ge 1 ] || MAX=1
case "$BACKOFF" in ''|*[!0-9]*) BACKOFF=5;; esac

if [ $# -eq 0 ]; then
  echo "cargo-retry: no cargo arguments given" >&2
  exit 1
fi

# Detection signature: the error text the observed SAC-blocked builds
# actually produced (CodeIntegrity 3033/3077/3118 surface through cargo as
# a rust-lld link failure with Win32 error 4551). Kept tight on purpose --
# over-broad matching would retry real breakage and hide it (AGENTS.md
# rule 8: never loosen a failure to get a green run).
PATTERN='rust-lld failed|os error 4551|CodeIntegrity'

attempt=1
while :; do
  OUT="$(cargo "$@" 2>&1)"
  RC=$?
  if [ $RC -eq 0 ]; then
    [ -n "$OUT" ] && printf '%s\n' "$OUT"
    exit 0
  fi
  printf '%s\n' "$OUT"
  if printf '%s' "$OUT" | grep -Eq "$PATTERN"; then
    if [ "$attempt" -ge "$MAX" ]; then
      echo "cargo-retry: SAC block signature persisted after $MAX attempt(s); failing" >&2
      exit 1
    fi
    echo "cargo-retry: Smart App Control blocked rust-lld (os error 4551 family); attempt $attempt/$MAX failed, retrying in ${BACKOFF}s" >&2
    attempt=$((attempt + 1))
    sleep "$BACKOFF"
  else
    echo "cargo-retry: non-SAC failure (exit $RC) passed through unchanged" >&2
    exit "$RC"
  fi
done
