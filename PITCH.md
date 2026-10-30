# NOVA: an AI computer that works for one person only

**The idea in one line:** your own AI runs entirely on your own machine. It never goes
online. New knowledge arrives on a signed USB stick that you carry in yourself.

Today's AI assistants live on someone else's servers. They see your data, they can change
under you, and they stop working when the internet does. NOVA flips that. The machine, the
models and the data all belong to you, and nothing phones home.

---

## A day with NOVA

You want to learn maths. You copy PDFs, lecture videos and photos of your notes onto the
stick and plug it in. Overnight, NOVA reads and indexes all of it, on your machine.

Next morning you say *"Study chapter 3."* It teaches the chapter, draws the graphs, quizzes
you, and **checks every answer before trusting it**. It remembers how you solved each
problem, so a similar problem next month replays your own method.

When it doesn't know something, it does not go online. It writes a **shopping list** of
exactly what to fetch. You fill the list on any internet computer, carry it back on the
stick, and NOVA checks the files are genuine before it installs them.

---

## Key features

### 1. Offline by design
- No network features, no telemetry, and no way to send it commands from outside.
- **Proven, not just promised:** a test watches the Python runtime and fails if any
  network socket opens during a normal AI session. Faults we planted on purpose (hidden
  network calls) are all caught. *(Status: done.)*

### 2. Camera and microphone stay in memory
- Raw camera frames and audio never touch the disk. The only thing saved is a line of
  text: `{time, label, confidence}`.
- *(Status: a project rule that every change is reviewed against. The camera and voice
  pipeline itself is still to come.)*

### 3. Updates only from a signed USB stick
- Every package is signed (Ed25519) and every file is hash-checked before anything is
  applied. Old or replayed packages are rejected.
- Each package states exactly what it is allowed to touch. It gets that and nothing else.
- **Installs can't half-finish.** A test stops the installer at each of its 15 steps,
  and the machine still boots every time. *(Status: done, and checked in CI on every change.)*

### 4. A maths engine that proves its answers
- `mathd` parses, simplifies, evaluates, differentiates and **verifies** maths. An answer
  is only stored once the verifier says "Verified" and keeps its evidence.
- Tested against SymPy, an independent maths library. A 551-problem test set drawn from
  six school and early-university topics, with 746 realistic wrong answers (a dropped chain
  rule factor, a sign slip, an extraneous root), is all handled correctly.
- That test set also found a real bug: the verifier accepted `ln(x^2) = 2*ln(x)`, which is
  only true for x > 0. The fix is being built now. *(Status: parser to differentiator done;
  verifier being fixed; study mode and the answer store are next.)*

### 5. Its own operating system, written from scratch
- **Nucleus** is a new x86-64 kernel in Rust. It boots, has its own memory management,
  reads FAT32 files, keeps time, and gives you a `nova>` shell.
- One disk image boots both old (BIOS) and new (UEFI) PCs, at least in the emulator.
- Recent: a built-in signature checker and per-process page tables. *(Status:
  boots and runs in the QEMU emulator; real hardware not tested yet; programs don't yet
  run in a separate protected mode, so they are not isolated from the kernel.)*

### 6. Small OS, swappable brains
- The OS code stays tiny. The intelligence (models, indexes, skills) is data you swap in
  like cartridges. If a task needs a bigger model, NOVA tells you which one to bring.

---

## Why you can trust the claims

Most projects say "tests pass". This one is built around catching the cases where tests
pass for the wrong reason:

- **Independent checkers.** Every result is checked by something that didn't produce it:
  SymPy for maths, real disk tools (`sgdisk`, `fsck.fat`, `mdir`) for the disk image, and
  real firmware in an emulator for booting.
- **We break it on purpose.** Reviewers plant bugs (a flipped sign, a loosened tolerance,
  a hidden network call) and re-run the tests. If the tests still pass, the work is
  rejected.
- **No claim without proof.** "Done" requires the exact command, its real output, and a
  green CI run, all saved in the repo.
- **Several AI agents build it, and they review each other.** One lead reviews and
  accepts everything. Work has been rejected and redone several times this month.

---

## Where it stands (honestly)

| Works today | In progress | Planned |
|---|---|---|
| Kernel boots in QEMU (BIOS + UEFI), with a shell | Programs running in their own protected mode | Running on real PCs from a USB stick |
| Signed packages, crash-proof installs | Builds that produce the exact same bytes every time | Voice, camera and an emotion-aware journal |
| Maths engine (4 of 5 stages) and a 551-problem test set | Verifier fix for domain bugs; "Prove it" study mode | The shopping-list module manager |
| Zero network sockets, proven by a test | Threat model (waiting on owner decisions) | Replacing the temporary Linux base with Nucleus |

**Not claimed yet:** the word "secure". An outside review comes first.

---

## The ask

Have a look, try booting it in QEMU, and tell me what you would want your own offline AI
to do. Repo quick start: `bash scripts/build-disk.sh release`, then `bash scripts/check.sh`.
