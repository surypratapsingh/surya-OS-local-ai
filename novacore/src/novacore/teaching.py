"""Work order N4d: NOVA learns from the owner's own commands.

Off unless the owner's configuration says `[learning] enabled = true`
(config.py). A lesson pairs words the owner typed or said to NOVA with what the
owner decided about them:

- "do":   the owner said yes to NOVA's proposal and it ran (source "confirmed"),
          or taught it with `/teach WORDS => CAPABILITY {json}` (source "taught").
- "chat": the owner taught that these words are conversation
          (`/teach WORDS => chat`, source "taught").
- "not":  the owner answered no to NOVA's proposal (source "cancelled").

How the assistant uses lessons is in assistant.py. `memory.save` is never
learned: it always asks anyway, and its words are the owner's private note.

Matching is exact. Words are compared after case folding and after dropping
wake and politeness words at the start and at the end ("nova", "please",
"can you", "thanks"), so "Nova, could you open Spotify please?" and "open
spotify" are one lesson, while "open spotify" and "open the spotify" are two.
No similarity is guessed: a lesson applies to its own words and to nothing else.

Lessons live in `state_dir/lessons.jsonl`, a hash chain with a tail anchor in
the format of the memory log (chainlog.py; novacore/SCOPE.md, "Log integrity").
Every read verifies the whole log and raises at the first bad line, so a
tampered log is refused, never used. Nothing here sends a lesson anywhere;
`training_lines` turns lessons into training lines only when the owner runs
`--export-lessons`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import unicodedata
from uuid import uuid4

from . import chainlog
from . import intent
from .capabilities import CATALOGUE


# verdict -> the sources it may come from
VERDICTS: dict[str, tuple[str, ...]] = {
    "do": ("confirmed", "taught"),
    "chat": ("taught",),
    "not": ("cancelled",),
}
NEVER_LEARNED = frozenset({"memory.save"})
MAX_TEXT = 500
FIELDS = ("lesson_id", "created_at", "text", "verdict", "capability", "arguments", "source")

# Dropped only at the start or the end of the words, never in the middle:
# "play i love you" keeps its "you".
LEADING_WORDS = frozenset({"nova", "hey", "hi", "hello", "ok", "okay", "please", "pls", "plz", "kindly", "just"})
LEADING_PAIRS = frozenset({("can", "you"), ("could", "you"), ("would", "you"), ("will", "you")})
TRAILING_WORDS = frozenset({"nova", "please", "pls", "plz", "thanks"})
TRAILING_PAIRS = frozenset({("thank", "you")})

def _words(text: str) -> list[str]:
    """Runs of letters, marks and digits in any script (Unicode categories L, M, N),
    after NFKC normalisation and case folding. Marks count, so Devanagari vowel
    signs stay inside their word."""
    words: list[str] = []
    current: list[str] = []
    for character in unicodedata.normalize("NFKC", text).casefold():
        if unicodedata.category(character)[0] in "LMN":
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def key(text: str) -> str:
    """The words a lesson is matched on, with wake and politeness words removed
    from both ends."""
    words = _words(text)
    start, end = 0, len(words)
    while start < end:
        if words[start] in LEADING_WORDS:
            start += 1
        elif end - start >= 2 and (words[start], words[start + 1]) in LEADING_PAIRS:
            start += 2
        else:
            break
    while end > start:
        if words[end - 1] in TRAILING_WORDS:
            end -= 1
        elif end - start >= 2 and (words[end - 2], words[end - 1]) in TRAILING_PAIRS:
            end -= 2
        else:
            break
    return " ".join(words[start:end])


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    created_at: str
    text: str
    verdict: str
    capability: str | None
    arguments: dict | None
    source: str
    chain_hash: str


def _problem(fields: Mapping[str, object]) -> str | None:
    """Why these fields are not a valid lesson, or None."""
    text = fields["text"]
    if not isinstance(text, str) or not text.strip():
        return "the words are empty"
    if len(text) > MAX_TEXT:
        return f"the words are longer than {MAX_TEXT} characters"
    if not key(text):
        return "only wake or politeness words; nothing to learn"
    verdict, source = fields["verdict"], fields["source"]
    if verdict not in VERDICTS:
        return f"unknown verdict {verdict!r}"
    if source not in VERDICTS[verdict]:
        return f"a {verdict!r} lesson cannot come from {source!r}"
    capability, arguments = fields["capability"], fields["arguments"]
    if verdict == "chat":
        if capability is not None or arguments is not None:
            return "a chat lesson has no capability or arguments"
        return None
    if not isinstance(capability, str) or capability not in CATALOGUE:
        return f"unknown capability {capability!r}"
    if capability in NEVER_LEARNED:
        return f"{capability} is never learned"
    if not isinstance(arguments, dict):
        return "arguments must be a JSON object"
    try:
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        return f"arguments are not plain JSON: {error}"
    return None


class TeachingStore:
    """The owner's lessons: append-only, chained, anchored (see the module docstring)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Lesson]:
        """Every lesson, verified. Raises ValueError on any break, missing or
        disagreeing anchor included."""
        lessons, last_line = self._read_chain() if self.path.exists() else ([], 0)
        head = lessons[-1].chain_hash if lessons else chainlog.GENESIS
        chainlog.check_tail(self.path, len(lessons), head, last_line)
        return lessons

    def lookup(self, text: str) -> Lesson | None:
        """The latest lesson for exactly these words (after `key`), or None."""
        wanted = key(text)
        if not wanted:
            return None
        found = None
        for lesson in self.all():
            if key(lesson.text) == wanted:
                found = lesson
        return found

    def add(
        self,
        text: str,
        verdict: str,
        capability: str | None = None,
        arguments: Mapping[str, object] | None = None,
        *,
        source: str,
    ) -> Lesson:
        fields: dict[str, object] = {
            "lesson_id": str(uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "text": " ".join(text.split()),
            "verdict": verdict,
            "capability": capability,
            "arguments": dict(arguments) if arguments is not None else None,
            "source": source,
        }
        problem = _problem(fields)
        if problem is not None:
            raise ValueError(f"lesson not saved: {problem}")
        # Raises on a broken log: never extend a chain that does not verify.
        existing = self.all()
        previous = existing[-1].chain_hash if existing else chainlog.GENESIS
        chain_hash = chainlog.chain_hash(previous, fields)
        lesson = Lesson(**fields, chain_hash=chain_hash)  # type: ignore[arg-type]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(lesson), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        # A crash here leaves the anchor one record behind; the next read then
        # fails closed (novacore/SCOPE.md, "Log integrity").
        chainlog.write_anchor(self.path, len(existing) + 1, chain_hash)
        return lesson

    def anchor(self) -> None:
        """Recovery after a crash between the append and the anchor write: the
        whole chain must verify first, and an existing anchor is never replaced."""
        lessons, _ = self._read_chain() if self.path.exists() else ([], 0)
        chainlog.adopt(self.path, len(lessons), lessons[-1].chain_hash if lessons else chainlog.GENESIS)

    def _read_chain(self) -> tuple[list[Lesson], int]:
        lessons: list[Lesson] = []
        previous = chainlog.GENESIS
        last_line = 0
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            where = f"{self.path}:{number}"
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid lesson record at {where}") from error
            if not isinstance(raw, dict) or set(raw) != set(FIELDS) | {"chain_hash"}:
                raise ValueError(f"invalid lesson record at {where}")
            fields = {name: raw[name] for name in FIELDS}
            problem = _problem(fields)
            if problem is not None:
                raise ValueError(f"invalid lesson record at {where}: {problem}")
            if chainlog.chain_hash(previous, fields) != raw["chain_hash"]:
                raise ValueError(
                    f"lesson chain hash mismatch at {where}; a lesson may have been "
                    f"deleted, reordered or changed"
                )
            previous = raw["chain_hash"]
            lessons.append(Lesson(**raw))
            last_line = number
        return lessons, last_line


# --- retraining lines (N4d item 3) -------------------------------------------------

def _intent_name(capability: str, arguments: Mapping[str, object]) -> str | None:
    for name, (mapped, fixed) in intent.ACTION_OF_INTENT.items():
        if mapped != capability:
            continue
        if fixed is None or fixed in (arguments.get("action"), arguments.get("format")):
            return name
    return None


def _find(words: Sequence[str], value: object, taken: set[int]) -> tuple[int, int] | None:
    """The first place `value`'s words appear, in order, without touching `taken`."""
    wanted = [t.text for t in intent.tokens(str(value))]
    if not wanted:
        return None
    size = len(wanted)
    for first in range(len(words) - size + 1):
        if list(words[first:first + size]) == wanted and not taken & set(range(first, first + size)):
            return first, first + size - 1
    return None


def _app_span(words: Sequence[str], app: object, taken: set[int]) -> tuple[int, int] | None:
    """The shortest, then first, words the runtime would read as this app (intent.match_app)."""
    for size in range(1, len(words) + 1):
        for first in range(len(words) - size + 1):
            span = set(range(first, first + size))
            if not taken & span and intent.match_app(list(words[first:first + size]), [str(app)]) == app:
                return first, first + size - 1
    return None


def _duration_span(words: Sequence[str], minutes: object, taken: set[int]) -> tuple[int, int] | None:
    """Words that intent.parse_minutes reads as `minutes`: a span ending in a unit
    ("10 minutes", "two hours") or a fixed phrase ("half an hour") first, the
    longest such, then the first; a bare number ("10") only if there is none."""
    units = intent.MINUTE_UNITS | intent.HOUR_UNITS
    best: tuple[tuple[bool, int], tuple[int, int]] | None = None
    for first in range(len(words)):
        for last in range(first, min(len(words), first + 6)):
            if taken & set(range(first, last + 1)):
                continue
            phrase = " ".join(words[first:last + 1])
            if intent.parse_minutes(phrase) != minutes:
                continue
            with_unit = words[last] in units or phrase in intent.FIXED_DURATIONS
            bare_number = first == last and (words[first].isdigit() or words[first] in intent.ONES
                                             or words[first] in intent.TENS)
            if not with_unit and not bare_number:
                continue  # "a" alone reads as 1; it is not a duration span
            rank = (with_unit, last - first)
            if best is None or rank > best[0]:
                best = (rank, (first, last))
    return best[1] if best else None


def training_line(lesson: Lesson) -> tuple[str | None, str]:
    """(line, "") for a lesson that becomes `intent<TAB>text<TAB>tags`, the format of
    intent/data/train.tsv (intent/README.md); (None, reason) for one that does not."""
    if lesson.verdict == "not":
        return None, "a \"not\" lesson says only what the words are not"
    words = [t.text for t in intent.tokens(lesson.text)]
    if not words:
        return None, "no a-z or 0-9 words for the command model to read"
    flat = " ".join(lesson.text.split())  # no tab or newline; the tokens are unchanged
    if lesson.verdict == "chat":
        return f"chat\t{flat}\t{' '.join(['O'] * len(words))}", ""
    capability, arguments = str(lesson.capability), lesson.arguments or {}
    name = _intent_name(capability, arguments)
    if name is None:
        return None, f"the command model has no intent for {capability} {arguments}"

    taken: set[int] = set()
    spans: dict[str, tuple[int, int]] = {}

    def place(slot: str, span: tuple[int, int] | None, value: object) -> str | None:
        if span is None:
            return f"the words do not contain the {slot} {value!r}"
        spans[slot] = span
        taken.update(range(span[0], span[1] + 1))
        return None

    problem: str | None = None
    if capability == "apps.open":
        problem = place("app", _app_span(words, arguments.get("app"), taken), arguments.get("app"))
    elif capability in ("files.find", "files.open", "music.play"):
        problem = place("query", _find(words, arguments.get("query"), taken), arguments.get("query"))
    elif capability == "timer.set":
        minutes = arguments.get("minutes")
        problem = place("num", _duration_span(words, minutes, taken), minutes)
        label = arguments.get("label")
        if problem is None and label:
            problem = place("label", _find(words, label, taken), label)
    elif capability == "docs.create":
        title = arguments.get("title")
        found = _find(words, title, taken)
        if found is not None or title != "Untitled":  # no title said reads as "Untitled"
            problem = place("title", found, title)
        body = arguments.get("text")
        if problem is None and body:
            problem = place("text", _find(words, body, taken), body)
    if problem is not None:
        return None, problem

    tags = ["O"] * len(words)
    for slot, (first, last) in spans.items():
        tags[first] = f"B-{slot}"
        for index in range(first + 1, last + 1):
            tags[index] = f"I-{slot}"
    return f"{name}\t{flat}\t{' '.join(tags)}", ""


def training_lines(lessons: Sequence[Lesson]) -> tuple[list[str], list[tuple[Lesson, str]]]:
    """The training lines, and each lesson left out with the reason."""
    lines: list[str] = []
    left_out: list[tuple[Lesson, str]] = []
    for lesson in lessons:
        line, reason = training_line(lesson)
        if line is None:
            left_out.append((lesson, reason))
        else:
            lines.append(line)
    return lines, left_out
