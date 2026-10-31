"""Work order N1: one owner line in, one answer out; actions only by the rules.

Order of decisions for a line that is not a slash command:

1. If an action is waiting for confirmation, this line answers it. Only the
   owner's typed `yes` (or `y`) runs it. `no` cancels it. Any other line
   cancels it and is then handled as a new line. Model output never reaches
   this step, so no model can confirm anything.
2. Otherwise the router (if configured) sees the owner's words and the
   offered actions, and nothing else (router.py).
3. A chat route goes to the companion, which does see recalled notes and
   history, and has no way to act.
4. An action route is shown to the owner for confirmation when the
   capability requires it or the owner turned on `confirm_all_actions`;
   otherwise the broker runs it at once. The broker checks it again either
   way (capabilities.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .capabilities import CATALOGUE, CapabilityBroker, DispatchResult
from .companion import Companion
from .desktop import DesktopBridge
from .router import CHAT_ROUTE, Router


YES = frozenset({"yes", "y"})
NO = frozenset({"no", "n", "cancel"})


@dataclass(frozen=True)
class Pending:
    capability: str
    arguments: Mapping[str, object]


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
    ) -> None:
        self.companion = companion
        self.broker = broker
        self.router = router
        self.bridge = bridge
        self.confirm_all_actions = confirm_all_actions
        self.pending: Pending | None = None

    def handle(self, line: str) -> str:
        text = line.strip()
        if self.pending is not None:
            pending, self.pending = self.pending, None
            answer = text.casefold()
            if answer in YES:
                result = self.broker.request(pending.capability, dict(pending.arguments), owner_confirmed=True)
                return format_result(result)
            if answer in NO:
                return "Cancelled. Nothing was done."
            return "Cancelled the waiting action; nothing was done.\n" + self.handle(text)

        if self.router is None:
            return self.companion.reply(text)
        route = self.router.route(text)
        if route.kind == CHAT_ROUTE or route.capability is None or route.args is None:
            reply = self.companion.reply(text)
            return reply if not route.note else f"{reply}\n[router: {route.note}; answered as chat]"

        capability = CATALOGUE[route.capability]
        if capability.requires_confirmation or self.confirm_all_actions:
            self.pending = Pending(route.capability, dict(route.args))
            what = self.bridge.describe(route.capability, route.args) if self.bridge else route.capability
            return f"NOVA would {what} ({route.capability}). Type yes to do it, or no to cancel."
        return format_result(self.broker.request(route.capability, dict(route.args)))
