"""A fail-closed capability catalogue and event-only bridge for future Nucleus IPC."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
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


class CapabilityBroker:
    """Writes reviewable `STUB` events; it cannot access host hardware."""

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
        event = {
            "event_id": f"stub-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}",
            "created_at": datetime.now(UTC).isoformat(),
            "kind": "STUB_CAPABILITY_REQUEST",
            "capability": asdict(capability),
            "arguments": arguments,
            "owner_confirmed": owner_confirmed,
            "applied": False,
        }
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        return DispatchResult(
            "queued_stub",
            f"{name} was recorded as a STUB event; no hardware action occurred.",
            event["event_id"],
        )
