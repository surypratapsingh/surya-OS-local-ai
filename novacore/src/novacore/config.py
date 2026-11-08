"""Configuration loading for a local, owner-selected model runner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from .capabilities import CATALOGUE


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
    # N5: where docs.create writes (None: the skill is off), and whether
    # music.play may hand the words to Spotify's own search (Spotify goes online).
    documents_folder: Path | None = None
    spotify_search: bool = False


@dataclass(frozen=True)
class VoiceConfig:
    """N2: the programs that hear and speak. NovaCore only sees text lines."""

    listener: tuple[str, ...]
    speaker: tuple[str, ...] | None
    wake_words: tuple[str, ...]
    # N2b: how long a spoken offer waits for yes or no before it expires.
    confirm_seconds: int = 30


@dataclass(frozen=True)
class LearningConfig:
    """N4d: whether NOVA keeps lessons from the owner's yes and no (teaching.py)."""

    enabled: bool = False
    trust_learned: bool = True


@dataclass(frozen=True)
class McpConfig:
    """N6: the capabilities an MCP client may call (mcp_server.py). Nothing else."""

    tools: tuple[str, ...]


# Capabilities an MCP client may never be given, and why.
NOT_FOR_MCP = {
    "memory.save": "it needs your typed yes, and MCP cannot carry that from NOVA's own window",
    "timer.set": "it announces in NOVA's own window, and an MCP server has none",
}


@dataclass(frozen=True)
class RuntimeConfig:
    source_path: Path
    state_dir: Path
    model: ModelConfig
    conversation: ConversationConfig
    # N1: both optional. Without [router] every message is chat, as before.
    router: ModelConfig | None = None
    desktop: DesktopConfig | None = None
    voice: VoiceConfig | None = None
    # N4d: off unless the owner turns it on.
    learning: LearningConfig = LearningConfig()
    # N6: without [mcp], --mcp refuses to start.
    mcp: McpConfig | None = None


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
    voice = None
    if "voice" in raw:
        voice = _voice_config(_table(raw["voice"], "voice"))
    learning = LearningConfig()
    if "learning" in raw:
        learning = _learning_config(_table(raw["learning"], "learning"))
    mcp = None
    if "mcp" in raw:
        mcp = _mcp_config(_table(raw["mcp"], "mcp"))

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
        voice=voice,
        learning=learning,
        mcp=mcp,
    )


def _mcp_config(table: dict[str, object]) -> McpConfig:
    unknown = sorted(set(table) - {"tools"})
    if unknown:
        raise ConfigError("[mcp] has unknown key(s): " + ", ".join(unknown))
    tools = table.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(name, str) for name in tools):
        raise ConfigError("mcp.tools must be a non-empty list of capability names")
    if len(set(tools)) != len(tools):
        raise ConfigError("mcp.tools lists a capability twice")
    for name in tools:
        if name in NOT_FOR_MCP:
            pass  # PLANT f2-14-p2
        capability = CATALOGUE.get(name)
        if capability is None or capability.bridge != "desktop" or not capability.available_now:
            raise ConfigError(f"mcp.tools: {name!r} is not a desktop capability (see /capabilities)")
    return McpConfig(tools=tuple(tools))


def _learning_config(table: dict[str, object]) -> LearningConfig:
    unknown = sorted(set(table) - {"enabled", "trust_learned"})
    if unknown:
        raise ConfigError("[learning] has unknown key(s): " + ", ".join(unknown))
    values = {name: table.get(name, default) for name, default in (("enabled", False), ("trust_learned", True))}
    for name, value in values.items():
        if not isinstance(value, bool):
            raise ConfigError(f"learning.{name} must be true or false")
    return LearningConfig(**values)


def _program(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise ConfigError(f"{name} must be a non-empty list of strings")
    if not Path(value[0]).is_absolute():
        raise ConfigError(f"{name}: the program must be an absolute path, not {value[0]!r}")
    return tuple(value)


def _voice_config(table: dict[str, object]) -> VoiceConfig:
    unknown = sorted(set(table) - {"listener", "speaker", "wake_words", "confirm_seconds"})
    if unknown:
        raise ConfigError("[voice] has unknown key(s): " + ", ".join(unknown))
    listener = _program(table.get("listener"), "voice.listener")
    speaker = _program(table["speaker"], "voice.speaker") if "speaker" in table else None
    wake_words = table.get("wake_words", ["nova", "hey nova", "ok nova", "okay nova", "hi nova"])
    if not isinstance(wake_words, list) or not wake_words or not all(isinstance(w, str) and w.strip() for w in wake_words):
        raise ConfigError("voice.wake_words must be a non-empty list of words")
    confirm_seconds = table.get("confirm_seconds", 30)
    if isinstance(confirm_seconds, bool) or not isinstance(confirm_seconds, int) or not 1 <= confirm_seconds <= 600:
        raise ConfigError("voice.confirm_seconds must be a whole number from 1 to 600")
    return VoiceConfig(listener=listener, speaker=speaker, wake_words=tuple(w.strip().lower() for w in wake_words),
                       confirm_seconds=confirm_seconds)


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
    unknown = sorted(set(table) - {"apps", "folders", "confirm_all_actions", "documents_folder", "spotify_search"})
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

    documents_folder = None
    if "documents_folder" in table:
        raw = table["documents_folder"]
        if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
            raise ConfigError("desktop.documents_folder must be an absolute path")
        documents_folder = Path(raw)
        if documents_folder.parent == documents_folder or not documents_folder.is_dir():
            raise ConfigError(f"desktop.documents_folder: {raw} is not an existing folder below a drive root")
    spotify_search = table.get("spotify_search", False)
    if not isinstance(spotify_search, bool):
        raise ConfigError("desktop.spotify_search must be a boolean")

    return DesktopConfig(
        apps=apps,
        folders=tuple(folders),
        confirm_all_actions=confirm_all,
        documents_folder=documents_folder,
        spotify_search=spotify_search,
    )
