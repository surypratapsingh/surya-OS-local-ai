# NOVA — Work orders

The ordered task list. Read `AGENTS.md` first; it is the contract every task
below is executed under.

Work orders are handed to an engineer (human or agent) **one at a time**. The
prompt text in each block is meant to be pasted verbatim.

---

## The shape of the project

Two tracks, running in parallel, converging once.

```
Phase 0 — Foundations          blocking; nothing starts until these are done
    │
    ├── Phase A — Nucleus      the custom kernel. NOVA's specialty.
    │   K2 → K3 → K4 → K5 → K6 → K7
    │
    └── Phase B — mathd        the payload. std-only Rust, no kernel needed.
        B1 → B2 → … → B8

Phase C — Trust model          gates any module shipping
Phase D — Proof                gates any release, on either track
```

**Why both.** `mathd` is std-only Rust with no I/O, so it runs unchanged on
Windows today, on a minimal-Linux base tomorrow, and on Nucleus at K6. It is not
a detour from the kernel — it is the cargo the kernel exists to carry. Nucleus is
what makes NOVA *yours*; `mathd` is what makes it *worth booting*.

**The convergence proof is K6:** `mathd`'s full test suite passing, unchanged, on
Nucleus. That is the only milestone that validates both tracks at once.

### The custom kernel's promise

Nucleus is not an attempt to reimplement Linux. It is a purpose-built AI
appliance kernel, and it wins by supporting **fewer things deliberately** while
guaranteeing more than a general-purpose OS can:

- No hidden background services. No network stack.
- The owner's key gates every install and every capability.
- Camera and audio data travel an enforced RAM-only path.
- Memory, CPU, battery and thermal budget are governed AI-first.
- Modules run with declared permissions, never ambient access.

Scope discipline that makes this reachable: one or two known x86-64 machines plus
QEMU; no arbitrary Linux apps, browsers, printers or Wi-Fi; a minimal
capability-based userspace rather than a POSIX clone; signed packages and a
read-only storage path as *core kernel features*, not later add-ons; text and
console AI before camera, microphone and audio.

---

## Known defects (confirmed, with evidence)

These were found by review of the current tree. They are the subject of Phase 0.

| # | Defect | Location |
|---|---|---|
| 1 | **K1 does not boot.** Limine panics: `Failed to open executable with path /NUCLEUS`. `README.md` claims `✅ boots`. No log of a successful boot exists in the repo. | `build/qemu-serial.log:10` |
| 2 | **GPT `LastUsableLBA` overlaps the backup entry array.** `LastUsableLBA = 81920-34 = 81886`, but backup entries are placed at `81920-1-128 = 81791` (128 entries × 128 B is 32 sectors, not 128). Correct value is 81790. Also wastes 96 sectors to a units confusion. | `tools/make-esp.py:33`, `:248` |
| 3 | **The disk verifier never checks `LastUsableLBA`** — and shares its FAT/GPT model with the writer, so it cannot catch a shared misconception. | `tools/verify-disk.py:112` |
| 4 | **Font has 9 populated glyphs; boot report prints 95.** `build_glyphs()` fills only the chars in `REF_ROWS`. | `font.rs:92`, `main.rs:101` |
| 5 | **Font "verification" is a tautology** — compares the atlas to the array it was built from. Can never fail. | `font.rs:46` |
| 6 | **Cited file does not exist:** `tests/font_ref_test.py`, referenced twice including in a user-facing error string. Real data sits unused at `.freebuff/ref/font8x8_basic.h`. | `font.rs:66`, `main.rs:107` |
| 7 | **Every real-hardware boot ends in a panic.** The `SMOL` guard is always true, so `qemu_exit::success()` always runs; on hardware the port write is ignored and `unreachable!()` panics onto a fault screen. | `main.rs:129`, `qemu_exit.rs:14` |
| 8 | **Out-of-bounds framebuffer write.** `pixel()` writes a `u32` regardless of `bytes_per_pixel()`; at 24bpp the final pixel writes past the buffer. | `framebuffer.rs:96` |
| 9 | **UB reading `BASE_REVISION`** — immutable static written by the bootloader; LLVM may fold the read. The `UnsafeCell` pattern is already used correctly for response pointers. | `limine.rs:32`, `:280` |
| 10 | **Underflow panic waiting.** `text_width()` computes `chars * 9 - 1`; empty string underflows with `overflow-checks = true` in release. | `framebuffer.rs:150` |
| 11 | **Documented guarantee not implemented.** `linker_assert!` is described as checking requests stay within the marker range; it checks a struct size. | `architecture.md:34`, `limine.rs:119` |
| 12 | **Architecture doc describes code that does not exist.** Track B / NovaEyes pipeline is written in the present tense; there is no `eyes/` directory. | `architecture.md:36-59` |
| 13 | **Not a git repository.** No history, no tags, no bisect — and the E4/Metamorph design depends on git-versioned rollback. | repo root |

---

# Phase 0 — Foundations

Blocking. Nothing in Phase A or B starts until W0–W3 are complete.

## W0 · Repository and CI

> This directory is not a git repository. Initialise it.
>
> Write a `.gitignore` excluding `kernel/target/`, `build/*.hdd`, `build/*.iso`,
> and the extracted Limine sources under `.freebuff/ref/limine/` (keep the
> tarballs — they are the pinned reference).
>
> Add a LICENSE. If you are unsure which, stop and ask — do not guess.
>
> Make the first commit contain the current state **exactly as it is**, with an
> honest message recording that K1 does not currently boot and that the status
> tables in `README.md` and `docs/roadmap.md` are known to be wrong.
>
> Then add GitHub Actions CI running, on every push: `cargo build --release`,
> `cargo clippy -- -D warnings`, `cargo fmt --check`, and `bash scripts/check.sh`.
>
> **CI will be red. That is correct and expected.** Do not fix the boot in this
> task. Do not disable a check to make it green.

## W1 · The truth pass

> `build/qemu-serial.log` shows Limine failing with `Failed to open executable
> with path /NUCLEUS`, while `README.md` claims K1 `✅ boots`. Make the repo tell
> the truth. Work through these in order and report each one.
>
> 1. **Diagnose the boot failure.** Start with `boot(1):` in `kernel/limine.conf`
>    — compare against `uri_boot_dispatch` in
>    `.freebuff/ref/limine/limine-12.9.0/common/lib/uri.c`, which resolves the
>    partition index through `volume_get_by_coord`. Also check whether
>    `build/nova.hdd` predates `kernel/target/x86_64-unknown-none/release/nucleus`.
>    Fix it. Paste the complete, real output of `bash scripts/check.sh`.
>
> 2. **The font.** `kernel/src/font.rs` has 9 populated glyphs but `main.rs`
>    reports `GLYPH_COUNT` (95). `verify_against_reference()` compares the atlas
>    to the array that built it, so it can never fail. `tests/font_ref_test.py` is
>    cited twice and does not exist. The real data is unused at
>    `.freebuff/ref/font8x8_basic.h`. Report each of these in writing, then load
>    the real font and report the count of **populated** glyphs. If you keep a
>    reduced font, restrict the draw path to characters it actually contains.
>
> 3. **QEMU exit.** `qemu_exit::success()` is always reached; on real hardware the
>    port write is ignored, so `unreachable!()` panics and every hardware boot
>    ends on a fault screen. Put it behind a cargo feature, off by default.
>
> 4. **Framebuffer formats.** `framebuffer.rs::pixel()` writes a `u32` regardless
>    of `bytes_per_pixel()` — out of bounds on the final pixel at 24bpp. Either
>    handle 16/24/32 bpp correctly, or refuse to initialise on anything but 32bpp
>    with a clear serial message. Do not leave it silently wrong.
>
> 5. **`BASE_REVISION` is UB.** It is an immutable static that the bootloader
>    writes to; LLVM may fold the read. Use the same `UnsafeCell` pattern already
>    used correctly for the response pointers in the same file.
>
> 6. **Underflow.** `framebuffer.rs::text_width()` computes `chars * 9 - 1`, which
>    underflows on an empty string with `overflow-checks = true` in release.
>
> 7. **`linker_assert!`** is documented in `docs/architecture.md:34` as checking
>    that requests stay within the marker range. It checks a struct size. Either
>    implement the documented guarantee or correct the documentation.
>
> 8. **`docs/architecture.md:36-59`** describes the NovaEyes pipeline in the
>    present tense. No `eyes/` directory exists. Mark it clearly as a design for
>    unwritten code.
>
> 9. Update `README.md` and `docs/roadmap.md` so **no milestone is marked ✅
>    unless a committed log shows it passing**. Commit the log.

## W2 · An independent disk verifier

> `tools/verify-disk.py` shares its model of FAT16 and GPT with
> `tools/make-esp.py`, so it cannot catch a misconception the two have in common.
> Here is a confirmed instance: the primary GPT header sets
> `LastUsableLBA = IMAGE_SECTORS - 34 = 81886`, while the backup entry array is
> placed at `IMAGE_SECTORS - 1 - 128 = 81791`. 128 entries of 128 bytes is 32
> sectors, not 128 — so `LastUsableLBA` falls inside the backup entry array, and
> `verify-disk.py` never validates that field at all.
>
> 1. Fix the placement and the constant in `make-esp.py`. **Cite the UEFI
>    specification section** for the rule you apply, in a comment.
>
> 2. Add external-tool verification to `scripts/check.sh`: `sgdisk --verify`,
>    `fsck.fat -n`, and a `mdir` directory listing compared against expectations.
>    These tools are the oracle — they did not come from us. If a tool is absent,
>    print a loud `SKIP` naming it. **Never silently pass a skipped check.**
>
> 3. Rewrite the GPT section of `verify-disk.py` **from the specification**, with
>    the section number cited per check. It must include, at minimum:
>    `LastUsableLBA >= ` the end of every declared partition, and
>    `LastUsableLBA < ` the backup entries LBA.
>
> 4. Add a fuzz test: mutate random bytes of `nova.hdd` and assert the verifier
>    reports a failure rather than crashing or passing. 10,000 iterations. Report
>    the count of crashes separately from the count of false passes — a false pass
>    is a serious finding.

## W3 · Hardware matrix

> Boot `build/nova.hdd` on: QEMU/SeaBIOS, QEMU/OVMF, and at least two real
> machines from a USB stick.
>
> Record in `docs/hardware-matrix.md`: machine model, firmware mode (BIOS or
> UEFI), Secure Boot state, result, and a serial capture or photograph as
> evidence. Commit the evidence.
>
> **M0 is not ✅ until this table exists and every row passes.** A row you could
> not test is recorded as untested, not omitted.

---

# Phase A — Nucleus (the specialty)

The kernel ladder. Each rung is a work order; write the prompt from the "done
when" column, and apply `AGENTS.md` rules as always.

| Rung | Scope | Done when |
|---|---|---|
| **K2** | GDT, IDT, all CPU fault handlers, PIC/APIC timer, PS/2 keyboard (scancode set 1), serial + screen console, small diagnostic shell | **Every one of the 32 exception vectors is deliberately triggered by a test and reports correctly.** A fault handler nobody has fired is not implemented, it is decoration. |
| **K3** | 4-level paging, physical frame allocator from the Limine memory map, kernel heap, **stack guard pages**, read-only FAT32, CMOS RTC | The FAT32 driver reads a real pendrive image **byte-identically to `mdir`** across a generated corpus of directory layouts. A deliberate stack overflow faults on the guard page rather than corrupting memory. |
| **K4** | ELF64 loader, processes, **capability table**, scheduler (cooperative then APIC-preemptive), `novad` init, **in-kernel package verifier** | A process cannot touch a resource absent from its capability set — proven by a test that attempts it and is denied. Capabilities and the package verifier land **here**, not later: a permission system retrofitted after userspace exists is a permission system with holes. |
| **K5** | xHCI, USB mass storage, display, Intel HDA audio | Reads the same pendrive the firmware does, verified against `mdir`. Audio round-trip test passes. |
| **K6** | Port `mathd` and llama.cpp onto the Nucleus libc shim and VFS | **`mathd`'s full test suite passes unchanged on Nucleus.** This is the convergence proof and the point of the whole project. |
| **K7** | UVC camera, voice input and output | The Phase D privacy tests pass **on Nucleus**, not only on a Linux base. |

---

# Phase B — `mathd` (the payload)

Runs in parallel with Phase A. Needs no kernel, no bootloader, no drivers.
std-only Rust. Create the crate at `mathd/`.

**The oracle rule, concretely.** Each work order names a checker that did not come
from the code under test:

| Component | Oracle | Why it cannot be faked |
|---|---|---|
| Evaluator | SymPy + mpmath fixtures | Mature external implementation, committed as data, inspectable |
| Differentiator | Central finite differences | Entirely different algorithm and code path |
| Verifier | Mutation testing | `return true` fails instantly |

## B1 · Parser and AST

> Create `mathd/` — std-only Rust, no dependencies, no kernel code.
>
> Implement an expression parser producing an AST covering: integers, rationals,
> decimals, named constants (`pi`, `e`), variables, `+ - * / ^`, unary minus,
> parentheses, and the functions `sin cos tan exp ln sqrt abs`. Accept plain text
> (`x^2 + 3*x`) and common unicode forms (`·`, `√`, `π`). Standard precedence;
> `^` is right-associative.
>
> **Done when** a round-trip property test passes over at least 10,000 generated
> expressions: `parse(print(parse(s)))` produces a structurally identical AST to
> `parse(s)`. Write the generator to build random well-formed ASTs, print them,
> and re-parse. Include a hand-written corpus of at least 40 malformed inputs,
> each of which must produce a specific error — never a panic.
>
> `cargo test` passes with zero ignored tests. Paste the output.

## B2 · Canonicaliser and structural hash

> Add canonicalisation: sort commutative operands into a total order, normalise
> unary minus, fold integer constants, flatten nested associative operations, and
> normalise `a/b` and `a*b^-1` to a single form. Then
> `structural_hash(&Ast) -> u64`.
>
> **Done when** two property tests pass, each over at least 10,000 generated cases:
>
> - **Agreement** — expressions generated to be equivalent by construction
>   (commuted operands, reassociated terms, `x+0`, `x*1`, double negation) hash
>   identically. Zero exceptions permitted.
> - **Separation** — expressions generated to be structurally different hash
>   differently. **Report the collision count.** More than 1 in 10,000 means the
>   hash or the canonical form is wrong.
>
> Do not tune either test to pass. If separation fails, report it and stop.

## B3 · Numeric evaluator

> Add `eval_at(&Ast, &HashMap<String, f64>) -> Result<f64, EvalError>`. Domain
> errors (`ln` of a negative, division by zero, `sqrt` of a negative) are errors —
> never NaN propagation, never a panic.
>
> **The oracle is not yours.** Write `mathd/fixtures/generate.py` using **SymPy
> and mpmath** to produce `mathd/fixtures/eval.json`: at least 2,000
> `(expression, bindings, expected)` triples at 30 digits of precision, covering
> every supported function plus edge cases near domain boundaries. Commit both the
> script and the JSON. The Rust test reads the JSON and asserts agreement within
> 1e-10 relative error.
>
> You may not generate expected values with your own evaluator, and you may not
> hand-write them. If SymPy is unavailable here, stop and say so — do not
> substitute something else.

## B4 · Differentiator

> Add `differentiate(&Ast, var: &str) -> Ast`: sum, product, quotient, chain and
> power rules, plus derivatives of every supported function.
>
> **The oracle is central finite differences** — a completely different algorithm
> from symbolic differentiation. For at least 10,000 generated expressions, at 5
> random points each, assert:
>
> ```
> | eval(differentiate(f), x) − (eval(f, x+h) − eval(f, x−h)) / 2h |  <  tol
> ```
>
> with `h = 1e-5` and a tolerance accounting for O(h²) truncation error. Skip
> points where `f` is undefined or the second derivative is very large — and
> **report the skip count**, because a high skip rate is how a broken
> differentiator hides.
>
> Additionally cross-check against the SymPy fixtures from B3, extended with
> derivative cases.

## B5 · The verifier

> This is the component the entire project rests on.
>
> ```rust
> verify_integral(integrand, claimed_antiderivative, var) -> Verdict
> verify_equation_solution(equation, claimed_roots, var)  -> Verdict
> verify_identity(lhs, rhs, vars)                          -> Verdict
> verify_factorisation(original, claimed_factors)          -> Verdict
>
> enum Verdict {
>     Verified   { evidence: Evidence },
>     Failed     { counterexample: Point, expected: f64, got: f64 },
>     Unverifiable { reason: String },
> }
> ```
>
> Method: differentiate or substitute as appropriate, then confirm numerically at
> **at least 8 random points** drawn from a sensible domain. `Failed` always
> carries the specific point and both values. `Unverifiable` is a first-class
> outcome — never guess when you cannot check.
>
> **Done when mutation testing passes.** Take a corpus of at least 300 correct
> `(problem, answer)` pairs. For each, generate mutations: flip a sign, change a
> coefficient by 1, swap operands of a non-commutative operator, drop a term,
> substitute a sibling function (`sin`→`cos`). The verifier must **reject 100% of
> mutations that are genuinely not equivalent** and **accept 100% of originals**.
> Report the full confusion matrix.
>
> **A false `Verified` is a project-stopping bug.** Report it; do not work around
> it.

## B6 · The adversarial corpus

> Build `mathd/corpus/`: at least 500 problems spanning a Class 11–12 maths
> syllabus, each with a correct answer **and** at least one plausible wrong answer
> of the kind a language model actually produces — sign error after integration by
> parts, dropped constant of integration, omitted chain rule, ignored domain.
>
> Source problems from textbooks in the repo if present; otherwise generate them
> and mark them clearly as generated. **Every correct answer must be
> independently confirmed by SymPy before entering the corpus**, with the
> confirming call recorded alongside it.
>
> **Done when** the B5 verifier scores 100% on both arms and you have pasted the
> full report. If it scores less, that is the finding — report it, do not tune.

## B7 · Prove-it at the command line

> Build `mathd-cli`: terminal only. No GUI, no model, no network.
>
> **Prove-it is the default mode.** It presents a problem from the corpus, takes
> the owner's answer, verifies it, and on failure shows the counterexample point
> and both values. Getting the machine to reveal the answer requires a separate,
> explicit command — the posture is "show me your working", not "here is the
> answer".
>
> Log every attempt to a local SQLite file: problem id, answer given, verdict,
> timestamp, time taken. That is the mistake ledger.
>
> **Done when** the owner can sit down, do twenty problems, and the tool has never
> once told them something false.

## B8 · The card store

> Add the cache, keyed by the B2 structural hash.
>
> **Hard invariant, enforced by the type system:** a card cannot be constructed
> without a `Verdict::Verified` and its evidence. Make an unverified insert
> impossible to write, not merely discouraged — no public constructor, no
> `Default`, no deserialisation path that bypasses it.
>
> Add promotion: after N cards share a structural shape, emit a candidate
> **method** card. Method cards are proposals until a human confirms them; they
> never auto-promote.
>
> **Done when** a test demonstrates no code path in the crate can insert an
> unverified card, and a benchmark shows cache-hit lookup under 1 ms over a corpus
> of 100,000 cards.

---

# Phase C — The trust model

Gates any module shipping, on either track.

- **C1 · Root key.** Owner-generated, created offline, never present on the NOVA
  machine. Document the ceremony.
- **C2 · Signed manifests.** Payload hashes, monotonic version counters with
  replay protection, and explicit capability declarations per package.
- **C3 · Atomic install.** A/B slots, atomic pointer swap, rollback. A failed or
  interrupted install always leaves a bootable machine.
- **C4 · Extend signing backwards.** Cover Limine, the kernel and the build
  inputs — not only future modules. A trust chain that starts halfway up is not a
  trust chain.
- **C5 · Reproducible builds.** Identical inputs produce a byte-identical image,
  proven in CI.
- **C6 · Secure Boot with an owner-controlled certificate** — only if the threat
  model calls for physical tamper resistance. Decide from C-D5, not by default.

---

# Phase D — Proof

Release gates, not a milestone. A release failing any row does not ship at a
lower grade; it does not ship.

- **D1 · Privacy, camera and audio.** Run the full pipeline, then scan the entire
  filesystem for the known frame and sample patterns. Zero hits required.
- **D2 · Network.** Assert zero sockets opened during normal operation.
- **D3 · Fuzz.** FAT, GPT and package parsing. Crashes and false passes reported
  separately.
- **D4 · Recovery.** Corrupted package; USB removed mid-install; power loss during
  install; failed model load. Each must leave a bootable machine.
- **D5 · Threat model.** Written, explicit about what NOVA does and does not
  defend against.
- **D6 · Independent review.** External code and security review before the word
  "secure" appears in any document.

---

# The scorecard, as gates

| Area | Gate |
|---|---|
| Boot reliability | Reproducible image; `docs/hardware-matrix.md` complete with real captures |
| Correctness | CI green; mutation tests pass; **no test compares a value to its own source** |
| Security | Signed chain from bootloader upward; rollback proven; external review complete |
| Privacy | Filesystem scan finds no frames or samples; socket count zero |
| Product | Twenty problems answered end to end, zero falsehoods |
| Maintainability | Git history, tags, CI, and docs that match the code |
| AI | Small local model completes bounded skills; every claim passes `mathd` |

---

# Reviewer's checklist

Run this on every delivery. Two minutes, and it would have caught every defect in
the table at the top of this file.

1. **Did it paste real command output, or describe it?** Descriptions are
   rejected. Real output has warnings, timestamps and ugly bits.
2. **Read one test closely.** What is on the left, what is on the right, where did
   each come from? If both trace to the same code, the test is void.
3. **Break something on purpose.** Flip a sign in the differentiator, change a
   byte in the image, re-run the suite. If it still passes, the suite is
   decorative. Do this at least once per work order — it is the single
   highest-value check available.
4. **Every path cited in a comment — does it exist?** One listing settles it.
5. **Does every displayed count match its data?** "95 glyphs" was nine.
6. **Read "what I could not do" and "what I am unsure of" first.** Three empty
   deliveries in a row means things are being hidden.
