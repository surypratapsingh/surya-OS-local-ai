# Windows Smart App Control blocks of build tools — detection and retry procedure

Status: host-specific operational doc (Windows dev machines). CI runs Linux
and never sees this. Written 2026-09-29 after the block recurred during K4a
(see `progress.md`, 2026-09-28 entry, "Blockers"). Extended 2026-10-02 with a
second, non-transient case (`limine.exe`); see "Second observed case" below —
the retry procedure in this file does NOT apply to it.

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

## Second observed case: `limine.exe` (exec denial; 2026-10-02 — lifted 2026-10-03, see update below)

Observed: 2026-10-02, `scripts/build-disk.sh` line 123, `"$TOOL" bios-install
"$OUT"`. Full write-up and transcripts in
`docs/logs/cap-oracle-dummy-bootchain.log`, section 4.

Same enforcing mechanism, different symptom. Through bash the vendored tool
fails as an exec denial, not a linker error:

    /usr/bin/bash: line 1: .../limine-tool-windows-x86/limine.exe: Permission denied
    rc=126

`cmd.exe` names it plainly:

    '...\limine.exe' was blocked by your organization's Device Guard policy.

and CodeIntegrity event 3033 (2026-10-02 19:13:15) gives the reason:

    Code Integrity determined that a process (...\cmd.exe) attempted to load
    ...\limine-tool-windows-x86\limine.exe that did not meet the Enterprise
    signing level requirements.

Event 3118 at the same timestamp is titled "Smart App Control Block Deteails"
[sic] — the same SAC family as the rust-lld case above.

**The retry procedure does not work here.** Five consecutive attempts all
returned 126. Where rust-lld's `os error 4551` is a transient loader verdict
that clears on a re-run, this is a signing-level policy verdict, and it is
sticky for an unchanged binary. Do not reach for `cargo-retry.sh`, and do not
extend it to cover this: a retry budget would only turn a hard failure into a
slow one.

Also do not expect the C-compiler fallback in `build-disk.sh` to rescue it.
A freshly compiled `limine.c` is unsigned as well, so it fails the same
signing-level requirement; and on this host there is no compiler at all.

Consequence: `build/nova.hdd` is UEFI-only, stage 5 reports the missing MBR
boot code, and every QEMU-gated stage (6, 8, 10, 11, 12, 13) times out at
`rc=124`. `build-disk.sh` does fail loudly and honestly — `set -euo pipefail`
aborts it, exit code 126, and it never prints "BIOS stages installed"
(verified: `grep -c 'BIOS stages installed' build/check-full.log` is 0).

Resolution requires either a signed `limine.exe` or a machine-owner policy
change. Neither is a repository change, so a persistent instance of this is a
hard build failure here, exactly as for rust-lld.

### Update, 2026-10-03: fresh diagnosis, then the block LIFTED itself

Full transcript: `docs/logs/k5-bios-install-restored.log`. Two material
corrections to everything above:

1. **The diagnosis is firmer than 2026-10-02's.** The CodeIntegrity log
   holds 11x event 3033 with 11x companion 3077 naming limine, spanning
   12:23:43 to 19:24:51 that day; the 3077 text adds the policy handle:
   "did not meet the Enterprise signing level requirements or violated code
   integrity policy (Policy ID: {0283ac0f-fff1-49ae-ada1-8a933130cad6})".
   `Get-MpComputerStatus` reports `SmartAppControlState: On`. An unrelated
   text file in the same directory runs fine (rc=0), so this is not a
   generic directory rule.
2. **"NOT transient" was wrong beyond the observed window.** The very next
   day, with no user action, the same pinned limine.exe executed
   successfully (build-disk smoke test: BD_RC=0, upstream's own "Limine BIOS
   stages installed successfully"). SAC verdicts are cloud-backed and
   change as reputation data accumulates — the same mechanism that makes
   rust-lld's block clear on retry. Five consecutive rc=126 on 2026-10-02
   were permanent for that window only. Retry is not a remedy within a
   window, but the verdict does move over hours.

**In-repo remedy now wired:** `tools/bios_install.py` is a faithful port of
the pinned upstream tool's `bios-install` for GPT images (the only shape
this repo builds), byte-parity-proven against the real exe: same inputs,
`cmp build/port-test.hdd build/nova.hdd` -> identical (sha256 d31dc9e9...).
`scripts/build-disk.sh` falls back to it ONLY on the exe's rc=126 (exec
refused — the tool rendered no verdict, so the port may speak); any other
exit code is a real verdict from the real tool and is propagated unchanged.
Port tests: `tests/test_bios_install.py`, 19 OK, including an executed
mutant caught by the parity predicate. While the block holds, BIOS-stage
installation therefore proceeds normally; the stage-2 reminder about
`limine-bios.sys` placement applies as upstream prints it.

**Persistent instance guidance is unchanged** for anything the port does
not cover: the UEFI loaders, `enroll-config`, and any future use of the
tool beyond `bios-install` still require the exe to run, which means a
signed binary, a policy change, or waiting out the verdict.

**Probing note (learned the hard way, 2026-10-03):** do NOT probe with
`bash <exe>` — bash then tries to interpret the PE binary as a shell
script and returns ENOEXEC / rc=126 ("cannot execute binary file") for
ANY binary, blocked or not. rc=126 is only evidence of a CodeIntegrity
block when it comes from a direct exec (as `build-disk.sh` invokes the
tool, and as yesterday's five pipeline failures did). Probe by direct
exec, and confirm in the CodeIntegrity event log.

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
- The `limine.exe` case (above) is verified to the level of the CodeIntegrity
event text and five consecutive failures. What is NOT verified is whether the
verdict is permanent or merely long-lived: it has been refused on every attempt
since the block first appeared, which is enough to rule out retry-in-a-loop but
not enough to say it can never clear. It was green on this same host at commit
`65e0270`, so something changed on the machine, and nothing in this repository
can observe what.
- Both cases are attributed to SAC because the event log says so (3118 "Smart
App Control Block Details"). No attempt was made to inspect the WDAC/AppLocker
policy itself, and no policy or exclusion was changed — that is a machine-owner
decision.
