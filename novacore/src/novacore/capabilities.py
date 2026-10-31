"""A fail-closed capability catalogue, its argument rules, and the broker.

Two kinds of capability live here. "outbox" capabilities have no executor:
a confirmed, available request is recorded as a STUB event for a future
Nucleus bridge. "desktop" capabilities (work order N1) run on the owner's
computer through an executor the caller supplies (`novacore.desktop`); the
broker checks the request against the executor's argument rules first and
records every run, applied or not.

Events are written with chained hashing: each event's chain_hash is the
SHA-256 of the canonical JSON (sorted keys) of its other fields followed by the
previous event's chain_hash, and the first event links to 64 zeros. The tail is
anchored by `<outbox name>.anchor` (see chainlog.py). Every write verifies the
whole outbox first and raises ValueError on a broken one. A desktop action is
not started on an outbox that does not verify.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
import json
from pathlib import Path
from typing import Any, Protocol
import unicodedata
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
    # "outbox": no executor; a request becomes a STUB event.
    # "desktop": runs through the executor passed to CapabilityBroker (N1).
    bridge: str = "outbox"


@dataclass(frozen=True)
class DispatchResult:
    state: str
    message: str
    event_id: str | None = None


# --- Argument rules (N1) -----------------------------------------------------
# A desktop action's arguments are checked here, at the boundary, against the
# rules its executor declares. The router checks them as well before it
# proposes an action, but this is the check that counts: `/request` and
# `/confirm` reach the broker without going through the router.

# Characters that could hide or reorder what the owner reads in a
# confirmation line: control characters (Cc), line and paragraph separators
# (Zl, Zp) and the Unicode bidirectional overrides and isolates.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _unsafe_character(text: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"} or character in _BIDI_CONTROLS
        for character in text
    )


@dataclass(frozen=True)
class TextArg:
    min_length: int
    max_length: int

    def problem(self, value: object) -> str | None:
        if not isinstance(value, str):
            return "must be text"
        if not self.min_length <= len(value) <= self.max_length:
            return f"must be {self.min_length} to {self.max_length} characters"
        if _unsafe_character(value):
            return "must not contain control, line-separator or direction-override characters"
        return None

    def schema(self) -> dict[str, object]:
        return {"type": "string", "minLength": self.min_length, "maxLength": self.max_length}

    def describe(self) -> str:
        return f"text, {self.min_length} to {self.max_length} characters"


@dataclass(frozen=True)
class IntArg:
    minimum: int
    maximum: int

    def problem(self, value: object) -> str | None:
        # bool is a subclass of int in Python; JSON true is not a number.
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be a whole number"
        if not self.minimum <= value <= self.maximum:
            return f"must be from {self.minimum} to {self.maximum}"
        return None

    def schema(self) -> dict[str, object]:
        return {"type": "integer", "minimum": self.minimum, "maximum": self.maximum}

    def describe(self) -> str:
        return f"whole number from {self.minimum} to {self.maximum}"


@dataclass(frozen=True)
class ChoiceArg:
    choices: tuple[str, ...]

    def problem(self, value: object) -> str | None:
        if not isinstance(value, str) or value not in self.choices:
            return "must be one of: " + ", ".join(self.choices)
        return None

    def schema(self) -> dict[str, object]:
        return {"type": "string", "enum": list(self.choices)}

    def describe(self) -> str:
        return "one of: " + ", ".join(f'"{choice}"' for choice in self.choices)


ArgSpec = TextArg | IntArg | ChoiceArg


def argument_problem(specs: Mapping[str, ArgSpec], arguments: object) -> str | None:
    """Return why `arguments` breaks `specs`, or None. Exact key set; no extras."""
    if not isinstance(arguments, dict):
        return "arguments must be a JSON object"
    missing = sorted(set(specs) - set(arguments))
    if missing:
        return "missing argument(s): " + ", ".join(missing)
    extra = sorted(str(key) for key in set(arguments) - set(specs))
    if extra:
        return "unexpected argument(s): " + ", ".join(extra)
    for key, spec in specs.items():
        problem = spec.problem(arguments[key])
        if problem is not None:
            return f"{key} {problem}"
    return None


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


class Executor(Protocol):
    """What the broker needs from a bridge that can act (novacore.desktop)."""

    def available(self) -> frozenset[str]:
        """Names this executor can run on this computer with this configuration."""

    def arg_specs(self, name: str) -> Mapping[str, ArgSpec]:
        """The argument rules for one available name."""

    def run(self, name: str, arguments: dict[str, object]) -> ActionResult:
        """Run a request whose arguments already passed `argument_problem`."""


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
    # N1 desktop actions. They run only through a configured desktop bridge
    # (novacore.desktop); without one, a request returns `unavailable`.
    "apps.open": Capability(
        "apps.open", Risk.LOW, False, True,
        "Start one application the owner listed by name in the configuration.",
        "desktop",
    ),
    "files.find": Capability(
        "files.find", Risk.OBSERVE, False, True,
        "List files whose folder and name match words, inside owner-listed folders only.",
        "desktop",
    ),
    "files.open": Capability(
        "files.open", Risk.LOW, False, True,
        "Open one document or media file found by name inside owner-listed folders.",
        "desktop",
    ),
    "media.control": Capability(
        "media.control", Risk.LOW, False, True,
        "Play or pause media, or skip to the next or previous track.",
        "desktop",
    ),
    "volume.control": Capability(
        "volume.control", Risk.LOW, False, True,
        "Turn the speaker volume up or down, or mute and unmute it.",
        "desktop",
    ),
    "system.lock": Capability(
        "system.lock", Risk.LOW, False, True,
        "Lock the screen.",
        "desktop",
    ),
    "time.now": Capability(
        "time.now", Risk.OBSERVE, False, True,
        "Tell the local date and time from this computer's clock.",
        "desktop",
    ),
    "timer.set": Capability(
        "timer.set", Risk.LOW, False, True,
        "Announce a reminder after a number of minutes while NOVA is running.",
        "desktop",
    ),
    "memory.save": Capability(
        "memory.save", Risk.LOW, True, True,
        "Save a note the owner has read and confirmed, exactly as shown.",
        "desktop",
    ),
    "updates.apply": Capability(
        "updates.apply", Risk.CRITICAL, True, False,
        "Blocked until signed packages, rollback, and the kernel capability system exist.",
    ),
}



class CapabilityBroker:
    """Checks every request against the catalogue and records it.

    Outbox capabilities become reviewable `STUB` events. Desktop capabilities
    run through `executor` when one is given and supports them; without it
    they are `unavailable`. Events include sequence numbers and chained
    hashing for tamper detection. A request never extends an outbox that does
    not verify, and a desktop action is never started on one.
    """

    def __init__(self, event_path: Path, executor: Executor | None = None) -> None:
        self.event_path = event_path
        self.executor = executor

    def status(self) -> DispatchResult:
        ready = sorted(self.executor.available()) if self.executor is not None else []
        if not ready:
            return DispatchResult(
                state="observed",
                message="NovaCore has no physical hardware adapter. Only event outbox requests are available.",
            )
        return DispatchResult(
            state="observed",
            message="Desktop bridge ready for: " + ", ".join(ready)
            + ". Other capabilities are event outbox requests only.",
        )

    def runnable(self, name: str) -> bool:
        """True when `name` is a desktop capability this broker can run now."""
        capability = CATALOGUE.get(name)
        return (
            capability is not None
            and capability.available_now
            and capability.bridge == "desktop"
            and self.executor is not None
            and name in self.executor.available()
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
        if capability.bridge == "desktop":
            return self._run_desktop(capability, arguments, owner_confirmed)

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

    def _run_desktop(
        self,
        capability: Capability,
        arguments: dict[str, Any],
        owner_confirmed: bool,
    ) -> DispatchResult:
        name = capability.name
        if not self.runnable(name):
            return DispatchResult(
                "unavailable",
                f"{name} needs the desktop bridge, which is not configured for it "
                "or not supported on this computer.",
            )
        assert self.executor is not None  # runnable() checked it
        problem = argument_problem(self.executor.arg_specs(name), arguments)
        if problem is not None:
            return DispatchResult("denied", f"{name}: {problem}")

        # Raises on a broken outbox before anything runs: an action is never
        # taken that the tamper-evident log cannot record.
        sequence, previous_chain_hash = self.verify()
        try:
            result = self.executor.run(name, dict(arguments))
        except Exception as error:  # an executor fault is still recorded
            result = ActionResult(False, f"{name} failed: {type(error).__name__}: {error}")
        sequence += 1
        event_without_chain = {
            "sequence": sequence,
            "event_id": f"run-{uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(),
            "kind": "CAPABILITY_RUN",
            "capability": asdict(capability),
            "arguments": arguments,
            "owner_confirmed": owner_confirmed,
            "applied": result.ok,
            "result": result.message,
        }
        chain_hash = chainlog.chain_hash(previous_chain_hash, event_without_chain)
        event = {**event_without_chain, "chain_hash": chain_hash}
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        # A crash here leaves the anchor one event behind; the next request
        # then fails closed (see novacore/SCOPE.md).
        chainlog.write_anchor(self.event_path, sequence, chain_hash)
        return DispatchResult("done" if result.ok else "failed", result.message, event["event_id"])

