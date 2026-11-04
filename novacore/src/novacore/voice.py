"""Work order N2: always-on voice, offline, with NovaCore never touching audio.

A *listener* program owns the microphone and prints what it recognised, one
utterance per line (Windows' built-in System.Speech, or whisper.cpp). NovaCore
reads only those text lines:

- A line that starts with a wake word ("nova", "hey nova", "ok nova") is a
  command: the wake word is removed and the rest goes to the assistant.
- While an action waits for confirmation, a line that is only "yes" or "no"
  (case and punctuation ignored, assistant.answer_word) is the answer, with or
  without the wake word. Any other line without the wake word is dropped, and
  the offer keeps waiting (N2b). The offer expires `confirm_seconds` after NOVA
  finished saying it; after that, nothing confirms it.
- Every other line is dropped at once. It is not logged, stored or passed on.

A *speaker* program (Windows System.Speech, espeak-ng, Piper) gets each reply
on stdin and says it. Both are started without a shell, with fixed argv from
the owner's configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import re
import subprocess
import time

from .assistant import answer_word

DEFAULT_WAKE_WORDS = ("nova", "hey nova", "ok nova", "okay nova", "hi nova")
_WORD = re.compile(r"[a-z0-9']+")


def strip_wake_word(line: str, wake_words: Sequence[str]) -> str | None:
    """The command after a leading wake word, or None if the line has none.

    Compares words, not characters: "Nova, open Spotify" -> "open Spotify";
    "novak said" -> None. The command keeps the line's original text."""
    spoken = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(line.lower())]
    for wake in sorted(wake_words, key=lambda w: -len(w.split())):
        wake_tokens = wake.lower().split()
        if [w for w, _, _ in spoken[:len(wake_tokens)]] == wake_tokens:
            end = spoken[len(wake_tokens) - 1][2]
            return line[end:].lstrip(" ,.:;!?-").strip()
    return None


class VoiceLoop:
    def __init__(
        self,
        handle: Callable[[str], str],
        waiting: Callable[[], bool],
        listener: Sequence[str],
        speaker: Sequence[str] | None = None,
        wake_words: Sequence[str] = DEFAULT_WAKE_WORDS,
        show: Callable[[str], None] = print,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        *,
        expire: Callable[[], None] | None = None,
        confirm_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.handle = handle
        self.waiting = waiting
        self.listener = tuple(listener)
        self.speaker = tuple(speaker) if speaker else None
        self.wake_words = tuple(wake_words)
        self.show = show
        self._popen = popen
        self._run = run
        self._speaker_broken = False
        # N2b: `expire` cancels the waiting offer (the CLI clears the assistant's
        # pending action). Without it an offer never expires: tests only.
        self._expire = expire
        self.confirm_seconds = confirm_seconds
        self._clock = clock or time.monotonic
        self._offer_since: float | None = None

    def speak(self, text: str) -> None:
        if not self.speaker or self._speaker_broken:
            return
        try:
            done = self._run(list(self.speaker), input=text.encode("utf-8"), capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._speaker_broken = True
            self.show(f"[voice] the speaker program failed ({error}); replies are shown, not spoken.")
            return
        if done.returncode != 0:
            self._speaker_broken = True
            self.show(f"[voice] the speaker program exited with status {done.returncode}; replies are shown, not spoken.")

    def _note_offer(self, restart: bool = False) -> None:
        """Start the offer's clock when an offer is waiting; `restart` once it has been said."""
        if not self.waiting():
            self._offer_since = None
        elif self._offer_since is None or restart:
            self._offer_since = self._clock()

    def _expire_old_offer(self) -> None:
        if not self.waiting():
            self._offer_since = None
            return
        if self._offer_since is None:
            self._offer_since = self._clock()  # an offer made outside this loop: count from now
            return
        if self._expire is not None and self._clock() - self._offer_since > self.confirm_seconds:
            self._expire()
            self._offer_since = None
            self.show(f"[voice] the offer waited more than {self.confirm_seconds:g} s for yes or no; "
                      f"it expired and nothing was done.")

    def step(self, line: str) -> str | None:
        """Handle one recognised line; return the reply, or None if it was not for NOVA."""
        words = " ".join(line.split())
        if not words:
            return None
        self._expire_old_offer()
        command = strip_wake_word(words, self.wake_words)
        if command is None:
            if not self.waiting():
                return None  # not addressed to NOVA: dropped, never stored
            if answer_word(words) is None:
                return None  # N2b: while an offer waits, only yes or no counts without the wake word
            command = words  # the answer to the waiting offer
        if not command:
            return "Yes?"
        reply = self.handle(command)
        self._note_offer()
        return reply

    def run(self) -> int:
        try:
            process = self._popen(
                list(self.listener),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            self.show(f"[voice] could not start the listener program: {error}")
            return 2
        self.show("[voice] listening. Say \"Nova\" and a command.")
        assert process.stdout is not None
        with process.stdout:  # closed when the listener ends: no leaked pipe
            for line in process.stdout:
                reply = self.step(line)
                if reply is None:
                    continue
                self.show(f"nova> {reply}")
                self.speak(reply)
                self._note_offer(restart=True)  # the offer's time starts once it has been said
        status = process.wait()
        self.show(f"[voice] the listener stopped (status {status}).")
        return 0 if status == 0 else 1
