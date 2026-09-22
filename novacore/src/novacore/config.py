"""Configuration loading for a local, owner-selected model runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Raised for a configuration that would make runtime behaviour ambiguous."""


@dataclass(frozen=True)
class ModelConfig:
    command: tuple[str, ...]
    timeout_seconds: int
    max_output_chars: int


@dataclass(frozen=True)
class ConversationConfig:
    history_turns: int
    memory_results: int


@dataclass(frozen=True)
class RuntimeConfig:
    source_path: Path
    state_dir: Path
    model: ModelConfig
    conversation: ConversationConfig


def _table(raw: object, name: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return raw


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def load_config(path: Path) -> RuntimeConfig:
    """Load a configuration without expanding shell syntax or environment variables."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"configuration not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error

    model_raw = _table(raw.get("model"), "model")
    conversation_raw = _table(raw.get("conversation"), "conversation")
    storage_raw = _table(raw.get("storage"), "storage")

    command_raw = model_raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ConfigError("model.command must be a non-empty list of strings")
    if not all(isinstance(part, str) and part for part in command_raw):
        raise ConfigError("model.command must contain only non-empty strings")
    command = tuple(command_raw)
    if "{prompt_file}" not in command:
        raise ConfigError("model.command must contain a literal {prompt_file} argument")

    state_dir_raw = storage_raw.get("state_dir")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise ConfigError("storage.state_dir must be a non-empty path")
    state_dir = (path.parent / state_dir_raw).resolve()

    return RuntimeConfig(
        source_path=path.resolve(),
        state_dir=state_dir,
        model=ModelConfig(
            command=command,
            timeout_seconds=_positive_int(model_raw.get("timeout_seconds"), "model.timeout_seconds"),
            max_output_chars=_positive_int(model_raw.get("max_output_chars"), "model.max_output_chars"),
        ),
        conversation=ConversationConfig(
            history_turns=_positive_int(conversation_raw.get("history_turns"), "conversation.history_turns"),
            memory_results=_positive_int(conversation_raw.get("memory_results"), "conversation.memory_results"),
        ),
    )
