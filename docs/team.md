# NOVA team board

Three agents work in this one folder at the same time. This board says who does
what, which files each agent may write, and who checks whose work. Read
`AGENTS.md` first, then this file, before every task.

The lead (Claude Code) owns this board. Other agents edit only two things here:
the **Status** cell of their own claim, and the **Messages** section.

## Agents

| Name | Tool | Track |
|---|---|---|
| `claude` | Claude Code | Lead. CI and `scripts/check.sh`, the evidence harness `tools/novacore-evidence.py`, novacore W6 (R12). Final ticks. |
| `antigravity` | Google Antigravity | `mathd` (B3, then B4). |
| `freebuff` | Freebuff | novacore W4, then W5 (R10, then R11). |

## Claims

You may write only the files in your own row. A file is in at most one row.
"Queued" rows hold no files yet.

| Work order | Agent | Files this agent may write | Reviewer | Status |
|---|---|---|---|---|
| R8 | claude | `scripts/check.sh`, `tools/novacore-evidence.py`, `novacore/tests/test_llm_privacy.py` (scanner only) | antigravity | Author done: CI run 38289059795 green (`docs/logs/ci-38289059795-r8.log`). Review pending. |
| B3 | antigravity | everything under `mathd/`, and the stray `eval.json` in the repo root (B3 wants it at `mathd/fixtures/eval.json`) | freebuff | Committed, awaiting review |
| R10 | freebuff | `novacore/src/novacore/prompts.py`, `novacore/src/novacore/capabilities.py`, `novacore/tests/test_prompts.py`, `novacore/tests/test_capabilities.py` | claude | Ready |
| R12 | claude | `novacore/src/novacore/memory.py`, `novacore/tests/test_memory.py`, `novacore/SCOPE.md`. The outbox part needs `capabilities.py` and waits until R10 is done. | antigravity | Queued after R8 |
| R11 | freebuff | `novacore/src/novacore/llm.py`, `novacore/src/novacore/config.py`, `novacore/config.example.toml`, `novacore/tests/test_llm_privacy.py` | claude | Queued after R10 and R8 |
| B4 | antigravity | everything under `mathd/` | freebuff | Queued after B3 |

**Waiting on the owner, not on an agent:** R4 (real hardware), R6 (stage 14
timeout), R9 (W1 item 3), C1 (root key). **Unassigned:** the kernel track
(`kernel/`). An agent that runs out of work asks the lead in Messages; it does
not pick up work on its own.

`tools/novacore-evidence.py` is the oracle for R10, R11 and R12. Only the lead
edits it. If you think it is wrong, say so in Messages (AGENTS.md rule 1: the
author of a fix does not also write the check that grades it).

## Rules for one shared folder

1. **Write only your claimed files.** Need another file? Ask in Messages and wait.
   Found a bug in another agent's file? Report it in Messages; do not fix it.
2. **Never run these on a file you do not hold:** `git checkout`, `git restore`,
   `git stash`, `git reset`, `git clean`, or a formatter over the whole tree. On
   2026-10-12 an agent's uncommitted fix was silently undone this way.
3. **Commit only your own paths:** `git commit -m "<message>" -- <path> <path>`.
   The `--` form commits the named paths only, even if another agent has staged
   something. Never `git add -A` or `git add .`. Never amend, rebase or force-push.
4. **Sign every commit.** End the message with one line naming you:
   `Agent: claude`, `Agent: antigravity` or `Agent: freebuff`.
5. **Shared files** (`progress.md`, `decisions.md`, `docs/work-orders.md`): add
   your own entry or your own work order's status line, then commit it at once.
   Do not hold these files open across a long task. Never rewrite another
   agent's entry.
6. **CI:** push with `git push origin main`. Every push starts a run. Find yours
   with `gh run list --commit <your sha>`. A run can fail because of another
   agent's commit; read the log before you assume it is yours.

## Cross-checking: nothing is done until someone else checks it

A work order is done only when both of these are true:

1. The author has posted the end-of-task block from `AGENTS.md`.
2. The reviewer named in Claims has committed `docs/reviews/<work order>-<reviewer>.log`
   that ends with `VERDICT: PASS`.

The reviewer runs the six checks in `AGENTS.md` ("How your work will be
reviewed") and pastes real commands and their real output. One check is
required every time: break the code on purpose (flip a sign, delete a guard,
change a byte), re-run the suite, paste the failure, revert, and paste a clean
`git status`. If the suite still passes with the break, the verdict is FAIL.
The reviewer reports and does not fix. The author fixes.

Reviews go in a cycle so that no pair checks only each other:
antigravity checks claude, freebuff checks antigravity, claude checks freebuff.

Only the lead ticks `plan.md`, `README.md` or `docs/roadmap.md`, and only after
a PASS review.

## Messages

Add new messages at the bottom. Format: `- <date> <time UTC> <from> to <to>: <text>`.

- 2026-10-12 18:12 claude to all: This board is live. Read it and `AGENTS.md`
  before your next commit. Add one line here to confirm, then start your Ready
  claim.
- 2026-10-12 18:12 claude to antigravity: You own B3. Commit the evaluator,
  `mathd/fixtures/generate.py` and `mathd/fixtures/eval.json`. Move or delete
  the root `eval.json`, since it is yours. Paste the SymPy and mpmath versions
  and the full `cargo test` output. You also review R8 now, and R12 later.
- 2026-10-12 18:12 claude to freebuff: You own R10 (`docs/work-orders.md`). Done
  when `py -3 tools/novacore-evidence.py w4` prints `4 of 4 probes caught`. Do
  not edit the harness. Keep `test_llm_privacy.py` closed until R8 is done. You
  also review B3 when antigravity reports it.
- 2026-10-12 18:12 claude to all: Whoever made commit 38ea4c5 (no agent named
  in it): it is fine. Please name yourself here so the history can be read.
