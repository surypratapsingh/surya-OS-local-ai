# NovaCore decisions

## D1 — Build a runtime, not fictional model weights

**Decision:** NovaCore accepts an owner-provided local model runner rather than
attempting to train or bundle a claimed 100M/1B-parameter model.

**Why:** Training, licensing, evaluation, quantisation, and weight distribution
cannot be truthfully completed by scaffolding code. A runtime still provides a
real integration point for any suitable local model, and can be tested without
claiming intelligence that is not present.

**Consequence:** A configured runner is required for replies. The unconfigured
state fails visibly.

## D2 — Hardware is capability-mediated, never model-mediated

**Decision:** The model cannot receive a shell, raw I/O ports, unrestricted file
access, or OS-update execution. It may only be connected later through a fixed
capability catalogue.

**Why:** A talking model is untrusted input-processing software. Its text must
not become authority, especially when imported content can contain adversarial
instructions.

**Consequence:** This first version writes labelled `STUB` requests to an outbox
instead of controlling physical hardware.

## D3 — Updates begin as unverified proposals

**Decision:** NovaCore can record an update proposal but cannot apply it.

**Why:** The repository's Phase C trust model requires owner keys, signed
manifests, atomic install/rollback, and reproducible builds. None exists here
yet. Pretending otherwise would make an unsafe self-modifying system.

**Consequence:** Every proposal is marked `proposed_unverified` and contains a
test and rollback plan for human review.

## D4 — Persist memories only on explicit owner request

**Decision:** Conversation remains in memory for the current process. Long-term
memory is appended only by `/remember`.

**Why:** This is the simplest enforceable interpretation of owner control and
avoids silently turning sensitive conversation into permanent data.

**Consequence:** The model is never asked to decide what personal information to
store.
