"""Explicit, owner-approved plain-text memory with tamper-evident chaining."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterable
from uuid import uuid4


_WORD = re.compile(r"[\w']+", re.UNICODE)
_GENESIS_CHAIN_HASH = "0" * 64


@dataclass(frozen=True)
class Memory:
    memory_id: str
    created_at: str
    text: str
    content_sha256: str
    chain_hash: str


class MemoryStore:
    """An append-only JSONL store with chained hashing.

    Each record includes a chain_hash: the SHA-256 of the canonical JSON of all
    fields (except chain_hash itself) plus the previous record's chain_hash.
    This makes deletion, reordering, and tampering detectable.

    A chain alone accepts a shorter valid chain, so the tail is anchored: the
    file `<log name>.anchor` next to the log holds the record count and the
    last chain_hash as JSON. What the anchor does and does not protect against
    is in novacore/SCOPE.md.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.anchor_path = path.with_name(path.name + ".anchor")
        self._cache: list[Memory] | None = None
        self._cache_key: tuple | None = None

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {match.group(0).casefold() for match in _WORD.finditer(text)}

    @staticmethod
    def _compute_chain_hash(previous_chain_hash: str, memory_without_chain: dict) -> str:
        """Compute chain hash: SHA-256 of all fields (no chain_hash) + previous chain hash."""
        canonical = json.dumps(memory_without_chain, ensure_ascii=False, sort_keys=True)
        combined = canonical + previous_chain_hash
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def add(self, text: str) -> Memory:
        text = text.strip()
        if not text:
            raise ValueError("memory text cannot be empty")
        if len(text) > 4_000:
            raise ValueError("memory text exceeds 4000 characters")
        encoded = text.encode("utf-8")

        # Raises on a broken log: never extend a chain that does not verify.
        existing = self.all()
        previous_chain_hash = existing[-1].chain_hash if existing else _GENESIS_CHAIN_HASH

        memory_without_chain = {
            "memory_id": str(uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "text": text,
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
        }
        chain_hash = self._compute_chain_hash(previous_chain_hash, memory_without_chain)
        memory = Memory(
            **memory_without_chain,
            chain_hash=chain_hash,
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(memory), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        # A crash here leaves the anchor one record behind; the next read then
        # fails closed (see novacore/SCOPE.md).
        self._write_anchor(len(existing) + 1, chain_hash)

        # Invalidate cache on write.
        self._cache = None
        self._cache_key = None

        return memory

    def anchor(self) -> None:
        """Migration: anchor a log that has none (written before anchors, or by hand).

        The whole chain must verify first. Refuses if an anchor already exists:
        to re-anchor, the owner checks the log and deletes the anchor file.
        """
        if self.anchor_path.exists():
            raise ValueError(f"{self.anchor_path} already exists; delete it only after checking the log")
        memories, _ = self._read_chain()
        self._write_anchor(len(memories), memories[-1].chain_hash if memories else _GENESIS_CHAIN_HASH)

    def _write_anchor(self, count: int, head: str) -> None:
        temporary = self.anchor_path.with_name(self.anchor_path.name + ".tmp")
        temporary.write_text(json.dumps({"count": count, "head": head}), encoding="utf-8", newline="\n")
        os.replace(temporary, self.anchor_path)

    def _read_anchor(self) -> dict | None:
        if not self.anchor_path.exists():
            return None
        try:
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            return {"count": int(anchor["count"]), "head": str(anchor["head"])}
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"unreadable memory anchor {self.anchor_path}") from error

    def _stat_key(self) -> tuple:
        # ponytail: (mtime_ns, size) as W6 item 3 asks. A same-size rewrite that
        # also restores mtime_ns hits the cache and returns the last verified
        # records; a new MemoryStore still verifies the file. Hash the file per
        # read if that matters.
        def key(path: Path) -> tuple[int, int] | None:
            try:
                stat = path.stat()
            except FileNotFoundError:
                return None
            return (stat.st_mtime_ns, stat.st_size)

        return (key(self.path), key(self.anchor_path))

    def all(self) -> list[Memory]:
        cache_key = self._stat_key()
        if self._cache is not None and self._cache_key == cache_key:
            return self._cache

        anchor = self._read_anchor()
        if not self.path.exists():
            if anchor is not None:
                raise ValueError(
                    f"memory log {self.path} is missing but {self.anchor_path} records {anchor['count']} records"
                )
            return []

        memories, last_line = self._read_chain()
        head = memories[-1].chain_hash if memories else _GENESIS_CHAIN_HASH
        if anchor is None:
            if memories:
                raise ValueError(
                    f"memory log {self.path} has no anchor ({self.anchor_path.name}); "
                    f"if this log is yours, check it and call MemoryStore.anchor() once"
                )
        elif (anchor["count"], anchor["head"]) != (len(memories), head):
            raise ValueError(
                f"memory anchor mismatch at {self.path}:{last_line}; the log has {len(memories)} records "
                f"but {self.anchor_path.name} records {anchor['count']}; records were dropped from or "
                f"added to the end"
            )

        self._cache = memories
        self._cache_key = cache_key
        return memories

    def _read_chain(self) -> tuple[list[Memory], int]:
        """Verify every record and chain link; raise at the first break. Returns the last line number too."""
        memories: list[Memory] = []
        previous_chain_hash = _GENESIS_CHAIN_HASH
        last_line = 0

        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid memory record at {self.path}:{line_number}") from error
            if isinstance(raw, dict) and "chain_hash" not in raw:
                # W6 item 5: v1 records hash the text only. Refused, not migrated.
                raise ValueError(
                    f"v1 memory record (no chain_hash) at {self.path}:{line_number}; "
                    f"v1 logs are refused, see novacore/SCOPE.md"
                )
            try:
                memory = Memory(**raw)
            except TypeError as error:
                raise ValueError(f"invalid memory record at {self.path}:{line_number}") from error

            # Verify content hash (single record integrity).
            actual_content_hash = hashlib.sha256(memory.text.encode("utf-8")).hexdigest()
            if actual_content_hash != memory.content_sha256:
                raise ValueError(f"memory content hash mismatch at {self.path}:{line_number}")

            # Verify chain hash (tampering detection).
            memory_without_chain = {
                "memory_id": memory.memory_id,
                "created_at": memory.created_at,
                "text": memory.text,
                "content_sha256": memory.content_sha256,
            }
            expected_chain_hash = self._compute_chain_hash(previous_chain_hash, memory_without_chain)
            if expected_chain_hash != memory.chain_hash:
                raise ValueError(
                    f"memory chain hash mismatch at {self.path}:{line_number}; "
                    f"record may have been deleted, reordered, or tampered"
                )
            previous_chain_hash = memory.chain_hash
            memories.append(memory)
            last_line = line_number

        return memories, last_line

    def search(self, query: str, limit: int) -> list[Memory]:
        if limit <= 0:
            return []
        query_terms = self._tokens(query)
        if not query_terms:
            return []
        scored: list[tuple[int, int, Memory]] = []
        for position, memory in enumerate(self.all()):
            overlap = len(query_terms & self._tokens(memory.text))
            if overlap:
                scored.append((overlap, position, memory))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [memory for _, _, memory in scored[:limit]]

    def iter_summaries(self) -> Iterable[Memory]:
        return iter(self.all())
