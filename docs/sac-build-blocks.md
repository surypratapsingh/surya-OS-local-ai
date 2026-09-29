# Windows Smart App Control blocks of rust-lld — detection and retry procedure

Status: host-specific operational doc (Windows dev machines). CI runs Linux
and never sees this. Written 2026-09-29 after the block recurred during K4a
(see `progress.md`, 2026-09-28 entry, "Blockers").

## The signature (what to look for)

Smart App Control (state `On`) intermittently refuses to let `rust-lld.exe`
run. Through cargo the failure reads, in the build output:

    error: linking with `rust-lld` failed: exit code: 0xc0000135
      ...
      = note: os error 4551

Windows Event Viewer shows the underlying verdicts as CodeIntegrity events
3033 / 3077 / 3118 naming `rust-lld.exe`. The operative text for automated
detection is the rust-lld link failure with **os error 4551**
(ERROR_POSSIBLE_LOCK — SAC's blocking verdict surfaced through the loader).

Observed: 2026-09-28, kernel release build, Windows 11, SAC state `On`. The
identical command succeeded on a later run with no source change, which is
why a retry is the correct response and why the retry must stay narrow.

## Automated handling (already wired)

`scripts/cargo-retry.sh` wraps the kernel build in `scripts/check.sh`
(stage 2). It retries **only** the signature above (`rust-lld failed | os
error 4551 | CodeIntegrity`), up to `SAC_MAX_ATTEMPTS` (default 3) attempts
with `SAC_BACKOFF_SECONDS` (default 5) between attempts. Any other failure
— a genuine compile error, a hard linker failure — is passed through
immediately with its original exit code, so the wrapper cannot turn real
breakage into a slow green run.

Proven behaviour (fake-cargo harness, `scripts/test-cargo-retry.sh`):

    SAC block then success is retried to rc 0 (rc=0)
    retry is announced with the SAC verdict
    non-SAC failure passes through with its own code (rc=42)
    non-SAC failure is not retried (one diagnostic line) (rc=1)
    persistent SAC block exhausts the budget (rc 1) (rc=1)
    persistent SAC block was attempted exactly 3 times (rc=3)
    exhaustion is named as such (not a silent pass)
    cargo-retry harness: PASS (3 scenarios)

## Manual procedure (outside check.sh)

1. See `os error 4551` (or a CodeIntegrity 3033/3077/3118 event naming
   rust-lld) in a failed cargo build.
2. Simply re-run the same command (`cd kernel && cargo build --release`, or
   `bash scripts/check.sh`). The observed block cleared on retry; SAC's
   verdict is not sticky for an unchanged binary.
3. If it persists across `SAC_MAX_ATTEMPTS`: check SAC state
   (`Get-MpPreference | select EnableSmartAppControl` — state `On`, `Ev`).
   Turning SAC off is a machine-owner decision, NOT something a build
   script should ever do; the repo treats a persistent block as a hard
   build failure (exit 1), never as a skip.
4. Never silence, filter, or work around the error text in logs — the
   signature is the detection mechanism; an undetectable block would have
   to be diagnosed by hand every time.

## Unverified / host-specific caveats

- The retry-clears-block observation is from a single recurrence
  (2026-09-28); the wrapper's real-world retry path has not yet fired on
  this machine since it was written. The harness scenarios, not a real
  SAC block, are the current evidence.
- os error 4551 is treated as exclusively-SAC here because that is the only
  observed source on this host. If a different cause ever produces the same
  Win32 error, the wrapper will retry it harmlessly (a real failure still
  fails after 3 attempts with the full output shown).
- Linux CI cannot exercise this path at all; the wrapper is a transparent
  passthrough there (verified by the real build below, and by check.sh
  stage 2 running through it on this machine).
