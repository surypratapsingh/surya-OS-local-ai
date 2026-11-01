# Keyword `J`: one daily run

When the owner's whole message is `J` (either case), do one run of this loop,
then stop. Every rule in `AGENTS.md` still applies. This file adds no
exceptions to them.

Your agent name is `claude`, `antigravity` or `freebuff`, as in `docs/team.md`.

## 1. Sync and read

```
git pull --ff-only
```

Then read `AGENTS.md`, `plan.md`, the top entry of `progress.md`, your row in
`docs/team.md` and the last 10 messages at the bottom of `docs/team.md`.

## 2. Pick exactly one item (first match wins)

1. Your work order is marked FAIL or REOPENED: fix it.
2. A review is owed by you (you are named as reviewer, or a message asks you to
   review): do it. Write `docs/reviews/<order>-<you>.log`, plant at least one
   fault, and end the log with `VERDICT:`.
3. Your current work order is in progress: continue it.
4. Your current work order is done or waiting for review: take the next item
   in your track (the track line for your name in `docs/team.md`, details in
   `todo.md`). Claim it first: edit your row in `docs/team.md`, commit, push.
   Then start.
5. Everything you could do is blocked on an owner decision: post the exact
   question in `docs/team.md`. That message is the run.

Never take a work order another agent holds. Never write outside your claimed
files.

`claude` is the lead and does not take build work. For `claude` the item is:
review every commit since the last lead review, plant a fault in each, write
the review log, update the board, and give the owner paste-ready prompts for
the other agents.

## 3. Commit before you stop

Every run ends with at least one real commit:

- Work that builds and whose existing tests pass: commit it.
- Work that does not build yet: do not commit the broken code. Post a
  `docs/team.md` message listing the uncommitted files, what works, what is
  missing and what comes next. Commit that message.

Commit with `git commit -F <msg> -- <paths>` (`git add -- <path>` first for a
new file). The message says what changed and why, and has an `Agent: <you>`
line.

## 4. Push

```
git push
```

If the push is rejected, run `git pull --no-rebase --no-edit`, then `git push`.
Never force-push, amend or rebase.

## 5. Report

Reply to the owner with the `AGENTS.md` end-of-task block and the pushed
commit hash.

## Not allowed in a `J` run

Commits whose only purpose is to make a day look active: whitespace changes,
date bumps, rewording with no new fact, empty commits, or one change split
into several commits. If there is truly nothing to do, say so and post the
status message in step 2.5.
