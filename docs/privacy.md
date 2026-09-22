# NOVA — Privacy Model

The rule set the whole system is audited against. If a change violates any line below, the
change is wrong.

## Camera and microphone

1. Frames exist only in RAM, one at a time, and are **never written to disk** — not for
   caching, not for logs, not for "debugging."
2. What is persisted is text only: `{utc_timestamp, emotion, confidence}` in the journal.
   Confidence is a float, not a face. Face coordinates are not persisted.
3. Audio is captured only after the wake word (when the voice loop lands). Raw audio is never
   written to disk; transcripts are journal text under the same rules as emotion lines.
4. The camera has a logical shutter state; the physical kill switch (tape / hardware toggle)
   is documented and recommended in the README of any deployment.

## Storage

- The journal is plain text, owner-readable, owner-erasable. One file per day.
- No logs contain image or audio bytes. Ever. A test in `eyes/tests/` enforces the journal
  format so a regression fails loudly.
- All AI models live under `eyes/models/` and are downloaded once via pendrive-friendly
  scripts; their URLs are pinned by filename and verified by size.

## Network

- The AI core path performs no network calls at runtime. Zero. Dependency installation happens
  explicitly at setup time on a network-connected machine of the owner's choosing.
- Internet data reaches NOVA only through the Shuttle pendrive channel: packages must be
  ed25519-signed by a key the owner controls; unsigned packages are refused and logged.
- The pendrive letterbox (diagnostics, journal excerpts, queued requests) leaves the machine
  only when the owner physically plugs the stick in.

## Self-evolution (Metamorph, when it lands)

- Reflection may propose changes to prompts, skills, schedules, and configuration.
- It may not modify the kernel, the updater, the trust anchors, or these rules.
- Every proposal is sandboxed, replay-tested against recorded scenarios, and promoted with a
  git-verifiable rollback path.
