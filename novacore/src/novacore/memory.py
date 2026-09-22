"""Explicit, owner-approved plain-text memory with local lexical recall."""

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


@dataclass(frozen=True)
class Memory:
    memory_id: str
    created_at: str
    text: str
    content_sha256: str


class MemoryStore:
    """An append-only JSONL store. `add` is the only persistence entry point."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {match.group(0).casefold() for match in _WORD.finditer(text)}

    def add(self, text: str) -> Memory:
        text = text.strip()
        if not text:
            raise ValueError("memory text cannot be empty")
        if len(text) > 4_000:
            raise ValueError("memory text exceeds 4000 characters")
        encoded = text.encode("utf-8")
        memory = Memory(
            memory_id=str(uuid4()),
            created_at=datetime.now(UTC).isoformat(),
            text=text,
            content_sha256=hashlib.sha256(encoded).hexdigest(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(memory), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        return memory

    def all(self) -> list[Memory]:
        if not self.path.exists():
            return []
        memories: list[Memory] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                raw = json.loads(line)
                memory = Memory(**raw)
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid memory record at {self.path}:{line_number}") from error
            actual = hashlib.sha256(memory.text.encode("utf-8")).hexdigest()
            if actual != memory.content_sha256:
                raise ValueError(f"memory hash mismatch at {self.path}:{line_number}")
            memories.append(memory)
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
        return self.all()
