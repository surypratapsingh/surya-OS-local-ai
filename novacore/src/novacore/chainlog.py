"""The hash chain and tail anchor shared by the memory log and the capability outbox.

Chain: a record's chain_hash is the SHA-256 of the canonical JSON of its other
fields followed by the previous record's chain_hash. The first record links to
GENESIS. Anchor: `<log name>.anchor` next to the log holds the record count and
the last chain_hash, so a shorter valid chain is not accepted. What this does
and does not protect against: novacore/SCOPE.md, "Log integrity".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


GENESIS = "0" * 64


def chain_hash(previous: str, fields: dict) -> str:
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((canonical + previous).encode("utf-8")).hexdigest()


def anchor_path(log: Path) -> Path:
    return log.with_name(log.name + ".anchor")


def write_anchor(log: Path, count: int, head: str) -> None:
    target = anchor_path(log)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"count": count, "head": head}), encoding="utf-8", newline="\n")
    os.replace(temporary, target)


def adopt(log: Path, count: int, head: str) -> None:
    """Migration: anchor a log that has none. The caller verifies the chain first."""
    if anchor_path(log).exists():
        raise ValueError(f"{anchor_path(log)} already exists; delete it only after checking the log")
    write_anchor(log, count, head)


def check_tail(log: Path, count: int, head: str, last_line: int) -> None:
    """Raise unless the anchor agrees with the log. Call after the chain has verified.

    For a missing or empty log, pass count 0, head GENESIS and last_line 0.
    """
    target = anchor_path(log)
    if not target.exists():
        if count:
            raise ValueError(
                f"{log} has no anchor ({target.name}); if this log is yours, check it "
                f"and run its anchor() migration once (novacore/SCOPE.md)"
            )
        return
    try:
        anchor = json.loads(target.read_text(encoding="utf-8"))
        anchored = (int(anchor["count"]), str(anchor["head"]))
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"unreadable anchor {target}") from error
    if not log.exists():
        raise ValueError(f"{log} is missing but {target.name} records {anchored[0]} records")
    if anchored != (count, head):
        raise ValueError(
            f"anchor mismatch at {log}:{last_line}; the log has {count} records but "
            f"{target.name} records {anchored[0]}; records were dropped from or added to the end"
        )
