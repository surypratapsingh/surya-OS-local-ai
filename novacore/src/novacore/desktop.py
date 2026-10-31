"""Work order N1: the desktop bridge, one narrow executor per capability.

The bridge acts on the owner's computer only through `Platform`, and only
with arguments the broker has already checked against `arg_specs()`:

- `apps.open` starts a program the owner listed by name in the configuration,
  with the fixed argv written there. The model picks a name, never a path.
- `files.find` and `files.open` search by words inside owner-listed folders.
  The model never writes a path. Every hit is resolved (symlinks and
  junctions included) and must still lie inside a listed folder, and
  `files.open` hands only document and media types to the system's default
  application.
- No executor uses a shell. Helper programs are found by name on PATH and run
  with a fixed argv (Linux), or reached through documented Windows API calls.

The Windows back-end is written from Microsoft's documentation and has not
been run by this work order's tests (they run on Linux).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Protocol

from .capabilities import ActionResult, ArgSpec, ChoiceArg, IntArg, TextArg
from .config import DesktopConfig
from .memory import MemoryStore
from .router import ActionSpec


# Types `files.open` may hand to the default application. An allow-list:
# programs, scripts, shortcuts, installers, web pages and SVG (both can run
# script in a browser) are refused because they are not listed.
OPENABLE_SUFFIXES = frozenset(
    {
        ".pdf", ".txt", ".md", ".rtf", ".odt", ".ods", ".odp", ".csv", ".epub",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
        ".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac",
        ".mp4", ".mkv", ".webm", ".avi", ".mov",
    }
)
MEDIA_ACTIONS = ("play_pause", "next", "previous")
VOLUME_ACTIONS = ("up", "down", "mute")
MAX_SCANNED_FILES = 50_000
MAX_MATCHES = 200
MAX_LISTED = 10
MAX_TIMERS = 10

# The order the router offers actions in.
ACTION_ORDER = (
    "apps.open",
    "files.open",
    "files.find",
    "media.control",
    "volume.control",
    "system.lock",
    "time.now",
    "timer.set",
    "memory.save",
)


class PlatformError(RuntimeError):
    """A desktop helper failed. The message says what happened, truthfully."""


# Programs NOVA started, for the life of the NOVA process (not of one bridge):
# finished ones are reaped on the next launch, so no zombie processes pile up,
# and a program still running when a bridge is dropped is not orphaned early.
_LAUNCHED: list[subprocess.Popen] = []
_LAUNCHED_LOCK = threading.Lock()


def _keep(child: object) -> None:
    with _LAUNCHED_LOCK:
        _LAUNCHED[:] = [running for running in _LAUNCHED if running.poll() is None]
        if isinstance(child, subprocess.Popen):
            _LAUNCHED.append(child)


class Platform(Protocol):
    name: str

    def launch(self, argv: tuple[str, ...]) -> None: ...

    def open_file(self, path: Path) -> None: ...

    def media(self, action: str) -> None: ...

    def volume(self, action: str) -> None: ...

    def lock(self) -> None: ...


class LinuxPlatform:
    """Linux back-end: xdg-open, playerctl, pactl and loginctl, each with a fixed argv."""

    name = "linux"
    _PLAYERCTL = {"play_pause": "play-pause", "next": "next", "previous": "previous"}

    def __init__(
        self,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self._which = which
        self._run = run
        self._popen = popen

    def _tool(self, program: str) -> str:
        found = self._which(program)
        if not found:
            raise PlatformError(f"{program} is not installed on this computer")
        return found

    def _detach(self, argv: tuple[str, ...]) -> None:
        # The owner's desktop session needs its own environment (DISPLAY,
        # DBUS_SESSION_BUS_ADDRESS), so it is inherited here, unlike the
        # model runner's, which llm.py scrubs.
        try:
            child = self._popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise PlatformError(f"could not start {Path(argv[0]).name}: {error.strerror or error}") from error
        _keep(child)

    def _call(self, argv: tuple[str, ...]) -> None:
        program = Path(argv[0]).name
        try:
            done = self._run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            raise PlatformError(f"{program} did not finish within 10 seconds") from error
        except OSError as error:
            raise PlatformError(f"could not run {program}: {error.strerror or error}") from error
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()[:200]
            raise PlatformError(
                f"{program} exited with status {done.returncode}" + (f": {detail}" if detail else "")
            )

    def launch(self, argv: tuple[str, ...]) -> None:
        self._detach(argv)

    def open_file(self, path: Path) -> None:
        # `path` is absolute, so it cannot be read as an xdg-open option.
        self._detach((self._tool("xdg-open"), str(path)))

    def media(self, action: str) -> None:
        self._call((self._tool("playerctl"), self._PLAYERCTL[action]))

    def volume(self, action: str) -> None:
        pactl = self._tool("pactl")
        argv = {
            "up": (pactl, "set-sink-volume", "@DEFAULT_SINK@", "+5%"),
            "down": (pactl, "set-sink-volume", "@DEFAULT_SINK@", "-5%"),
            "mute": (pactl, "set-sink-mute", "@DEFAULT_SINK@", "toggle"),
        }[action]
        self._call(argv)

    def lock(self) -> None:
        self._call((self._tool("loginctl"), "lock-session"))


class WindowsPlatform:
    """Windows back-end through user32 and the shell's "open" verb.

    UNVERIFIED: written from Microsoft's documentation; no test in this
    repository runs it (CI and this work order's tests run on Linux).
    Virtual-key codes: https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes
    """

    name = "windows"
    _VK = {
        "play_pause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
        "next": 0xB0,  # VK_MEDIA_NEXT_TRACK
        "previous": 0xB1,  # VK_MEDIA_PREV_TRACK
        "mute": 0xAD,  # VK_VOLUME_MUTE
        "down": 0xAE,  # VK_VOLUME_DOWN
        "up": 0xAF,  # VK_VOLUME_UP
    }
    _KEYEVENTF_KEYUP = 0x0002
    _VOLUME_STEPS = 3  # key presses per "up" or "down" request

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def launch(self, argv: tuple[str, ...]) -> None:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            child = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
        except OSError as error:
            raise PlatformError(f"could not start {Path(argv[0]).name}: {error.strerror or error}") from error
        _keep(child)

    def open_file(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # the file type's registered "open" verb
        except OSError as error:
            raise PlatformError(f"could not open {path.name}: {error.strerror or error}") from error

    def _press(self, key: int, times: int = 1) -> None:
        for _ in range(times):
            self._user32.keybd_event(key, 0, 0, 0)
            self._user32.keybd_event(key, 0, self._KEYEVENTF_KEYUP, 0)

    def media(self, action: str) -> None:
        self._press(self._VK[action])

    def volume(self, action: str) -> None:
        self._press(self._VK[action], 1 if action == "mute" else self._VOLUME_STEPS)

    def lock(self) -> None:
        if not self._user32.LockWorkStation():
            raise PlatformError(f"LockWorkStation failed (Windows error {self._ctypes.get_last_error()})")


def host_platform() -> Platform | None:
    """The back-end for this computer, or None where none exists (then the
    platform actions are simply not offered)."""
    if sys.platform.startswith("linux"):
        return LinuxPlatform()
    if sys.platform == "win32":
        return WindowsPlatform()
    return None


def _canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _start_timer(seconds: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()


def print_notice(message: str) -> None:
    print(f"\n[NOVA] {message}", flush=True)


class DesktopBridge:
    """The executor the broker calls for "desktop" capabilities."""

    def __init__(
        self,
        config: DesktopConfig,
        platform: Platform | None,
        *,
        memory: MemoryStore | None = None,
        notify: Callable[[str], None] = print_notice,
        clock: Callable[[], datetime] = _local_now,
        start_timer: Callable[[float, Callable[[], None]], None] = _start_timer,
    ) -> None:
        self.config = config
        self.platform = platform
        self.memory = memory
        self._notify = notify
        self._clock = clock
        self._start_timer = start_timer
        self._roots = tuple(_canonical(folder) for folder in config.folders)
        self._lock = threading.Lock()
        self._active_timers = 0

    # --- what the broker and the router ask -------------------------------

    def available(self) -> frozenset[str]:
        names = {"time.now", "timer.set"}
        if self.memory is not None:
            names.add("memory.save")
        if self._roots:
            names.add("files.find")
        if self.platform is not None:
            names |= {"media.control", "volume.control", "system.lock"}
            if self.config.apps:
                names.add("apps.open")
            if self._roots:
                names.add("files.open")
        return frozenset(names)

    def arg_specs(self, name: str) -> Mapping[str, ArgSpec]:
        specs: dict[str, Mapping[str, ArgSpec]] = {
            "apps.open": {"app": ChoiceArg(tuple(self.config.apps))},
            "files.find": {"query": TextArg(1, 64)},
            "files.open": {"query": TextArg(1, 64)},
            "media.control": {"action": ChoiceArg(MEDIA_ACTIONS)},
            "volume.control": {"action": ChoiceArg(VOLUME_ACTIONS)},
            "system.lock": {},
            "time.now": {},
            "timer.set": {"minutes": IntArg(1, 1440), "label": TextArg(0, 40)},
            "memory.save": {"text": TextArg(1, 300)},
        }
        return specs[name]

    def action_specs(self) -> list[ActionSpec]:
        """The actions the router may offer, in a fixed order."""
        first_app = next(iter(self.config.apps), "music")
        texts: dict[str, tuple[str, str, dict[str, object]]] = {
            "apps.open": ("Open an application by its name.", f"open {first_app}", {"app": first_app}),
            "files.open": (
                "Open one document or media file found by words from its name or folder.",
                "open the file budget 2026",
                {"query": "budget 2026"},
            ),
            "files.find": (
                "List files whose name or folder contains the words.",
                "find my physics notes",
                {"query": "physics notes"},
            ),
            "media.control": ("Play or pause media, or skip a track.", "pause the music", {"action": "play_pause"}),
            "volume.control": ("Change the speaker volume.", "turn the volume up", {"action": "up"}),
            "system.lock": ("Lock the screen.", "lock my computer", {}),
            "time.now": ("Tell the current date and time.", "what time is it", {}),
            "timer.set": (
                "Announce a reminder after some minutes.",
                "remind me in 10 minutes to stretch",
                {"minutes": 10, "label": "stretch"},
            ),
            "memory.save": (
                "Save a note the owner dictates, after the owner confirms it.",
                "remember that my exam is on Monday",
                {"text": "my exam is on Monday"},
            ),
        }
        available = self.available()
        result = []
        for name in ACTION_ORDER:
            if name in available:
                description, words, example = texts[name]
                result.append(ActionSpec(name, description, self.arg_specs(name), words, example))
        return result

    def describe(self, name: str, arguments: Mapping[str, object]) -> str:
        """One line the owner reads before confirming."""
        if name == "apps.open":
            return f'open the application "{arguments["app"]}"'
        if name == "files.find":
            return f'list files matching "{arguments["query"]}"'
        if name == "files.open":
            return f'open the file matching "{arguments["query"]}"'
        if name == "media.control":
            return {
                "play_pause": "play or pause media",
                "next": "skip to the next track",
                "previous": "go back to the previous track",
            }[str(arguments["action"])]
        if name == "volume.control":
            return {
                "up": "turn the volume up",
                "down": "turn the volume down",
                "mute": "mute or unmute the speakers",
            }[str(arguments["action"])]
        if name == "system.lock":
            return "lock the screen"
        if name == "time.now":
            return "tell the time"
        if name == "timer.set":
            label = f' labelled "{arguments["label"]}"' if arguments["label"] else ""
            return f"set a {arguments['minutes']}-minute timer{label}"
        if name == "memory.save":
            return f'save this note: "{arguments["text"]}"'
        raise KeyError(name)

    def run(self, name: str, arguments: dict[str, object]) -> ActionResult:
        handlers: dict[str, Callable[[dict[str, object]], ActionResult]] = {
            "apps.open": self._open_app,
            "files.find": self._find_files,
            "files.open": self._open_file,
            "media.control": self._media,
            "volume.control": self._volume,
            "system.lock": self._lock_screen,
            "time.now": self._time_now,
            "timer.set": self._set_timer,
            "memory.save": self._save_note,
        }
        try:
            return handlers[name](arguments)
        except PlatformError as error:
            return ActionResult(False, f"{name} failed: {error}")

    # --- files: confinement first -----------------------------------------

    def _confined(self, path: str | Path) -> str | None:
        """The resolved path if it lies inside a listed folder, else None."""
        real = _canonical(path)
        for root in self._roots:
            try:
                if os.path.commonpath([real, root]) == root:
                    return real
            except ValueError:  # different drives on Windows
                continue
        return None

    def _search(self, query: str) -> tuple[list[tuple[str, str]], bool]:
        """Files whose folder-and-name text holds every query word.

        Returns (resolved path, display name) pairs and whether the scan was
        cut short. Hidden files and folders are skipped; symlinked folders
        are not entered; every hit must resolve inside a listed folder.
        """
        words = query.casefold().split()
        if not words:
            return [], False
        found: list[tuple[str, str]] = []
        scanned = 0
        for root in self._roots:
            for directory, folders, files in os.walk(root, followlinks=False):
                folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
                for filename in sorted(files):
                    if filename.startswith("."):
                        continue
                    scanned += 1
                    if scanned > MAX_SCANNED_FILES:
                        return found, True
                    full = os.path.join(directory, filename)
                    relative = os.path.relpath(full, root)
                    haystack = relative.replace(os.sep, " ").casefold()
                    if all(word in haystack for word in words):
                        real = self._confined(full)
                        if real is None:
                            continue
                        found.append((real, f"{Path(root).name}/{relative.replace(os.sep, '/')}"))
                        if len(found) >= MAX_MATCHES:
                            return found, True
        return found, False

    @staticmethod
    def _listing(matches: list[tuple[str, str]]) -> str:
        shown = [f"{number}) {display}" for number, (_, display) in enumerate(matches[:MAX_LISTED], start=1)]
        more = len(matches) - MAX_LISTED
        return " ".join(shown) + (f" ...and {more} more" if more > 0 else "")

    def _find_files(self, arguments: dict[str, object]) -> ActionResult:
        query = str(arguments["query"])
        matches, cut_short = self._search(query)
        if not matches:
            note = " (the search stopped early: too many files)" if cut_short else ""
            return ActionResult(True, f'No file in your folders matches "{query}"{note}.')
        count = f"{len(matches)}+" if cut_short else str(len(matches))
        return ActionResult(True, f"Found {count}: {self._listing(matches)}")

    def _open_file(self, arguments: dict[str, object]) -> ActionResult:
        assert self.platform is not None  # files.open is offered only with a platform
        query = str(arguments["query"])
        matches, _ = self._search(query)
        openable = [item for item in matches if Path(item[0]).suffix.casefold() in OPENABLE_SUFFIXES]
        if not openable:
            if matches:
                return ActionResult(
                    False, f'Files match "{query}", but none is a document or media type NOVA may open.'
                )
            return ActionResult(False, f'No document or media file in your folders matches "{query}".')
        if len(openable) > 1:
            exact = [item for item in openable if Path(item[0]).stem.casefold() == query.casefold()]
            if len(exact) != 1:
                return ActionResult(
                    False, f"Several files match: {self._listing(openable)}. Say more of the name or its folder."
                )
            openable = exact
        real, display = openable[0]
        # Check again just before opening: the file may have been replaced by
        # a link since the search. (A swap after this line is not caught.)
        if self._confined(real) != real or not os.path.isfile(real):
            return ActionResult(False, f"{display} changed since the search; nothing was opened.")
        self.platform.open_file(Path(real))
        return ActionResult(True, f"Opened {display}.")

    # --- the rest ----------------------------------------------------------

    def _open_app(self, arguments: dict[str, object]) -> ActionResult:
        assert self.platform is not None
        name = str(arguments["app"])
        argv = self.config.apps[name]
        if not os.path.isfile(argv[0]):
            return ActionResult(False, f'The program for "{name}" was not found at {argv[0]}.')
        self.platform.launch(argv)
        return ActionResult(True, f'Started "{name}".')

    def _media(self, arguments: dict[str, object]) -> ActionResult:
        assert self.platform is not None
        action = str(arguments["action"])
        self.platform.media(action)
        return ActionResult(True, f"Sent {action.replace('_', '/')} to the media player.")

    def _volume(self, arguments: dict[str, object]) -> ActionResult:
        assert self.platform is not None
        action = str(arguments["action"])
        self.platform.volume(action)
        return ActionResult(True, {"up": "Volume up.", "down": "Volume down.", "mute": "Mute toggled."}[action])

    def _lock_screen(self, arguments: dict[str, object]) -> ActionResult:
        assert self.platform is not None
        self.platform.lock()
        return ActionResult(True, "Screen locked.")

    def _time_now(self, arguments: dict[str, object]) -> ActionResult:
        now = self._clock()
        zone = now.tzname() or "local time"
        return ActionResult(True, now.strftime("It is %H:%M on %A %d %B %Y") + f" ({zone}).")

    def _set_timer(self, arguments: dict[str, object]) -> ActionResult:
        minutes = int(arguments["minutes"])  # type: ignore[call-overload]
        label = str(arguments["label"])
        with self._lock:
            if self._active_timers >= MAX_TIMERS:
                return ActionResult(False, f"{MAX_TIMERS} timers are already running.")
            self._active_timers += 1
        message = f"Timer: {label}" if label else f"Timer: {minutes} minute(s) are up."

        def fire() -> None:
            with self._lock:
                self._active_timers -= 1
            self._notify(message)

        self._start_timer(minutes * 60.0, fire)
        named = f' "{label}"' if label else ""
        return ActionResult(True, f"Timer{named} set for {minutes} minute(s). It rings only while NOVA is running.")

    def _save_note(self, arguments: dict[str, object]) -> ActionResult:
        assert self.memory is not None
        memory = self.memory.add(str(arguments["text"]))
        return ActionResult(True, f"Saved note {memory.memory_id}.")
