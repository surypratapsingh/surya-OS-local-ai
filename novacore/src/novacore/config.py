"""Configuration loading for a local, owner-selected model runner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


class ConfigError(ValueError):
    """Raised for a configuration that would make runtime behaviour ambiguous."""


@dataclass(frozen=True)
class ModelConfig:
    command: tuple[str, ...]
    timeout_seconds: int
    max_output_chars: int
    use_stdin: bool


@dataclass(frozen=True)
class ConversationConfig:
    history_turns: int
    memory_results: int


@dataclass(frozen=True)
class DesktopConfig:
    """N1: what the desktop bridge may touch. Nothing outside these lists."""

    apps: Mapping[str, tuple[str, ...]]
    folders: tuple[Path, ...]
    confirm_all_actions: bool


@dataclass(frozen=True)
class RuntimeConfig:
    source_path: Path
    state_dir: Path
    model: ModelConfig
    conversation: ConversationConfig
    # N1: both optional. Without [router] every message is chat, as before.
    router: ModelConfig | None = None
    desktop: DesktopConfig | None = None


# An application name the owner says or types: short, lower case, no spaces.
APP_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
SCHEMA_FILE_TOKEN = "{schema_file}"


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

    state_dir_raw = storage_raw.get("state_dir")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise ConfigError("storage.state_dir must be a non-empty path")
    state_dir = (path.parent / state_dir_raw).resolve()

    router = None
    if "router" in raw:
        router = _model_config(_table(raw["router"], "router"), "router")
    desktop = None
    if "desktop" in raw:
        desktop = _desktop_config(_table(raw["desktop"], "desktop"))

    return RuntimeConfig(
        source_path=path.resolve(),
        state_dir=state_dir,
        model=_model_config(model_raw, "model"),
        conversation=ConversationConfig(
            history_turns=_positive_int(conversation_raw.get("history_turns"), "conversation.history_turns"),
            memory_results=_positive_int(conversation_raw.get("memory_results"), "conversation.memory_results"),
        ),
        router=router,
        desktop=desktop,
    )


def _model_config(model_raw: dict[str, object], section: str) -> ModelConfig:
    """[model] and [router] share one set of rules; messages name the section."""
    command_raw = model_raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ConfigError(f"{section}.command must be a non-empty list of strings")
    if not all(isinstance(part, str) and part for part in command_raw):
        raise ConfigError(f"{section}.command must contain only non-empty strings")
    command = tuple(command_raw)

    use_stdin = model_raw.get("use_stdin", True)
    if not isinstance(use_stdin, bool):
        raise ConfigError(f"{section}.use_stdin must be a boolean")

    if use_stdin:
        if "{prompt_file}" in command:
            raise ConfigError(f"{section}.command cannot contain {{prompt_file}} when use_stdin is true")
    else:
        if "{prompt_file}" not in command:
            raise ConfigError(
                f"{section}.command must contain a literal {{prompt_file}} argument when use_stdin is false"
            )
    if section != "router" and SCHEMA_FILE_TOKEN in command:
        raise ConfigError(f"{section}.command cannot contain {SCHEMA_FILE_TOKEN}; only [router] uses it")

    return ModelConfig(
        command=command,
        timeout_seconds=_positive_int(model_raw.get("timeout_seconds"), f"{section}.timeout_seconds"),
        max_output_chars=_positive_int(model_raw.get("max_output_chars"), f"{section}.max_output_chars"),
        use_stdin=use_stdin,
    )


def _desktop_config(table: dict[str, object]) -> DesktopConfig:
    """[desktop]: absolute paths only, nothing expanded, unknown keys refused."""
    unknown = sorted(set(table) - {"apps", "folders", "confirm_all_actions"})
    if unknown:
        raise ConfigError("[desktop] has unknown key(s): " + ", ".join(unknown))

    confirm_all = table.get("confirm_all_actions", True)
    if not isinstance(confirm_all, bool):
        raise ConfigError("desktop.confirm_all_actions must be a boolean")

    folders_raw = table.get("folders", [])
    if not isinstance(folders_raw, list) or not all(isinstance(item, str) and item for item in folders_raw):
        raise ConfigError("desktop.folders must be a list of non-empty paths")
    folders = []
    for item in folders_raw:
        folder = Path(item)
        if not folder.is_absolute():
            raise ConfigError(f"desktop.folders: {item} is not an absolute path")
        if folder.parent == folder:
            raise ConfigError(f"desktop.folders: {item} is a whole drive or filesystem root; list a folder")
        if not folder.is_dir():
            raise ConfigError(f"desktop.folders: {item} is not an existing folder")
        folders.append(folder)

    apps_raw = _table(table.get("apps", {}), "desktop.apps")
    apps: dict[str, tuple[str, ...]] = {}
    for name, argv in apps_raw.items():
        if not APP_NAME.match(name):
            raise ConfigError(
                f"desktop.apps: {name!r} must be 1-32 lower-case letters, digits, '-' or '_'"
            )
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise ConfigError(f"desktop.apps.{name} must be a non-empty list of strings")
        if not Path(argv[0]).is_absolute():
            raise ConfigError(f"desktop.apps.{name}: the program must be an absolute path, not {argv[0]!r}")
        apps[name] = tuple(argv)

    return DesktopConfig(apps=apps, folders=tuple(folders), confirm_all_actions=confirm_all)
