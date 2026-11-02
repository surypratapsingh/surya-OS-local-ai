"""Work order N1: one owner line in, one answer out; actions only by the rules.

Order of decisions for a line that is not a slash command:

1. If an action is waiting for confirmation, this line answers it. Only the
   owner's typed `yes` (or `y`) runs it. `no` cancels it. Any other line
   cancels it and is then handled as a new line. Model output never reaches
   this step, so no model can confirm anything.
2. (N4d, only with `[learning] enabled = true`) A lesson for exactly these
   words (teaching.py) comes before the router. A "do" lesson proposes or
   runs its action without asking the router; a "chat" lesson sends the words
   to chat; a "not" lesson lets the router answer, but not with the capability
   the owner refused for these words.
3. Otherwise the router (if configured) sees the owner's words and the
   offered actions, and nothing else (router.py).
4. A chat route goes to the companion, which does see recalled notes and
   history, and has no way to act.
5. An action is shown to the owner for confirmation when the capability
   requires it, or when the owner turned on `confirm_all_actions` and the
   action is not a "do" lesson the owner trusts (`trust_learned`, on by
   default). Otherwise the broker runs it at once. The broker checks every
   action again either way (capabilities.py).

With learning on, the owner's `yes` to a proposal that then ran records a
"do" lesson, and the owner's `no` records a "not" lesson. Nothing else
records one: not a cancel by another line, not a failed run, not a proposal
that came from a lesson, and never `memory.save`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

from .capabilities import CATALOGUE, CapabilityBroker, DispatchResult, argument_problem
from .companion import Companion
from .desktop import DesktopBridge
from .router import CHAT_ROUTE, Router
from .teaching import NEVER_LEARNED, TeachingStore, key


YES = frozenset({"yes", "y"})
NO = frozenset({"no", "n", "cancel"})
LEARNED = "[learned]"
LEARNING_OFF = "Learning is off ([learning] enabled = false in your configuration); nothing was saved."


@dataclass(frozen=True)
class Pending:
    capability: str
    arguments: Mapping[str, object]
    text: str = ""  # N4d: the owner's words that led to this proposal
    learned: bool = False  # N4d: proposed from a lesson, not by the router


def format_result(result: DispatchResult) -> str:
    if result.state in {"done", "failed"}:
        return result.message
    return f"{result.state}: {result.message}"


class Assistant:
    def __init__(
        self,
        companion: Companion,
        broker: CapabilityBroker,
        router: Router | None = None,
        bridge: DesktopBridge | None = None,
        *,
        confirm_all_actions: bool = True,
        teaching: TeachingStore | None = None,
        trust_learned: bool = True,
    ) -> None:
        self.companion = companion
        self.broker = broker
        self.router = router
        self.bridge = bridge
        self.confirm_all_actions = confirm_all_actions
        self.teaching = teaching
        self.trust_learned = trust_learned
        self.pending: Pending | None = None

    def handle(self, line: str) -> str:
        text = line.strip()
        if self.pending is not None:
            pending, self.pending = self.pending, None
            answer = text.casefold()
            if answer in YES:
                result = self.broker.request(pending.capability, dict(pending.arguments), owner_confirmed=True)
                reply = format_result(result)
                if result.state == "done" and not pending.learned:
                    reply += self._learn(pending, "do", "confirmed")
                return reply
            if answer in NO:
                return "Cancelled. Nothing was done." + self._learn(pending, "not", "cancelled")
            return "Cancelled the waiting action; nothing was done.\n" + self.handle(text)

        # Raises on a lesson log that does not verify: nothing runs from it.
        lesson = self.teaching.lookup(text) if self.teaching is not None else None
        note = ""
        if lesson is not None and lesson.verdict == "chat":
            return self.companion.reply(text)
        if lesson is not None and lesson.verdict == "do":
            assert lesson.capability is not None and lesson.arguments is not None
            stale = self._unusable(lesson.capability, lesson.arguments)
            if stale is None:
                return self._propose_or_run(text, lesson.capability, lesson.arguments, learned=True)
            note = f"\n[learned: the lesson for these words no longer fits ({stale}); it was not used]"

        if self.router is None:
            return self.companion.reply(text) + note
        route = self.router.route(text)
        if route.kind == CHAT_ROUTE or route.capability is None or route.args is None:
            reply = self.companion.reply(text)
            return (reply if not route.note else f"{reply}\n[router: {route.note}; answered as chat]") + note
        if lesson is not None and lesson.verdict == "not" and route.capability == lesson.capability:
            reply = self.companion.reply(text)
            return f"{reply}\n[learned: you said no to {route.capability} for these words; answered as chat]"
        return self._propose_or_run(text, route.capability, route.args, learned=False) + note

    # --- actions ------------------------------------------------------------------

    def _runs_at_once(self, capability: str, learned: bool) -> bool:
        if CATALOGUE[capability].requires_confirmation:
            return False
        return not self.confirm_all_actions or (learned and self.trust_learned)

    def _propose_or_run(self, text: str, capability: str, arguments: Mapping[str, object], learned: bool) -> str:
        mark = f" {LEARNED}" if learned else ""
        if self._runs_at_once(capability, learned):
            return format_result(self.broker.request(capability, dict(arguments))) + mark
        self.pending = Pending(capability, dict(arguments), text, learned)
        what = self.bridge.describe(capability, arguments) if self.bridge else capability
        return f"NOVA would {what} ({capability}). Type yes to do it, or no to cancel.{mark}"

    def _unusable(self, capability: str, arguments: Mapping[str, object]) -> str | None:
        """Why a learned action cannot be offered now (configuration changed), or None."""
        if self.bridge is None or not self.broker.runnable(capability):
            return f"{capability} does not run here"
        return argument_problem(self.bridge.arg_specs(capability), dict(arguments))

    # --- lessons (N4d) --------------------------------------------------------------

    def _learn(self, pending: Pending, verdict: str, source: str) -> str:
        """Record the owner's decision. Returns a note for the owner; never raises,
        because the action already ran or was already cancelled."""
        if self.teaching is None or pending.capability in NEVER_LEARNED or not key(pending.text):
            return ""
        try:
            self.teaching.add(pending.text, verdict, pending.capability, pending.arguments, source=source)
        except ValueError as error:
            return f"\n[learning: {error}]"
        if verdict == "not":
            return f"\n[learned: NOVA will not offer {pending.capability} for these words again]"
        if self._runs_at_once(pending.capability, learned=True):
            return "\n[learned: next time these words run at once]"
        return "\n[learned: next time these words skip the router and ask you first]"

    def teach(self, words: str, capability: str | None, arguments: Mapping[str, object] | None) -> str:
        """/teach: the owner states what words mean. Raises ValueError when refused."""
        if self.teaching is None:
            return LEARNING_OFF
        if capability is None:
            lesson = self.teaching.add(words, "chat", source="taught")
            return f'Learned: "{lesson.text}" is conversation; NOVA will answer it as chat.'
        if capability not in CATALOGUE:
            raise ValueError(f"unknown capability {capability!r}; see /capabilities")
        if capability in NEVER_LEARNED:
            raise ValueError(f"{capability} is never learned; it always asks for your yes")
        arguments = dict(arguments or {})
        problem = self._unusable(capability, arguments)
        if problem is not None:
            raise ValueError(f"cannot teach {capability}: {problem}")
        lesson = self.teaching.add(words, "do", capability, arguments, source="taught")
        how = "does it at once" if self._runs_at_once(capability, learned=True) else "asks you first"
        shown = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        return f'Learned: "{lesson.text}" => {capability} {shown}. Next time NOVA {how}.'

    def lessons_report(self) -> str:
        """/lessons: every lesson, oldest first, in the owner's words."""
        if self.teaching is None:
            return LEARNING_OFF.replace("nothing was saved", "there are no lessons to show")
        lessons = self.teaching.all()
        if not lessons:
            return "No lessons yet."
        last_for_key = {key(lesson.text): index for index, lesson in enumerate(lessons)}
        rows = [f"{len(lessons)} lesson{'s' if len(lessons) != 1 else ''}; for the same words, the latest counts:"]
        for index, lesson in enumerate(lessons):
            if lesson.verdict == "chat":
                what = "chat"
            elif lesson.verdict == "do":
                what = f"{lesson.capability} {json.dumps(lesson.arguments, ensure_ascii=False, sort_keys=True)}"
            else:
                what = f"not {lesson.capability}"
            replaced = "" if last_for_key[key(lesson.text)] == index else " (replaced by a later lesson)"
            rows.append(f'{index + 1}. {lesson.created_at[:10]} {lesson.source}: "{lesson.text}" => {what}{replaced}')
        return "\n".join(rows)
