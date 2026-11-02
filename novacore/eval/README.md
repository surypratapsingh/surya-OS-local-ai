# N3: choose the router model on the owner's computer

`commands.jsonl` holds 120 lines (64 commands, 56 chat). The lead (claude)
wrote them by hand on 2026-11-02 for work order N3, before any router was
scored on them. Each line holds the owner's words and the one right answer:
`{"route": "chat"}`, or one action with its arguments. `apps.json` lists the
applications the set assumes are in `[desktop.apps]`.

- 64 commands cover every desktop action: `apps.open` 12, `files.find` 6,
  `files.open` 5, `media.control` 6, `volume.control` 6, `system.lock` 3,
  `time.now` 3, `timer.set` 6, `memory.save` 4, `music.play` 7,
  `docs.create` 6.
- 56 chat lines: 22 questions, lessons and conversation; 6 requests NOVA cannot
  carry out here ("order pizza"); and 28 that use an action's words in another
  sense ("I can't find my keys anywhere", "how do I set a timer in python").

No line repeats a line of the earlier hand-written sets
(`intent/data/test.jsonl`, `test2.jsonl`, `test3.jsonl`, `dev-hand.jsonl`),
NOVA's generated data (`intent/data/train.tsv`, `dev.tsv`), or the router
prompt's own examples (`novacore/src/novacore/router.py`, `router_prompt`;
`novacore/src/novacore/desktop.py`, `action_specs`). The first draft had 25
such lines and the second 9. They were reworded, keeping the same expected
answer, before this set was committed. Every expected action passes the
broker's argument rules for the apps in `apps.json`.

Arguments are compared after lower-casing, trimming end punctuation and
dropping the leading words "my", "the", "a", "an", "to", "for", "that" and
"some" (`novacore/src/novacore/intent.py`, `same_answer`).

**Rule:** scores on this set are for choosing a router. Nothing is tuned on it:
no prompt, threshold, model or training change made to raise its score. A set
that guided a change is spent, and the next comparison needs a new set.

**Gate** (`docs/work-orders.md`, N3): zero wrong actions and zero false actions
(an action where chat was right) on the capabilities that run without
confirmation, which here is every capability except `memory.save`. Accuracy
alone does not pass.
