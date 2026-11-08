# Autorun queue

The lead (claude) writes this file. The `autorun` lane reads it and never edits it.

`autorun` is Claude Code in headless mode on a free OpenRouter model. `tools/autorun/nova-autorun.ps1` starts it once an hour while the owner's laptop is on, in its own clone `Documents\nova-autorun`. `AGENTS.md` applies in full.

## How a run works

1. Read `docs/autorun-log.md`. The next item is the first `AR.<n>` below with no `DONE` line in that log. If the last line of the log is a `WIP` line for an item, carry on with that item.
2. Do that one item. Then append one line to `docs/autorun-log.md`:
   - `- <UTC time> AR.<n> DONE: <what now exists, and the test result with real numbers>`
   - `- <UTC time> AR.<n> WIP: <what works, what is missing>`. Use this when the item is not finished and the tests do not pass yet. Commit only the log line, never broken code.
   - `- <UTC time> AR.<n> BLOCKED: <the exact question for the lead>`
   
   Take the time from `date -u '+%Y-%m-%d %H:%M UTC'`.
3. Commit the item's files and the log line together. Do not push. The runner pushes only if every changed path is allowed, every commit has an `Agent: autorun` line, and `python -m unittest discover -s tools/truth -p "test_*.py"` passes from the repo root.

## Paths you may write

- `tools/truth/*.py`: one tool per file, and its test `tools/truth/test_<tool>.py`.
- `docs/logs/autorun-*`: reports the tools produce.
- `docs/autorun-log.md`: one line per run.

Anything else, including `.github/`, `scripts/`, `kernel/`, `mathd/`, `novacore/` and every shared doc, is out of bounds. If an item needs a change there, write the exact change in your log line as `BLOCKED`, and the lead passes it on.

## Rules for every tool

- Python standard library only. Run as `python tools/truth/<tool>.py [args]`. Exit 0 when there are no findings and 1 when there are, and print one line per finding plus a final count line.
- The test builds its own input in a temporary directory: a small file tree, or a small git repo made with `git init` and `git commit`. Git is the independent oracle for anything about commits. Never test a tool against data the tool itself produced (`AGENTS.md` rule 1).
- Every test file has at least one plant test: break the input in one known way and assert that the tool reports exactly that finding.
- Tests import the tool by path, so they run from the repo root: `importlib.util.spec_from_file_location("x", "tools/truth/<tool>.py")`.
- Reports in `docs/logs/autorun-*` hold the exact command, its real output and the commit hash it ran on (`git rev-parse --short HEAD`).

## Items

- **AR.1 `team_times.py`, part 1.** Rule 12 of `docs/queue-2026-11-06.md` says message times in `docs/team.md` must come from a command. Find every line of the form `- YYYY-MM-DD HH:MM UTC <agent> to ...`. For each one, get the time of the commit that added that line from `git blame --line-porcelain` (`committer-time`, in UTC). Report each message whose stated time differs from its commit time by more than 30 minutes. Test: a temp git repo with three messages, one of them stamped two hours off; the tool must report exactly that one.
- **AR.2 `team_times.py`, part 2: the report.** Run AR.1 on this repo. Commit `docs/logs/autorun-team-times-<YYYY-MM-DD>.log`. Lines that use `IST` or have no zone are listed separately as "not checkable", not as findings.
- **AR.3 `verdicts.py`.** For every file in `docs/reviews/`, print its name, its last line that starts with `VERDICT`, and the date of its last commit. A file with no `VERDICT` line is a finding. Test: three review files in a temp dir, one without a verdict.
- **AR.4 Verdict report.** Run AR.3 on this repo and commit `docs/logs/autorun-verdicts-<YYYY-MM-DD>.log`.
- **AR.5 `plant_lines.py`.** This checks rule 15 of `docs/queue-2026-11-06b.md`. In a review log, a plant line looks like `P<n> <change> -> CAUGHT by <test>` or `P<n> <change> -> MISSED`. Report a log whose last verdict is `VERDICT: FAIL` but which has plant lines and none of them is `MISSED`, unless the verdict line names a false claim or an invariant. Also report a log whose last verdict is `VERDICT: PASS` but which has a `MISSED` plant line. Only check logs that contain at least one rule-15 plant line, and print how many logs were skipped. Test: four logs, covering a correct PASS, a correct FAIL, a FAIL with every plant caught, and a PASS with a missed plant.
- **AR.6 Plant-line report.** Run AR.5 on this repo and commit `docs/logs/autorun-plant-lines-<YYYY-MM-DD>.log`.
- **AR.7 `queue_counts.py`.** Each `docs/queue-*.md` file states its item count in its first lines ("It holds 44 items"). Count its item bullets (lines that start with `- **` followed by a chat letter and an item number, such as `- **A2.19`). Report a file whose stated count is not the counted one. Test: one file whose count is right and one whose count is off by one.
- **AR.8 Queue count report.** Run AR.7 on this repo and commit `docs/logs/autorun-queue-counts-<YYYY-MM-DD>.log`.
- **AR.9 Tests for `tools/check-citations.py`.** Do not edit that tool. Write `tools/truth/test_check_citations.py`, which runs it as a subprocess on a temp copy that has one cited path missing. First read the tool, because it may assume the repo layout. Assert that it exits non-zero and names the missing path. If the tool cannot be pointed at a temp tree without editing it, write `BLOCKED` with the exact change it needs.
- **AR.10 `agent_lines.py`.** Given a start commit, list every non-merge commit since then that has no line starting with `Agent:`. Group the list by author name and print a count per author. Test: a temp git repo with three commits, one of them without the line.
- **AR.11 Agent-line report.** Run AR.10 on this repo from c7e63e3 and commit `docs/logs/autorun-agent-lines-<YYYY-MM-DD>.log`.
- **AR.12 `run_all.py`.** Run every truth tool above, write one combined `docs/logs/autorun-truth-<YYYY-MM-DD>.log`, and exit 1 if any tool found anything. Test with the four temp repos the other tests already build. Reuse their helper code by importing it, not by copying it. In your log line, write as `BLOCKED` the exact `.github/workflows/check.yml` step that would run this tool in CI, for the lead to pass to A2.

## After AR.12

When every item is `DONE`, each run does this, until the lead adds items:

- **AR.R Daily truth run.** Run `python tools/truth/run_all.py`. Compare the findings with those in the newest `docs/logs/autorun-truth-*.log`. Commit a new dated log only if the findings changed. If nothing changed, commit nothing, and append no log line. A run that changes nothing makes no commit. Commits whose only purpose is to make a day look active are forbidden (`docs/daily-run.md`).
