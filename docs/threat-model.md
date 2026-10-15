# NOVA threat model (D5)

**Status: DRAFT, 2026-10-16, by claude. Not reviewed.** Reviewer: antigravity
(`docs/team.md`). Four questions in the last section belong to the owner, and
the model is not final until the owner answers them. This document makes no
claim that NOVA is safe to rely on. Under D6 (`docs/work-orders.md`, Phase D),
the word "secure" may not describe NOVA until an external review is done.
"Secure Boot" below is the name of a UEFI feature, not a claim.

How to read the tables. Each row names one threat and gives one status:

- **Defended (evidence)**: a committed log shows a planted fault being caught.
  The log is named.
- **Designed, not proven**: code or a design exists, but no committed log yet
  shows a planted fault being caught. The open work order is named.
- **Not defended**: nothing in the tree addresses it. Out of scope until the
  owner says otherwise.

A row moves up only when its log is committed. If a row and a log disagree,
the log is right (AGENTS.md rule 15).

---

## 1. What NOVA protects

| Asset | Where it lives today |
|---|---|
| The owner's conversations and approved notes | `novacore` state folder on the host OS: `memories.jsonl`, `events.jsonl` and their `.anchor` files (`novacore/SCOPE.md`) |
| Camera frames and audio samples | Nowhere yet. No camera or audio code exists (`docs/architecture.md`: "No `eyes/` directory exists in the tree") |
| What runs at boot | Limine v12.9.0 and the Nucleus kernel on the ESP (`docs/bootchain-signing-design.md`) |
| What gets installed | Release packages on a pendrive, signed by the owner's Ed25519 root key (`docs/manifest-format.md`) |
| The owner's authority | No remote instruction path; the model proposes, a broker decides (`novacore/SCOPE.md`, "Non-negotiable boundary") |

## 2. What this model trusts (the trusted base)

Every row below is assumed, not checked. If one of these fails, the tables in
section 3 do not hold.

1. **The CPU and the firmware.** Nothing in NOVA checks them.
2. **The host OS, until K6.** Today `novacore` runs as a Python program on the
   owner's Windows or Linux account, and the trust tools run on a host OS too.
   That OS has a network stack, other programs and other accounts. Until
   Nucleus runs the AI layer (K6), NOVA is only as strong as that OS account.
3. **The offline machine** that holds the root key (`docs/root-key-ceremony.md`).
4. **The toolchain:** rustc, Limine v12.9.0, Python's `hashlib`. Pinned, not
   reproduced (C5 is open).
5. **The local model runner and its weights.** The owner chooses them.
   `novacore` checks no hash for the model file: the only SHA-256 uses in
   `novacore/src/novacore/` are the log chain (`chainlog.py`) and note
   content (`memory.py`).
6. **The agents that write NOVA's code.** Three AI agents write and
   cross-review this repo. Their reviews break code on purpose (AGENTS.md,
   "How your work will be reviewed"), but no human has reviewed the code yet
   (D6 is open).

## 3. Threats

### 3.1 Remote attackers

| # | Threat | Status | Evidence or open work |
|---|---|---|---|
| T1 | An attacker on a network sends NOVA commands or reads its data | **Kernel: by construction. AI layer: designed, not proven.** | Nucleus has no network driver: the module list in `kernel/src/` has no NIC, socket or TCP code. `novacore` makes no network calls by design (`docs/privacy.md`, "Network"), but no test asserts zero sockets yet (D2 is open). The host OS under `novacore` has a full network stack, which NOVA does not control (section 2, item 2). |
| T2 | A remote party pushes an update | **Designed, not proven** | There is no update path except the pendrive. No code listens for one. Nothing tests this as a negative claim; D2 would cover it. |

### 3.2 The pendrive channel

| # | Threat | Status | Evidence or open work |
|---|---|---|---|
| T3 | A package on the pendrive was changed after signing | **Defended (evidence), with the real key missing** | `docs/logs/c1c2-trust-mutations.log`: `mutants: 11, killed: 11`. The 24 trust tests run in CI (`docs/logs/ci-37944150122-full.log` line 99, `Ran 24 tests`). **But the owner's root key does not exist yet** (`kernel/trust/root-key.pub` is absent; C1 is open), so no real release is signed today. |
| T4 | An old signed release is replayed (rollback) | **Defended against replay; not against someone who can write the state file** | `release_sequence` must rise; equal or lower is refused, and a missing state file is an error (`docs/manifest-format.md`, "Replay and rollback"; mutant "replay check lets the same release_sequence in twice" killed in the log above). Anyone who can write the state file can reset it (same section, "Limitation"). |
| T5 | A malformed FAT32 stick attacks the kernel's FAT32 reader | **Designed, not proven** | `kernel/src/fat.rs` passes its selftest on a corpus built as a real FAT32 volume, and a planted lookup fault fails it (R2: `docs/logs/r2-step3-kernel.log` line 26 `NOVA_SELFTEST_OK`, line 97 `NOVA_SELFTEST_FAILED: 1 of 16 fat-gate checks failed`). That corpus is well formed. The kernel's parser is not fuzzed: the W2 fuzzer (`tools/fuzz-disk.py`) tests host tools, not `fat.rs`. D3 is open. |
| T6 | The package verifier itself has a bug | **Partly defended** | The Ed25519 code is checked byte for byte against OpenSSL (tests named in `docs/logs/c1c2-trust-mutations.log`). The verifier runs in Python on a host OS (`tools/verify-manifest.py`); the kernel has no signature code (`grep -ril "ed25519\|signature" kernel/src` finds nothing). |

### 3.3 The model and the text it reads

| # | Threat | Status | Evidence or open work |
|---|---|---|---|
| T7 | A prompt injection, in conversation or in an imported note, makes the model act | **Designed, not proven** | The model only proposes words; `CapabilityBroker` checks a named request and emits an event; no executor exists (`novacore/SCOPE.md`). The W4 harness catches 4 of 4 probes (`docs/logs/r10-w4-after-fixes.log` line 130), but W4 is not ticked: the prompt check misses a three-part name such as `system.reboot.force` (`docs/reviews/r10-claude.log`, VERDICT: FAIL (F5)). Actions named in prose, not as dotted names, are a documented limit of that check. |
| T8 | A kernel module or process uses a resource it was not granted | **Designed, partly proven** | `kernel/src/cap.rs` is deny by default and has no wildcard: a resource is an enum arm, not a string. Planted faults are caught (`docs/logs/k4a-cap-mutations.log`). K4 is not complete: the ELF loader does not create address spaces or enter ring 3 yet (`kernel/src/elf.rs` lines 5-8), so nothing is isolated by the hardware. |
| T9 | A runaway model floods memory or the screen | **Defended for stdout; not for stderr** | Output is read in 1 KB chunks and capped; peak 34.11 KB on 50 MB of output (`docs/logs/r11-memory-bounding.log`). But a runner that writes more than about 5 KB to stderr fails with exit 120 (`docs/reviews/r11-claude.log`, F1, open). |

### 3.4 Owner content on the disk

| # | Threat | Status | Evidence or open work |
|---|---|---|---|
| T10 | Conversation text is left on the disk in temporary files | **Designed, not proven** | The prompt goes on stdin by default. The W5 harness finds 0 hits outside the memory store in 6 runs (`docs/logs/r11-w5-evidence.log` line 73). W5 is not ticked: the 0o600 and O_EXCL rules have no test (`docs/reviews/r11-claude.log`, F2). On Windows no ACL is set, and on any disk, unlink is not erasure (`novacore/SCOPE.md`, "Privacy and data handling"). |
| T11 | A camera frame or audio sample reaches the disk | **Nothing to defend yet** | No camera or audio code exists. D1 (scan the whole disk for planted frame and sample patterns) must pass before any such code ships. |
| T12 | Someone edits the memory log or the capability log | **Defended against accidents and partial copies; not against anyone who can write the folder** | W6: `docs/logs/r12-w6-after.log` line 13, `W6 memory log: 7 of 7 mutations detected`. The chain has no key, so whoever can write the folder can rewrite the log and its anchor (`novacore/SCOPE.md`, "Log integrity"). |

### 3.5 Physical access

| # | Threat | Status | Evidence or open work |
|---|---|---|---|
| T13 | The machine or its disk is stolen and read | **Not defended** | Notes and logs are plain text. No work order plans disk encryption (`grep -i encrypt` finds nothing in `docs/work-orders.md` or `plan.md`). Owner question Q1. |
| T14 | Someone with a few minutes alone with the machine swaps the kernel ("evil maid") | **Not defended** | At boot, Limine checks the kernel against a BLAKE2b hash written in `limine.conf` (`docs/bootchain-signing-design.md`). `limine.conf` itself is not protected: nothing in `scripts/` or `tools/` runs `limine enroll-config`, so whoever can write the ESP can change the kernel and its hash together. This catches a corrupted file, not an attacker. Closing it needs C6 (Secure Boot with the owner's certificate) or config enrolment. Owner question Q1. |
| T15 | Firmware or hardware is modified | **Not defended, and C6 would not change that** | `docs/secure-boot-design.md`, "Threat Model": C6 does not stop an attacker who can reset the BIOS or replace the firmware chip. |

### 3.6 Keys, builds and people

| # | Threat | Status | Evidence or open work |
|---|---|---|---|
| T16 | The root private key is stolen | **Not defended after the theft; prevention is procedural** | The key is made and kept offline, with the fingerprint on paper (`docs/root-key-ceremony.md`). There is no revocation. Rotation means committing a new public key, after which old manifests stop verifying (`docs/manifest-format.md`, "Not covered by this version"). Owner question Q3. |
| T17 | The build machine or toolchain inserts something into the image | **Not defended** | C5 (reproducible builds) is not started: no stage in `scripts/check.sh` or `.github/workflows/` rebuilds and compares. Without C5, a signed hash proves only "this is what the build machine produced". |
| T18 | Generated code contains a plausible but wrong check | **Partly defended** | Every work order is reviewed by a second agent, who breaks the code on purpose (`docs/team.md`, "Cross-checking"). This process has caught real faults: B3, R10 and R11 each failed their first review (`docs/reviews/`). No human has reviewed the code yet (D6 is open). |
| T19 | Another account on the same host OS reads the state folder | **Not defended on Windows; untested on POSIX** | `novacore/SCOPE.md`: no ACLs on Windows; 0o600/0o700 on POSIX, with no test yet (R11 F2). |
| T20 | The owner signs a bad release by mistake | **Not defended** | `docs/root-key-ceremony.md`, "It does not protect against": "the owner signing something bad". |

## 4. Out of scope, on purpose

- Side channels (timing, power, electromagnetic). The Ed25519 code in
  `tools/nova_trust.py` is pure Python and not constant-time. It signs only on
  the offline machine, so a timing attack needs someone already on that
  machine.
- Denial of service by someone with physical access (pulling the power or the
  disk). D4 (recovery) covers keeping the machine bootable; it does not cover
  keeping it available.
- Attacks on the owner (coercion, theft of the paper fingerprint).

## 5. Questions for the owner

These decide which "Not defended" rows become work orders. The lead does not
decide them.

- **Q1. Is a person with physical access in scope?** If yes: C6 (Secure Boot
  with the owner's certificate) or Limine config enrolment for T14, and a new
  work order for disk encryption for T13. If no: T13 and T14 stay "Not
  defended", and C6 can be closed as not needed (`docs/work-orders.md`, C6:
  "Decide from C-D5, not by default").
- **Q2. Does anyone else use this machine or its host OS account?** If yes,
  T19 must be closed before notes go into `novacore` (Windows ACLs, a POSIX
  mode test).
- **Q3. What happens if the root key is lost or stolen?** Today the only
  answer is a new key and a new `kernel/trust/root-key.pub`. A revocation
  plan, such as a second offline backup key that can sign a "replace the root
  key" manifest, is a design choice for the owner.
- **Q4. Should model weights be signed?** Today the owner's model file is
  trusted as is (section 2, item 5). Putting the model's SHA-256 in the
  signed manifest would close that, at the cost of a signing step per model.

## 6. What would change this document

A row changes status only with a committed log. When D2, D3, C5, R10 (F5) or
R11 (F1, F2) close, update the matching row and cite the log. When the owner
answers a question in section 5, record it in `decisions.md` and update the
rows it names.
