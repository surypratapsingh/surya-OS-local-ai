"""A fail-closed capability catalogue and event-only bridge for future Nucleus IPC.

Events are written with chained hashing: each event's chain_hash is the
SHA-256 of the canonical JSON (sorted keys) of its other fields followed by the
previous event's chain_hash, and the first event links to 64 zeros. The tail is
anchored by `<outbox name>.anchor` (see chainlog.py). Every write verifies the
whole outbox first and raises ValueError on a broken one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import chainlog


class Risk(str, Enum):
    OBSERVE = "observe"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Capability:
    name: str
    risk: Risk
    requires_confirmation: bool
    available_now: bool
    description: str


@dataclass(frozen=True)
class DispatchResult:
    state: str
    message: str
    event_id: str | None = None


CATALOGUE: dict[str, Capability] = {
    "system.status": Capability(
        "system.status", Risk.OBSERVE, False, True,
        "Return NovaCore bridge status; this is not a physical hardware inventory.",
    ),
    # No test-only entries here. `_test.stub` used to sit in the
    # production catalogue with available_now=True, so a confirmed request
    # for it wrote a real outbox event (R10). Tests that need a fixture
    # capability inject one with unittest.mock.patch.dict — see
    # novacore/tests/test_capabilities.py.
    "screen.say": Capability(
        "screen.say", Risk.LOW, False, False,
        "Request a future display bridge to show text.",
    ),
    "audio.speak": Capability(
        "audio.speak", Risk.LOW, False, False,
        "Request a future local text-to-speech bridge to speak text.",
    ),
    "camera.enable": Capability(
        "camera.enable", Risk.HIGH, True, False,
        "Request camera enablement after a future privacy-safe adapter exists.",
    ),
    "microphone.enable": Capability(
        "microphone.enable", Risk.HIGH, True, False,
        "Request microphone enablement after a future privacy-safe adapter exists.",
    ),
    "system.reboot": Capability(
        "system.reboot", Risk.HIGH, True, False,
        "Request a reboot through a future trusted power adapter.",
    ),
    "updates.apply": Capability(
        "updates.apply", Risk.CRITICAL, True, False,
        "Blocked until signed packages, rollback, and the kernel capability system exist.",
    ),
}



class CapabilityBroker:
    """Writes reviewable `STUB` events; it cannot access host hardware.

    Events include sequence numbers and chained hashing for tamper detection.
    A request never extends an outbox that does not verify.
    """

    def __init__(self, event_path: Path) -> None:
        self.event_path = event_path

    def status(self) -> DispatchResult:
        return DispatchResult(
            state="observed",
            message="NovaCore has no physical hardware adapter. Only event outbox requests are available.",
        )

    @staticmethod
    def _validate_arguments(arguments: dict[str, Any]) -> DispatchResult | None:
        try:
            json.dumps(arguments, ensure_ascii=False, sort_keys=True)
            return None
        except (TypeError, ValueError) as error:
            return DispatchResult(
                "denied",
                f"capability arguments must be JSON-serializable: {error}",
            )

    def verify(self) -> tuple[int, str]:
        """Verify every event, the sequence and the anchor. Returns (last sequence, last chain_hash)."""
        sequence, head, last_line = self._read_chain()
        chainlog.check_tail(self.event_path, sequence, head, last_line)
        return sequence, head

    def anchor(self) -> None:
        """Migration: anchor an outbox that has none. The whole chain must verify first."""
        sequence, head, _ = self._read_chain()
        chainlog.adopt(self.event_path, sequence, head)

    def _read_chain(self) -> tuple[int, str, int]:
        """Raise at the first bad event. Returns (last sequence, last chain_hash, last line number)."""
        sequence, head, last_line = 0, chainlog.GENESIS, 0
        if not self.event_path.exists():
            return sequence, head, last_line
        for line_number, line in enumerate(self.event_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            where = f"{self.event_path}:{line_number}"
            try:
                event = json.loads(line)
                fields = {key: value for key, value in event.items() if key != "chain_hash"}
                number, stored = event["sequence"], event["chain_hash"]
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ValueError(f"invalid outbox event at {where}") from error
            if number != sequence + 1:
                raise ValueError(f"outbox sequence gap at {where}; expected {sequence + 1}, found {number}")
            if chainlog.chain_hash(head, fields) != stored:
                raise ValueError(
                    f"outbox chain hash mismatch at {where}; event may have been deleted, reordered, or tampered"
                )
            sequence, head, last_line = number, stored, line_number
        return sequence, head, last_line

    def request(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        owner_confirmed: bool = False,
    ) -> DispatchResult:
        capability = CATALOGUE.get(name)
        if capability is None:
            return DispatchResult("denied", f"Unknown capability: {name}")
        if capability.requires_confirmation and not owner_confirmed:
            return DispatchResult(
                "confirmation_required",
                f"{name} is {capability.risk.value}-impact and requires explicit owner confirmation.",
            )
        validation_result = self._validate_arguments(arguments)
        if validation_result is not None:
            return validation_result
        if not capability.available_now:
            return DispatchResult(
                "unavailable",
                f"{name} is not yet available; required infrastructure does not exist.",
            )
        if name == "system.status":
            return self.status()

        # Raises on a broken outbox: never extend a chain that does not verify.
        sequence, previous_chain_hash = self.verify()
        sequence += 1

        # R5 item 2: a microsecond timestamp was not unique by construction -
        # two requests in the same microsecond wrote the same event_id, and
        # neither the chain hash nor the anchor can detect that. The random
        # suffix is what makes the id unique; created_at keeps the human time.
        event_without_chain = {
            "sequence": sequence,
            "event_id": f"stub-{uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(),
            "kind": "STUB_CAPABILITY_REQUEST",
            "capability": asdict(capability),
            "arguments": arguments,
            "owner_confirmed": owner_confirmed,
            "applied": False,
        }
        chain_hash = chainlog.chain_hash(previous_chain_hash, event_without_chain)
        event = {**event_without_chain, "chain_hash": chain_hash}

        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        # A crash here leaves the anchor one event behind; the next request then
        # fails closed (see novacore/SCOPE.md).
        chainlog.write_anchor(self.event_path, sequence, chain_hash)
        return DispatchResult(
            "queued_stub",
            f"{name} was recorded as a STUB event; no hardware action occurred.",
            event["event_id"],
        )
