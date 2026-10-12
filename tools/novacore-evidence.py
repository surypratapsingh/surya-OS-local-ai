#!/usr/bin/env python3
"""R8 evidence harness for the novacore work orders W4, W5 and W6.

Each check follows the done-when text in docs/work-orders.md (sections W4, W5,
W6), not the existing unit tests, so a weak test cannot vouch for itself.

    py -3 tools/novacore-evidence.py w4   # source mutations against the suite
    py -3 tools/novacore-evidence.py w5   # canary token scan, 6 runs
    py -3 tools/novacore-evidence.py w6   # tamper mutations on a memory log

Standard library only. w4 edits two files under novacore/src and writes the
original bytes back in a finally block; check `git status` afterwards.
w5 scans the whole system temp directory, so it reads every file there.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "novacore" / "src"
TESTS = ROOT / "novacore" / "tests"
sys.path.insert(0, str(SRC))


# --- W4 ----------------------------------------------------------------------

def run_suite() -> int:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    done = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS)],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    print(done.stderr.rstrip())  # unittest reports on stderr
    print(f"[suite exit {done.returncode}]")
    return done.returncode


def mutate(path: Path, old: str, new: str) -> bytes:
    """Replace `old` with `new` once, in the file's own newline style."""
    original = path.read_bytes()
    nl = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8")
    old, new = old.replace("\n", nl), new.replace("\n", nl)
    if text.count(old) != 1:
        raise SystemExit(f"mutation anchor not found exactly once in {path}")
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    return original


W4_MUTATIONS = [
    # (label, file, old, new); every one should make the suite fail.
    (
        "add a confirmation-required capability without naming it in the prompt",
        "capabilities.py",
        'capability system exist.",\n    ),\n}',
        'capability system exist.",\n    ),\n    "storage.erase": Capability(\n'
        '        "storage.erase", Risk.CRITICAL, True, False,\n'
        '        "R8 probe: not named in the prompt.",\n    ),\n}',
    ),
    (
        "add a capability with requires_confirmation=False without naming it in the prompt",
        "capabilities.py",
        'capability system exist.",\n    ),\n}',
        'capability system exist.",\n    ),\n    "storage.erase": Capability(\n'
        '        "storage.erase", Risk.LOW, False, False,\n'
        '        "R8 probe: not named in the prompt.",\n    ),\n}',
    ),
    (
        "prompt names a capability absent from CATALOGUE (camera.record)",
        "prompts.py",
        "These include camera.enable,",
        "These include camera.enable, camera.record,",
    ),
    (
        "prompt no longer forbids invented tools",
        "prompts.py",
        "Do not invent tools, commands, memories, device state, successful actions, or\n"
        "installed software. ",
        "",
    ),
]


def w4() -> int:
    print("W4 done-when: adding a capability to CATALOGUE without mentioning it in the")
    print("prompt fails the suite. Probes 3 and 4 test W4 item 4's other two properties.")
    print("\n=== baseline (no mutation)")
    if run_suite() != 0:
        print("baseline suite is not green; stopping")
        return 1
    missed = 0
    for number, (label, name, old, new) in enumerate(W4_MUTATIONS, start=1):
        path = SRC / "novacore" / name
        print(f"\n=== probe {number}: {label}")
        print(f"file: novacore/src/novacore/{name}")
        print(f"- {old!r}\n+ {new!r}")
        original = mutate(path, old, new)
        try:
            rc = run_suite()
        finally:
            path.write_bytes(original)
        verdict = "CAUGHT (suite failed)" if rc != 0 else "NOT CAUGHT (suite passed)"
        missed += rc == 0
        print(f"probe {number}: {verdict}")
    print(f"\nW4 summary: {len(W4_MUTATIONS) - missed} of {len(W4_MUTATIONS)} probes caught")
    return 0


# --- W5 ----------------------------------------------------------------------

STUB = """import sys, time
mode = sys.argv[1]
prompt = open(sys.argv[2], encoding="utf-8").read() if len(sys.argv) > 2 else sys.stdin.read()
if mode == "timeout":
    time.sleep(100)
if mode == "nonzero":
    sys.stderr.write("stub runner failed on purpose\\n")
    sys.exit(3)
print("stub reply; prompt chars:", len(prompt), "echo:", prompt[-400:])
"""


def scan(root: Path, token: bytes, skip: set[Path]) -> tuple[list[str], int, int, int]:
    """Return (hits, files_read, bytes_read, unreadable) for every file under root."""
    hits, files, total, unreadable = [], 0, 0, 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = Path(dirpath) / name
            if path.resolve() in skip:
                continue
            try:
                with open(path, "rb") as handle:
                    tail = b""
                    while chunk := handle.read(1 << 20):
                        if token in tail + chunk:
                            hits.append(str(path))
                            break
                        tail = chunk[-len(token):]
                        total += len(chunk)
                files += 1
            except OSError:
                unreadable += 1
    return hits, files, total, unreadable


def w5_run(work: Path, use_stdin: bool, mode: str) -> int:
    from novacore.companion import Companion
    from novacore.config import ConversationConfig, ModelConfig
    from novacore.llm import LocalCommandBackend
    from novacore.memory import MemoryStore

    token = f"NOVACANARY{uuid.uuid4().hex}"
    run_dir = work / f"{'stdin' if use_stdin else 'file'}-{mode}"
    data_dir, cwd = run_dir / "data", run_dir / "cwd"
    data_dir.mkdir(parents=True)
    cwd.mkdir()
    stub = run_dir / "stub_runner.py"
    stub.write_text(STUB, encoding="utf-8")

    store_path = data_dir / "memories.jsonl"
    store = MemoryStore(store_path)
    store.add(f"canary note: my favourite word is {token}")
    command = (sys.executable, str(stub), mode) + (() if use_stdin else ("{prompt_file}",))
    backend = LocalCommandBackend(
        ModelConfig(command=command, timeout_seconds=2, max_output_chars=4000, use_stdin=use_stdin),
        data_dir,
    )
    companion = Companion(backend, store, ConversationConfig(memory_results=3, history_turns=4))
    recalled = store.search("what is my favourite word", 3)

    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        reply = companion.reply("what is my favourite word")
    finally:
        os.chdir(old_cwd)

    print(f"\n=== run: {'stdin' if use_stdin else 'prompt_file'} / {mode}")
    print(f"memory recalled into prompt: {any(token in m.text for m in recalled)}")
    print(f"reply (first 100 chars, token masked): {reply[:100].replace(token, '<TOKEN>')!r}")
    print(f"token echoed back in reply: {token in reply}")
    skip = {store_path.resolve()}
    misses = 0
    for label, root in (
        ("system temp", Path(tempfile.gettempdir())),
        ("NOVA data dir", data_dir),
        ("process cwd", cwd),
    ):
        hits, files, total, unreadable = scan(root, token.encode(), skip)
        print(f"scan {label} ({root}): {files} files, {total} bytes read, "
              f"{unreadable} unreadable, hits={hits}")
        misses += len(hits)
    left = sorted(p.name for p in (data_dir / "prompts").glob("*")) if (data_dir / "prompts").exists() else []
    print(f"files left in data/prompts: {left}")
    print(f"result: {'PASS' if misses == 0 else 'FAIL'} ({misses} hits outside {store_path.name})")
    return misses


def w5() -> int:
    print("W5 done-when: store a memory with a unique token, run one full conversation")
    print("turn through a stub runner, then scan the system temp directory, the NOVA")
    print("data directory and the process cwd: zero hits outside the memory store.")
    print("Repeated with the runner timing out mid-turn and exiting non-zero, for both")
    print("the stdin path and the {prompt_file} path. Unreadable files are counted, not read.")
    work = ROOT / "build" / "r8-w5"
    shutil.rmtree(work, ignore_errors=True)

    # Control first: a scan that cannot find a planted token proves nothing.
    token = f"NOVACANARY{uuid.uuid4().hex}"
    plant = Path(tempfile.mkdtemp(prefix="nova-w5-control-")) / "a" / "b" / "c"
    plant.mkdir(parents=True)
    (plant / "leak.bin").write_bytes(os.urandom(3 << 20) + token.encode() + os.urandom(100))
    hits, files, total, unreadable = scan(Path(tempfile.gettempdir()), token.encode(), set())
    shutil.rmtree(plant.parent.parent.parent)
    print(f"\n=== control: token planted 3 MiB into a file 4 directories deep in system temp")
    print(f"scan system temp: {files} files, {total} bytes read, {unreadable} unreadable, "
          f"hits={[str(Path(h).relative_to(tempfile.gettempdir())) for h in hits]}")
    print(f"control: {'PASS (planted token found)' if len(hits) == 1 else 'FAIL - W5 result below is void'}")

    total = 0
    for use_stdin in (True, False):
        for mode in ("ok", "timeout", "nonzero"):
            total += w5_run(work, use_stdin, mode)
    print(f"\nW5 summary: {total} hits outside the memory store across 6 runs")
    return 0


# --- W6 ----------------------------------------------------------------------

def chain(fields: dict, previous: str) -> str:
    """The documented chain format (memory.py, MemoryStore docstring)."""
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((canonical + previous).encode("utf-8")).hexdigest()


def w6() -> int:
    from novacore.memory import MemoryStore

    work = Path(tempfile.mkdtemp(prefix="nova-w6-"))
    clean = work / "clean.jsonl"
    store = MemoryStore(clean)
    for number in range(1, 6):
        store.add(f"record {number} text")
    lines = clean.read_text(encoding="utf-8").splitlines()
    print(f"W6: valid log of {len(lines)} records; clean read returns "
          f"{len(MemoryStore(clean).all())} records")

    def stale_append() -> list[str]:
        fields = {
            "memory_id": "r8-injected",
            "created_at": "2026-10-12T00:00:00+00:00",
            "text": "injected",
            "content_sha256": hashlib.sha256(b"injected").hexdigest(),
        }
        stale_link = json.loads(lines[3])["chain_hash"]  # links to record 4, not 5
        record = {**fields, "chain_hash": chain(fields, stale_link)}
        return lines + [json.dumps(record, ensure_ascii=False, sort_keys=True)]

    def retime(line: str) -> str:
        record = json.loads(line)
        record["created_at"] = "2000-01-01T00:00:00+00:00"
        return json.dumps(record, ensure_ascii=False, sort_keys=True)

    raw = clean.read_text(encoding="utf-8")
    line4_start = raw.index(lines[3])
    mutations = [
        ("1 byte changed inside record 3's text",
         "\n".join(lines[:2] + [lines[2].replace("record 3 text", "record X text")] + lines[3:]) + "\n"),
        ("2 record 3 deleted from the middle", "\n".join(lines[:2] + lines[3:]) + "\n"),
        ("3 records 2 and 3 swapped", "\n".join([lines[0], lines[2], lines[1]] + lines[3:]) + "\n"),
        ("4 record 3 timestamp altered", "\n".join(lines[:2] + [retime(lines[2])] + lines[3:]) + "\n"),
        ("5 record appended: correct self-digest, chain links to record 4 not 5",
         "\n".join(stale_append()) + "\n"),
        ("6a file truncated mid-record (inside record 4)", raw[: line4_start + len(lines[3]) // 2]),
        ("6b file truncated at a record boundary (records 4-5 dropped)", "\n".join(lines[:3]) + "\n"),
    ]
    missed = 0
    for label, text in mutations:
        path = work / f"m{label.split()[0]}.jsonl"
        path.write_text(text, encoding="utf-8", newline="\n")
        try:
            got = MemoryStore(path).all()
            missed += 1
            print(f"mutation {label}: NOT DETECTED - read returned {len(got)} records")
        except ValueError as error:
            print(f"mutation {label}: DETECTED - {error}")
    print(f"W6 memory log: {len(mutations) - missed} of {len(mutations)} mutations detected")

    # W6 item 3: cache keyed on path + mtime + size. Shrink the file, restore mtime.
    cached = work / "cache.jsonl"
    shutil.copy(clean, cached)
    reader = MemoryStore(cached)
    first = len(reader.all())
    stat = cached.stat()
    cached.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8", newline="\n")
    os.utime(cached, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    try:
        second = f"{len(reader.all())} records, no error"
    except ValueError as error:
        second = f"DETECTED - {error}"
    print(f"\ncache probe: same reader, record 3 deleted, mtime restored, size "
          f"{stat.st_size} -> {cached.stat().st_size}: first read {first} records, second read {second}")

    # W6 item 2: the capability outbox. List what can read it back.
    from novacore import capabilities
    readers = [n for n in dir(capabilities.CapabilityBroker) if "verify" in n or "read" in n]
    print(f"\noutbox: CapabilityBroker methods with 'verify' or 'read' in the name: {readers}")
    outbox = work / "events.jsonl"
    broker = capabilities.CapabilityBroker(outbox)
    for _ in range(3):
        broker.request("_test.stub", {}, owner_confirmed=True)
    events = outbox.read_text(encoding="utf-8").splitlines()
    outbox.write_text(events[0] + "\n" + events[1][:-5] + "\n", encoding="utf-8", newline="\n")
    broker.request("_test.stub", {}, owner_confirmed=True)
    after = outbox.read_text(encoding="utf-8").splitlines()
    newest = json.loads(after[-1])
    print(f"outbox: 3 events written; event 3 deleted and event 2 cut short; the next "
          f"request returned normally and wrote sequence {newest['sequence']} "
          f"(chain link restarted: {newest['chain_hash'] == chain({k: v for k, v in newest.items() if k != 'chain_hash'}, '0' * 64)})")
    shutil.rmtree(work)
    return 0


if __name__ == "__main__":
    commands = {"w4": w4, "w5": w5, "w6": w6}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(f"usage: {sys.argv[0]} {'|'.join(commands)}")
    raise SystemExit(commands[sys.argv[1]]())
