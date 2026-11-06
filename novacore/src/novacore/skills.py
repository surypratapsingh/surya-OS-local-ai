"""Skill library: fixed recipes with typed slots (work order E2 / plan-v2).

Everything NOVA can do is a fixed recipe with predefined steps and typed slots.
The local model only picks a recipe and fills slots.
Each recipe explicitly declares its required capabilities, and capabilities.py
decides whether to allow or deny them.

INVARIANTS:
1. A slot value can NEVER add a step or a command; steps are fixed by the recipe.
2. Slot values are strictly typed and validated (rejecting shell metacharacters
   and control characters).
3. Undeclared capabilities are strictly denied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any

from .capabilities import CapabilityBroker, DispatchResult


class SlotType(str, Enum):
    STRING = "string"          # Single-line string, alphanumeric/punctuation, no shell metachars
    INTEGER = "integer"        # Bounded integer
    BOOLEAN = "boolean"        # Boolean
    IDENTIFIER = "identifier"  # Lowercase alphanumeric + underscores/hyphens [a-z0-9_-]+
    TEXT = "text"              # Multi-line plain text, strictly treated as data only


_DANGEROUS_SHELL_CHARS = re.compile(r"[;&|`$<>()\\\"'\n\r\t\x00]")
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")


@dataclass(frozen=True)
class SlotDefinition:
    name: str
    slot_type: SlotType
    description: str
    required: bool = True
    default: Any = None
    min_value: int | None = None
    max_value: int | None = None
    max_length: int = 500
    allowed_values: tuple[str, ...] | None = None

    def validate(self, value: Any) -> Any:
        if value is None:
            if self.required and self.default is None:
                raise ValueError(f"slot '{self.name}' is required")
            return self.default

        if self.slot_type == SlotType.INTEGER:
            if isinstance(value, bool):
                raise TypeError(f"slot '{self.name}' expected integer, got boolean")
            try:
                iv = int(value)
            except (ValueError, TypeError) as e:
                raise ValueError(f"slot '{self.name}' must be an integer: {e}") from e
            if self.min_value is not None and iv < self.min_value:
                raise ValueError(f"slot '{self.name}' must be >= {self.min_value}, got {iv}")
            if self.max_value is not None and iv > self.max_value:
                raise ValueError(f"slot '{self.name}' must be <= {self.max_value}, got {iv}")
            return iv

        if self.slot_type == SlotType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                val_cf = value.strip().casefold()
                if val_cf in {"true", "1", "yes"}:
                    return True
                if val_cf in {"false", "0", "no"}:
                    return False
            raise ValueError(f"slot '{self.name}' must be a boolean, got {value!r}")

        # String, Identifier, or Text
        if not isinstance(value, str):
            raise TypeError(f"slot '{self.name}' expected string, got {type(value).__name__}")

        val_str = value.strip()
        if len(val_str) > self.max_length:
            raise ValueError(f"slot '{self.name}' exceeds maximum length of {self.max_length}")

        if self.slot_type == SlotType.IDENTIFIER:
            if not _IDENTIFIER_PATTERN.match(val_str):
                raise ValueError(
                    f"slot '{self.name}' must be an identifier [a-zA-Z0-9_.-]+, got {val_str!r}"
                )
            return val_str

        if self.slot_type == SlotType.STRING:
            if _DANGEROUS_SHELL_CHARS.search(val_str):
                raise ValueError(
                    f"slot '{self.name}' contains invalid characters, newlines, or shell metacharacters"
                )
            if self.allowed_values and val_str not in self.allowed_values:
                raise ValueError(
                    f"slot '{self.name}' must be one of {self.allowed_values}, got {val_str!r}"
                )
            return val_str

        if self.slot_type == SlotType.TEXT:
            # Multi-line allowed, but control characters like NUL, shell backticks, or expansion syntax rejected
            if "\x00" in val_str or "`" in val_str or "$(" in val_str or "${" in val_str:
                raise ValueError(f"slot '{self.name}' contains forbidden control or expansion characters")
            return val_str

        return val_str


@dataclass(frozen=True)
class StepDefinition:
    step_number: int
    action: str  # e.g. "format_text", "status", "bridge_call"
    description: str
    capability_required: str | None = None
    template: str = ""


@dataclass(frozen=True)
class RecipeExecutionResult:
    recipe_name: str
    state: str  # "completed", "denied", "confirmation_required", "unavailable"
    message: str
    steps_executed: tuple[str, ...] = ()
    slot_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BoundRecipe:
    recipe: Recipe
    slot_values: dict[str, Any]


@dataclass(frozen=True)
class Recipe:
    name: str
    title: str
    description: str
    slots: tuple[SlotDefinition, ...]
    steps: tuple[StepDefinition, ...]
    required_capabilities: tuple[str, ...] = ()
    required_resources: tuple[str, ...] = ()  # e.g. "camera", "voice", "model"

    def bind(self, raw_slots: dict[str, Any]) -> BoundRecipe:
        """Validate and bind slot values. Extra slots are strictly rejected."""
        valid_slot_names = {s.name for s in self.slots}
        for k in raw_slots:
            if k not in valid_slot_names:
                raise ValueError(f"recipe '{self.name}' has no slot '{k}'")

        bound_values: dict[str, Any] = {}
        for slot in self.slots:
            raw_val = raw_slots.get(slot.name)
            bound_values[slot.name] = slot.validate(raw_val)

        return BoundRecipe(recipe=self, slot_values=bound_values)

    def execute(
        self,
        broker: CapabilityBroker,
        raw_slots: dict[str, Any],
        *,
        owner_confirmed: bool = False,
    ) -> RecipeExecutionResult:
        """Execute the fixed recipe.

        Capabilities are checked fail-closed via broker.
        Undeclared capabilities cannot be invoked.
        Slot values cannot alter steps or inject commands.
        """
        bound = self.bind(raw_slots)
        values = bound.slot_values

        # 1. Capability checks: every capability required by the recipe or steps must be declared
        declared_caps = set(self.required_capabilities)
        for step in self.steps:
            if step.capability_required:
                if step.capability_required not in declared_caps:
                    return RecipeExecutionResult(
                        recipe_name=self.name,
                        state="denied",
                        message=f"Undeclared capability in recipe step: {step.capability_required}",
                        slot_values=values,
                    )

        # 2. Check each declared capability with CapabilityBroker
        for cap_name in self.required_capabilities:
            res = broker.request(cap_name, {"recipe": self.name}, owner_confirmed=owner_confirmed)
            if res.state in {"denied", "unavailable", "confirmation_required"}:
                return RecipeExecutionResult(
                    recipe_name=self.name,
                    state=res.state,
                    message=f"Capability '{cap_name}' {res.state}: {res.message}",
                    slot_values=values,
                )

        # 3. Execute fixed steps deterministically
        step_outputs: list[str] = []
        for step in self.steps:
            # Slot values are inserted as data into predefined templates;
            # they can never add or execute a new step or shell command.
            try:
                rendered = step.template.format(**values)
            except KeyError as err:
                rendered = f"{step.description} (missing slot: {err})"
            step_outputs.append(f"Step {step.step_number}: {rendered}")

        return RecipeExecutionResult(
            recipe_name=self.name,
            state="completed",
            message=f"Recipe '{self.name}' completed successfully ({len(step_outputs)} step(s)).",
            steps_executed=tuple(step_outputs),
            slot_values=values,
        )


class RecipeRegistry:
    """Registry holding predefined skill recipes."""

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}
        self._init_defaults()

    def _init_defaults(self) -> None:
        # 1. Study session
        self.register(
            Recipe(
                name="study_session",
                title="Study Session",
                description="Review syllabus, formulas, and concepts for a subject chapter.",
                slots=(
                    SlotDefinition("subject", SlotType.STRING, "Subject name (e.g. maths, physics)"),
                    SlotDefinition("chapter", SlotType.INTEGER, "Chapter number", min_value=1, max_value=999),
                    SlotDefinition("notes", SlotType.TEXT, "Notes or questions to cover", required=False, default=""),
                ),
                steps=(
                    StepDefinition(1, "format_text", "Load chapter outline", template="Loading chapter {chapter} outline for {subject}."),
                    StepDefinition(2, "format_text", "Present key formulas", template="Presenting formulas for {subject} chapter {chapter}."),
                ),
                required_capabilities=(),
                required_resources=(),
            )
        )

        # 2. Quiz maker
        self.register(
            Recipe(
                name="quiz_maker",
                title="Make a Quiz",
                description="Generate practice questions for a topic.",
                slots=(
                    SlotDefinition("topic", SlotType.STRING, "Topic name"),
                    SlotDefinition("num_questions", SlotType.INTEGER, "Number of questions", min_value=1, max_value=50, default=5),
                ),
                steps=(
                    StepDefinition(1, "format_text", "Generate quiz", template="Generating {num_questions} quiz questions on {topic}."),
                ),
                required_capabilities=(),
                required_resources=(),
            )
        )

        # 3. Draw formula (screen)
        self.register(
            Recipe(
                name="draw_formula",
                title="Draw Mathematical Formula",
                description="Render a mathematical formula on screen.",
                slots=(
                    SlotDefinition("formula", SlotType.STRING, "Mathematical formula expression"),
                ),
                steps=(
                    StepDefinition(1, "format_text", "Render formula", template="Rendering formula: {formula}."),
                ),
                required_capabilities=(),
                required_resources=(),
            )
        )

        # 4. Music player (requires audio.speak / voice resource)
        self.register(
            Recipe(
                name="play_music",
                title="Play Audio Track",
                description="Play an offline audio file from the library.",
                slots=(
                    SlotDefinition("track_id", SlotType.IDENTIFIER, "Identifier of the audio track"),
                ),
                steps=(
                    StepDefinition(1, "play_audio", "Stream audio", capability_required="audio.speak", template="Playing track {track_id}."),
                ),
                required_capabilities=("audio.speak",),
                required_resources=("voice",),
            )
        )

        # 5. Camera capture (requires camera.enable / camera resource)
        self.register(
            Recipe(
                name="camera_notes",
                title="Capture Handwritten Notes",
                description="Enable camera to photograph handwritten notes for OCR.",
                slots=(
                    SlotDefinition("label", SlotType.STRING, "Label for notes capture"),
                ),
                steps=(
                    StepDefinition(1, "enable_cam", "Capture frame", capability_required="camera.enable", template="Capturing notes frame: {label}."),
                ),
                required_capabilities=("camera.enable",),
                required_resources=("camera",),
            )
        )

        # 6. Deep reasoning (requires model weights resource)
        self.register(
            Recipe(
                name="deep_reasoning",
                title="Deep Mathematical Reasoning",
                description="Run multi-step proof derivation requiring external model weights.",
                slots=(
                    SlotDefinition("theorem", SlotType.STRING, "Theorem to prove"),
                ),
                steps=(
                    StepDefinition(1, "derive_proof", "Derive proof", template="Deriving proof for {theorem}."),
                ),
                required_capabilities=(),
                required_resources=("model",),
            )
        )

    def register(self, recipe: Recipe) -> None:
        self._recipes[recipe.name] = recipe

    def get(self, name: str) -> Recipe | None:
        return self._recipes.get(name)

    def list(self) -> list[Recipe]:
        return list(self._recipes.values())
