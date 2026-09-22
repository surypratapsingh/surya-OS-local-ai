"""Explicit, owner-approved plain-text memory with tamper-evident chaining."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
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
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: list[Memory] | None = None
        self._cache_mtime: float | None = None

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

        # Get the previous chain hash.
        previous_chain_hash = _GENESIS_CHAIN_HASH
        try:
            all_memories = self.all()
            if all_memories:
                previous_chain_hash = all_memories[-1].chain_hash
        except ValueError:
            # Chain is broken; still append but document the break.
            pass

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

        # Invalidate cache on write.
        self._cache = None
        self._cache_mtime = None

        return memory

    def all(self) -> list[Memory]:
        if not self.path.exists():
            return []

        # Check cache validity.
        if self.path.exists():
            current_mtime = self.path.stat().st_mtime
            if self._cache is not None and self._cache_mtime == current_mtime:
                return self._cache

        memories: list[Memory] = []
        previous_chain_hash = _GENESIS_CHAIN_HASH

        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                raw = json.loads(line)
                memory = Memory(**raw)
            except (TypeError, json.JSONDecodeError, TypeError) as error:
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

        # Cache the result.
        self._cache = memories
        self._cache_mtime = self.path.stat().st_mtime if self.path.exists() else None

        return memories

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
