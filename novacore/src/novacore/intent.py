"""Work order N4: NOVA's own command model, run in plain Python.

This is the second implementation of the model specified in intent/README.md
(the first is the Rust trainer in intent/). It imports only the standard
library, so it runs as a plain script. It is also an N1 router runner:

    python -I novacore/src/novacore/intent.py --model intent/model/nova-intent.bin SCHEMA_FILE < router-prompt

(-I: isolated mode, so the script's folder cannot shadow a standard module,
and no PYTHONPATH is needed under llm.py's scrubbed environment.)

reads the router prompt on stdin, takes the owner's words from its last
"Owner: " line, and prints exactly one JSON answer: {"route": "chat"} or one
action that SCHEMA_FILE offers. Whatever it prints, N1's strict parser and the
broker check again (novacore/SCOPE.md). Any doubt, error or unreadable input
gives chat: a missed action is safe, a wrong one is not.

Evaluation (docs/work-orders.md N4):

    python -I novacore/src/novacore/intent.py --model M --select-threshold intent/data/dev.tsv
    python -I novacore/src/novacore/intent.py --model M --evaluate intent/data/test.jsonl --apps intent/data/test-config.json
"""

from __future__ import annotations

import argparse
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from operator import mul
from pathlib import Path
import re
import sys

INTENTS = (
    "chat", "apps.open", "files.find", "files.open", "media.play_pause", "media.next",
    "media.previous", "volume.up", "volume.down", "volume.mute", "system.lock", "time.now",
    "timer.set", "memory.save",
)
TAGS = ("O", "B-app", "I-app", "B-query", "I-query", "B-num", "I-num", "B-label", "I-label", "B-text", "I-text")
MAGIC = b"NOVAINT1"
CLS_FEATURE = "c:cls"
LN_EPS = 1e-5
GELU_C = math.sqrt(2.0 / math.pi)

# Chosen on intent/data/dev.tsv by the rule in intent/README.md ("Decoding,
# fixed before training"); the run that chose it is in docs/logs/n4-evidence.log.
THRESHOLD = 0.5

ACTION_OF_INTENT = {
    "apps.open": ("apps.open", None),
    "files.find": ("files.find", None),
    "files.open": ("files.open", None),
    "media.play_pause": ("media.control", "play_pause"),
    "media.next": ("media.control", "next"),
    "media.previous": ("media.control", "previous"),
    "volume.up": ("volume.control", "up"),
    "volume.down": ("volume.control", "down"),
    "volume.mute": ("volume.control", "mute"),
    "system.lock": ("system.lock", None),
    "time.now": ("time.now", None),
    "timer.set": ("timer.set", None),
    "memory.save": ("memory.save", None),
}


# --- tokens and features (intent/README.md, "Tokens", "Features") -----------------

def fnv1a32(data: bytes) -> int:
    value = 0x811C9DC5
    for byte in data:
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


def tokens(text: str) -> list[Token]:
    """ASCII lower-case, then maximal runs of a-z and 0-9, with their places in `text`."""
    out: list[Token] = []
    start = -1
    chars: list[str] = []
    for index, character in enumerate(text):
        lowered = chr(ord(character) + 32) if "A" <= character <= "Z" else character
        if "a" <= lowered <= "z" or "0" <= lowered <= "9":
            if start < 0:
                start = index
            chars.append(lowered)
        elif start >= 0:
            out.append(Token("".join(chars), start, index))
            start, chars = -1, []
    if start >= 0:
        out.append(Token("".join(chars), start, len(text)))
    return out


def token_features(word: str) -> list[str]:
    marked = f"<{word}>"
    return [f"w:{word}"] + [f"t:{marked[i:i + 3]}" for i in range(len(marked) - 2)]


# --- the model ------------------------------------------------------------------

class ModelError(ValueError):
    pass


def _columns(flat: Sequence[float], at: int, n: int, m: int) -> list[list[float]]:
    """Columns of the n x m row-major matrix starting at `at`."""
    return [[flat[at + i * m + j] for i in range(n)] for j in range(m)]


class Model:
    """Loads a NOVAINT1 file (intent/README.md, "Layout and file format")."""

    def __init__(self, path: Path) -> None:
        data = Path(path).read_bytes()
        if len(data) < 8 + 36 or data[:8] != MAGIC:
            raise ModelError(f"{path}: not a NOVAINT1 model file")
        fields = array("I")
        fields.frombytes(data[8:44])
        if sys.byteorder != "little":
            fields.byteswap()
        (self.buckets, self.d, self.heads, self.ff, self.layers, self.max_len,
         self.intents, self.tags, count) = fields
        if self.d % self.heads or (self.intents, self.tags) != (len(INTENTS), len(TAGS)):
            raise ModelError(f"{path}: shape does not match this runtime")
        d, ff = self.d, self.ff
        per_layer = 2 * d + d * 3 * d + 3 * d + d * d + d + 2 * d + d * ff + ff + ff * d + d
        expected = (self.buckets * d + self.max_len * d + self.layers * per_layer
                    + 2 * d + d * self.intents + self.intents + d * self.tags + self.tags)
        if count != expected or len(data) != 44 + 4 * count:
            raise ModelError(f"{path}: {count} parameters in a {len(data)}-byte file; expected {expected}")
        values = array("f")
        values.frombytes(data[44:])
        if sys.byteorder != "little":
            values.byteswap()
        flat = values.tolist()
        if not all(math.isfinite(v) for v in flat):
            raise ModelError(f"{path}: a parameter is not finite")

        at = 0

        def take(size: int) -> int:
            nonlocal at
            start = at
            at += size
            return start

        self.flat = flat
        self.emb = take(self.buckets * d)
        self.pos = take(self.max_len * d)
        self.layer_params = []
        for _ in range(self.layers):
            ln1_g, ln1_b = take(d), take(d)
            wqkv, bqkv = take(d * 3 * d), take(3 * d)
            wo, bo = take(d * d), take(d)
            ln2_g, ln2_b = take(d), take(d)
            w1, b1 = take(d * ff), take(ff)
            w2, b2 = take(ff * d), take(d)
            self.layer_params.append({
                "ln1": (flat[ln1_g:ln1_g + d], flat[ln1_b:ln1_b + d]),
                "qkv": (_columns(flat, wqkv, d, 3 * d), flat[bqkv:bqkv + 3 * d]),
                "o": (_columns(flat, wo, d, d), flat[bo:bo + d]),
                "ln2": (flat[ln2_g:ln2_g + d], flat[ln2_b:ln2_b + d]),
                "w1": (_columns(flat, w1, d, ff), flat[b1:b1 + ff]),
                "w2": (_columns(flat, w2, ff, d), flat[b2:b2 + d]),
            })
        lnf_g, lnf_b = take(d), take(d)
        wi, bi = take(d * self.intents), take(self.intents)
        ws, bs = take(d * self.tags), take(self.tags)
        self.lnf = (flat[lnf_g:lnf_g + d], flat[lnf_b:lnf_b + d])
        self.head_intent = (_columns(flat, wi, d, self.intents), flat[bi:bi + self.intents])
        self.head_tags = (_columns(flat, ws, d, self.tags), flat[bs:bs + self.tags])
        assert at == count

    def encode(self, text: str) -> list[list[int]] | None:
        words = tokens(text)
        if len(words) + 1 > self.max_len:
            return None

        def bucket(feature: str) -> int:
            return fnv1a32(feature.encode("utf-8")) % self.buckets

        return [[bucket(CLS_FEATURE)]] + [[bucket(f) for f in token_features(w.text)] for w in words]

    def forward(self, positions: list[list[int]]) -> tuple[list[float], list[list[float]]]:
        """Intent probabilities, and tag probabilities for each token position."""
        d, flat = self.d, self.flat
        x = []
        for index, features in enumerate(positions):
            row = [0.0] * d
            for f in features:
                start = self.emb + f * d
                row = [a + b for a, b in zip(row, flat[start:start + d])]
            n = len(features)
            start = self.pos + index * d
            x.append([a / n + b for a, b in zip(row, flat[start:start + d])])
        for layer in self.layer_params:
            a = [_layernorm(row, *layer["ln1"]) for row in x]
            qkv = [_linear(row, *layer["qkv"]) for row in a]
            o = _attention(qkv, d, self.heads)
            x = [[u + v for u, v in zip(row, _linear(orow, *layer["o"]))] for row, orow in zip(x, o)]
            m = [_layernorm(row, *layer["ln2"]) for row in x]
            g = [[_gelu(v) for v in _linear(row, *layer["w1"])] for row in m]
            x = [[u + v for u, v in zip(row, _linear(grow, *layer["w2"]))] for row, grow in zip(x, g)]
        xf = [_layernorm(row, *self.lnf) for row in x]
        intent = _softmax(_linear(xf[0], *self.head_intent))
        tags = [_softmax(_linear(row, *self.head_tags)) for row in xf[1:]]
        return intent, tags


def _linear(x: list[float], columns: list[list[float]], bias: list[float]) -> list[float]:
    return [sum(map(mul, x, column)) + b for column, b in zip(columns, bias)]


def _layernorm(x: list[float], gain: list[float], bias: list[float]) -> list[float]:
    d = len(x)
    mean = sum(x) / d
    var = sum((v - mean) * (v - mean) for v in x) / d
    s = 1.0 / math.sqrt(var + LN_EPS)
    return [(v - mean) * s * g + b for v, g, b in zip(x, gain, bias)]


def _gelu(v: float) -> float:
    return 0.5 * v * (1.0 + math.tanh(GELU_C * (v + 0.044715 * v * v * v)))


def _softmax(values: list[float]) -> list[float]:
    top = max(values)
    exps = [math.exp(v - top) for v in values]
    total = sum(exps)
    return [e / total for e in exps]


def _attention(qkv: list[list[float]], d: int, heads: int) -> list[list[float]]:
    dh = d // heads
    scale = 1.0 / math.sqrt(dh)
    t = len(qkv)
    out = [[0.0] * d for _ in range(t)]
    for h in range(heads):
        q = [row[h * dh:(h + 1) * dh] for row in qkv]
        k = [row[d + h * dh:d + (h + 1) * dh] for row in qkv]
        v = [row[2 * d + h * dh:2 * d + (h + 1) * dh] for row in qkv]
        for i in range(t):
            weights = _softmax([sum(map(mul, q[i], k[s])) * scale for s in range(t)])
            for j in range(dh):
                out[i][h * dh + j] = sum(w * v[s][j] for s, w in enumerate(weights))
    return out


# --- from probabilities to one answer (intent/README.md, "Decoding") ---------------

ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
MINUTE_UNITS = {"minute", "minutes", "min", "mins", "m"}
HOUR_UNITS = {"hour", "hours", "hr", "hrs", "h"}
FIXED_DURATIONS = {
    "half an hour": 30, "half hour": 30, "an hour and a half": 90, "one and a half hours": 90,
    "a quarter of an hour": 15, "quarter of an hour": 15, "quarter hour": 15,
}


def parse_minutes(span: str) -> int | None:
    """Minutes from a spoken duration; None if it cannot be read (then: chat)."""
    words = [t.text for t in tokens(span)]
    phrase = " ".join(words)
    if phrase in FIXED_DURATIONS:
        return FIXED_DURATIONS[phrase]
    if not words:
        return None
    index, number = 0, None
    if words[0].isdigit():
        number, index = int(words[0]), 1
    elif words[0] in ("a", "an"):
        number, index = 1, 1
    elif words[0] in TENS:
        number, index = TENS[words[0]], 1
        if len(words) > 1 and words[1] in ONES and ONES[words[1]] < 10:
            number, index = number + ONES[words[1]], 2
    elif words[0] in ONES:
        number, index = ONES[words[0]], 1
    if number is None:
        return None
    rest = words[index:]
    if not rest or rest[0] in MINUTE_UNITS:
        minutes = number
    elif rest[0] in HOUR_UNITS:
        minutes = number * 60
    else:
        return None
    if len(rest) > 1:
        return None
    return minutes if 1 <= minutes <= 1440 else None


def first_spans(words: list[Token], tags: list[int]) -> dict[str, tuple[int, int]]:
    """slot -> (first token, last token) of its first span."""
    spans: dict[str, tuple[int, int]] = {}
    current: str | None = None
    for index, tag_id in enumerate(tags):
        tag = TAGS[tag_id]
        if tag == "O":
            current = None
            continue
        kind, slot = tag.split("-", 1)
        if current == slot and kind == "I":
            spans[slot] = (spans[slot][0], index)
            continue
        current = None
        if slot not in spans:
            spans[slot] = (index, index)
            current = slot
    return spans


APP_SUFFIXES = {"app", "application", "program"}


def match_app(span_words: list[str], offered: Sequence[str] | None) -> str | None:
    """The one offered app the span names, or None. `offered=None`: any non-empty span."""
    if not span_words:
        return None
    if offered is None:
        return " ".join(span_words)
    trimmed = span_words[:-1] if len(span_words) > 1 and span_words[-1] in APP_SUFFIXES else span_words
    wanted = {"".join(span_words), "".join(trimmed)}
    hits = [name for name in offered if re.sub(r"[^a-z0-9]", "", name.lower()) in wanted]
    return hits[0] if len(hits) == 1 else None


@dataclass(frozen=True)
class Offer:
    """What the router schema offers: capability names, and the app names for apps.open.
    `apps=None` means any app name is accepted (used only for threshold selection)."""

    capabilities: frozenset[str]
    apps: tuple[str, ...] | None


ALL_CAPABILITIES = frozenset(name for name, _ in ACTION_OF_INTENT.values())


def offer_from_schema(schema: Mapping[str, object]) -> Offer:
    names: set[str] = set()
    apps: tuple[str, ...] = ()
    for branch in schema["oneOf"][1:]:  # type: ignore[index]
        properties = branch["properties"]
        name = properties["capability"]["const"]
        names.add(name)
        if name == "apps.open":
            apps = tuple(properties["args"]["properties"]["app"]["enum"])
    return Offer(frozenset(names), apps)


@dataclass(frozen=True)
class Reading:
    """The model's output for one line, before any threshold or offer is applied."""

    intent_probs: list[float]
    tag_probs: list[list[float]]


def read(model: Model, text: str) -> Reading | None:
    positions = model.encode(text)
    if positions is None:
        return None
    return Reading(*model.forward(positions))


def decide(model: Model, text: str, offer: Offer, threshold: float = THRESHOLD) -> dict[str, object]:
    """One answer for the owner's words: {"route": "chat"} or one offered action."""
    return answer(read(model, text), text, offer, threshold)


def answer(reading: Reading | None, text: str, offer: Offer, threshold: float = THRESHOLD) -> dict[str, object]:
    chat: dict[str, object] = {"route": "chat"}
    if reading is None:
        return chat
    intent_probs, tag_probs = reading.intent_probs, reading.tag_probs
    best = max(range(len(intent_probs)), key=intent_probs.__getitem__)
    intent = INTENTS[best]
    if intent == "chat" or intent_probs[best] < threshold:
        return chat
    capability, fixed = ACTION_OF_INTENT[intent]
    if capability not in offer.capabilities:
        return chat
    words = tokens(text)
    tags = [max(range(len(row)), key=row.__getitem__) for row in tag_probs]
    spans = first_spans(words, tags)

    def span_text(slot: str) -> str:
        if slot not in spans:
            return ""
        first, last = spans[slot]
        return text[words[first].start:words[last].end]

    args: dict[str, object]
    if fixed is not None:
        args = {"action": fixed}
    elif capability == "apps.open":
        first, last = spans.get("app", (0, -1))
        app = match_app([w.text for w in words[first:last + 1]], offer.apps)
        if app is None:
            return chat
        args = {"app": app}
    elif capability in ("files.find", "files.open"):
        query = span_text("query")
        if not 1 <= len(query) <= 64:
            return chat
        args = {"query": query}
    elif capability == "timer.set":
        minutes = parse_minutes(span_text("num"))
        label = span_text("label")
        if minutes is None or len(label) > 40:
            return chat
        args = {"minutes": minutes, "label": label}
    elif capability == "memory.save":
        note = span_text("text")
        if not 1 <= len(note) <= 300:
            return chat
        args = {"text": note}
    else:
        args = {}
    return {"route": "action", "capability": capability, "args": args}


# --- the router runner --------------------------------------------------------------

def owner_line(prompt: str) -> str | None:
    lines = [line for line in prompt.splitlines() if line.startswith("Owner: ")]
    return lines[-1][len("Owner: "):] if lines else None


def run_router(model_path: Path, schema_path: Path, prompt: str) -> str:
    try:
        words = owner_line(prompt)
        if words is None:
            return json.dumps({"route": "chat"})
        offer = offer_from_schema(json.loads(schema_path.read_text(encoding="utf-8")))
        return json.dumps(decide(Model(model_path), words, offer), ensure_ascii=False)
    except Exception as error:  # fail closed: any fault is chat
        print(f"novacore.intent: {type(error).__name__}: {error}", file=sys.stderr)
        return json.dumps({"route": "chat"})


# --- evaluation (docs/work-orders.md N4) ----------------------------------------------

LEADING = {"my", "the", "a", "an", "to", "for", "that"}


def normalise(value: object) -> object:
    if not isinstance(value, str):
        return value
    words = " ".join(value.lower().split()).strip(" .,!?;:'\"").split()
    while words and words[0] in LEADING:
        words = words[1:]
    return " ".join(words)


def same_answer(expected: Mapping[str, object], got: Mapping[str, object]) -> bool:
    if expected["route"] != got["route"]:
        return False
    if expected["route"] == "chat":
        return True
    if expected["capability"] != got["capability"]:
        return False
    want, have = expected["args"], got["args"]
    return set(want) == set(have) and all(normalise(want[k]) == normalise(have[k]) for k in want)  # type: ignore[index]


def evaluate(model: Model, rows: list[dict], apps: Sequence[str], threshold: float = THRESHOLD) -> dict[str, object]:
    offer = Offer(ALL_CAPABILITIES, tuple(apps))
    result = {"lines": len(rows), "action_lines": 0, "chat_lines": 0, "right_actions": 0,
              "missed_actions": 0, "wrong_actions": 0, "false_actions": 0, "failures": []}
    for row in rows:
        got = decide(model, row["text"], offer, threshold)
        expected = {k: row[k] for k in ("route", "capability", "args") if k in row}
        if expected["route"] == "chat":
            result["chat_lines"] += 1
            if got["route"] != "chat":
                result["false_actions"] += 1
                result["failures"].append(("FALSE ACTION", row["text"], got))
            continue
        result["action_lines"] += 1
        if same_answer(expected, got):
            result["right_actions"] += 1
        elif got["route"] == "chat":
            result["missed_actions"] += 1
            result["failures"].append(("missed", row["text"], got))
        else:
            result["wrong_actions"] += 1
            result["failures"].append(("WRONG ACTION", row["text"], got))
    return result


def dev_false_actions(model: Model, dev_path: Path, thresholds: Sequence[float]) -> tuple[dict[float, int], int]:
    """Per threshold, the chat lines of dev.tsv read as an action (every action and
    any app name offered); and the number of chat lines. The model runs once per line."""
    offer = Offer(ALL_CAPABILITIES, None)
    false_actions = {threshold: 0 for threshold in thresholds}
    chat_lines = 0
    for line in dev_path.read_text(encoding="utf-8").splitlines():
        intent, text, _ = line.split("\t")
        if intent != "chat":
            continue
        chat_lines += 1
        reading = read(model, text)
        for threshold in thresholds:
            if answer(reading, text, offer, threshold)["route"] != "chat":
                false_actions[threshold] += 1
    return false_actions, chat_lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NOVA's own command model (N4)")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("schema", nargs="?", type=Path, help="router mode: the {schema_file} path")
    parser.add_argument("--select-threshold", type=Path, metavar="DEV_TSV")
    parser.add_argument("--evaluate", type=Path, metavar="TEST_JSONL")
    parser.add_argument("--apps", type=Path, metavar="TEST_CONFIG_JSON")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    args = parser.parse_args(argv)

    if args.select_threshold:
        model = Model(args.model)
        candidates = (0.5, 0.6, 0.7, 0.8, 0.9)
        counts, chat_lines = dev_false_actions(model, args.select_threshold, candidates)
        chosen = None
        for threshold in candidates:
            print(f"threshold {threshold}: {counts[threshold]} of {chat_lines} dev chat lines read as an action")
            if chosen is None and counts[threshold] == 0:
                chosen = threshold
        print(f"chosen threshold (rule in intent/README.md): {chosen if chosen is not None else 0.9}")
        return 0
    if args.evaluate:
        model = Model(args.model)
        apps = json.loads(args.apps.read_text(encoding="utf-8"))["apps"]
        rows = [json.loads(line) for line in args.evaluate.read_text(encoding="utf-8").splitlines() if line.strip()]
        result = evaluate(model, rows, apps, args.threshold)
        for kind, text, got in result.pop("failures"):
            print(f"{kind}: {text!r} -> {json.dumps(got, ensure_ascii=False)}")
        print(json.dumps(result, indent=1))
        return 0
    if args.schema is None:
        parser.error("router mode needs the schema file path")
    print(run_router(args.model, args.schema, sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
