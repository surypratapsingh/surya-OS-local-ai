"""A fail-closed capability catalogue and event-only bridge for future Nucleus IPC.

Events are written with chained hashing: each event includes a chain_hash
computed from its own fields plus the previous event's chain_hash, making
deletion, reordering, and tampering detectable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any


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
    "_test.stub": Capability(
        "_test.stub", Risk.LOW, True, True,
        "Test-only capability: requires confirmation, is available. Used by test suite only.",
    ),
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


_GENESIS_CHAIN_HASH = "0" * 64


class CapabilityBroker:
    """Writes reviewable `STUB` events; it cannot access host hardware.

    Events include sequence numbers and chained hashing for tamper detection.
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

    def _read_last_sequence_and_chain(self) -> tuple[int, str]:
        """Return (sequence_number, chain_hash) of the last event, or (0, genesis) if empty."""
        if not self.event_path.exists():
            return 0, _GENESIS_CHAIN_HASH
        try:
            lines = self.event_path.read_text(encoding="utf-8").splitlines()
            if not lines:
                return 0, _GENESIS_CHAIN_HASH
            last_line = lines[-1]
            if not last_line:
                return 0, _GENESIS_CHAIN_HASH
            event = json.loads(last_line)
            return event.get("sequence", 0), event.get("chain_hash", _GENESIS_CHAIN_HASH)
        except Exception:
            # Corrupted file or invalid JSON; start fresh chain.
            return 0, _GENESIS_CHAIN_HASH

    @staticmethod
    def _compute_chain_hash(previous_chain_hash: str, event_without_chain: dict) -> str:
        """Compute chain hash: SHA-256 of canonical event JSON + previous chain hash."""
        canonical = json.dumps(event_without_chain, ensure_ascii=False, sort_keys=True)
        combined = canonical + previous_chain_hash
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

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

        # Get sequence and previous chain for tamper-evident chaining.
        sequence, previous_chain_hash = self._read_last_sequence_and_chain()
        sequence += 1

        event_without_chain = {
            "sequence": sequence,
            "event_id": f"stub-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}",
            "created_at": datetime.now(UTC).isoformat(),
            "kind": "STUB_CAPABILITY_REQUEST",
            "capability": asdict(capability),
            "arguments": arguments,
            "owner_confirmed": owner_confirmed,
            "applied": False,
        }
        chain_hash = self._compute_chain_hash(previous_chain_hash, event_without_chain)
        event = {**event_without_chain, "chain_hash": chain_hash}

        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        return DispatchResult(
            "queued_stub",
            f"{name} was recorded as a STUB event; no hardware action occurred.",
            event["event_id"],
        )
