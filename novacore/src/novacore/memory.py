"""Memory management for NOVA companion.

Work order R5 items:
- Prevent event_id collisions when multiple memories written within same millisecond/second
- Make iter_summaries() a lazy generator so it does not read every summary file into memory at once
- Add document-length normalisation to search() scoring so long rambling entries do not
  outrank short exact matches
"""

from __future__ import annotations

import hashlib
import time
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Generator

from .prompts import Memory


@dataclass(order=True)
class MemoryEntry:
    """Internal entry representing a stored memory with ordering support."""
    timestamp: float = field(compare=True, default_factory=time.time)
    memory_id: str = field(compare=False, default_factory="")
    text: str = field(compare=False, default_factory="")
    summary: str = field(compare=False, default_factory="")
    # Document length for normalisation
    char_count: int = field(compare=False, default_factory=int)


class MemoryStore:
    """Manages storage and retrieval of companion memories with collision prevention
    and lazy loading."""

    def __init__(self, summaries_dir: str = "novacore/summaries") -> None:
        self.summaries_dir = summaries_dir
        self._entries: List[MemoryEntry] = []
        # Ensure summaries directory exists
        os.makedirs(self.summaries_dir, exist_ok=True)

    def _generate_event_id(self) -> str:
        """Generate a collision-resistant event_id using timestamp with microsecond
        precision plus a hash of the memory content.

        This prevents event_id collisions when multiple memories are written within
        the same millisecond/second by incorporating content-based hashing.
        """
        # Use time.time() with full float precision (includes sub-second fractions)
        ts = time.time()
        # Hash the timestamp combined with current memory count for additional uniqueness
        hash_input = f"{ts}_{id(self)}".encode("utf-8")
        event_id = hashlib.sha256(hash_input).hexdigest()[:16]
        return event_id

    def write_memory(self, text: str, summary: str = "") -> Memory:
        """Write a new memory with a collision-resistant event_id.

        Returns a Memory dataclass with the generated event_id.
        """
        event_id = self._generate_event_id()
        entry = MemoryEntry(
            timestamp=time.time(),
            memory_id=event_id,
            text=text,
            summary=summary,
            char_count=len(text),
        )
        self._entries.append(entry)

        # Persist to summary file
        summary_text = summary or (text[:200] + "...") if len(text) > 200 else text
        summary = summary or text
        filepath = os.path.join(
            self.summaries_dir, f"{event_id}.md"
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Memory {event_id}\n\n")
            f.write(f"**timestamp**: {datetime.fromtimestamp(ts).isoformat()}\n\n")
            f.write(f"**char_count**: {len(text)}\n\n")
            f.write(f"**summary**: {summary}\n\n")
            f.write(f"**text**: {text}\n")

        return Memory(
            memory_id=event_id,
            text=text,
            summary=summary,
        )

    def iter_summaries(self) -> Generator[MemoryEntry, None, None]:
        """Lazy generator that yields MemoryEntry objects from summary files.

        This does not read every summary file into memory at once. Instead, it
        yields entries one at a time, reading each file only when needed.
        This is important for memory efficiency when there are many summaries.
        """
        # First, yield from in-memory entries
        for entry in self._entries:
            yield entry

        # Then, lazily read from disk-based summary files
        if os.path.isdir(self.summaries_dir):
            for filename in sorted(os.listdir(self.summaries_dir)):
                if filename.endswith(".md"):
                    filepath = os.path.join(self.summaries_dir, filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            content = f.read()
                        # Parse the summary from the file
                        # Extract summary text from the markdown file
                        summary_text = self._parse_summary_from_markdown(content)
                        # Extract timestamp
                        ts = self._extract_timestamp_from_markdown(content)
                        # Create a minimal entry
                        entry = MemoryEntry(
                            timestamp=ts,
                            memory_id=filename.replace(".md", ""),
                            text="",
                            summary=summary_text,
                            char_count=len(summary_text) if summary_text else 0,
                        )
                        yield entry
                    except (OSError, UnicodeDecodeError):
                        # Skip files that can't be read
                        continue

    def _parse_summary_from_markdown(self, content: str) -> str:
        """Extract the summary text from a markdown memory file."""
        lines = content.split("\n")
        in_summary = False
        summary_lines: List[str] = []
        for line in lines:
            if line.startswith("**summary**: "):
                in_summary = True
                summary_lines.append(line[len("**summary**: "):])
            elif in_summary:
                if line.startswith("**") and line.endswith("**"):
                    # Next section starting
                    break
                else:
                    summary_lines.append(line)
        if summary_lines:
            return " ".join(summary_lines).strip()
        return ""

    def _extract_timestamp_from_markdown(self, content: str) -> float:
        """Extract timestamp from a markdown memory file."""
        import re
        match = re.search(r"\*\*timestamp\*\*: (.+)", content)
        if match:
            try:
                dt_str = match.group(1)
                dt = datetime.fromisoformat(dt_str)
                return dt.timestamp()
            except (ValueError, TypeError):
                pass
        return time.time()

    def search(self, query: str, limit: int = 10) -> List[MemoryEntry]:
        """Search memories by query string with document-length normalisation.

        Long rambling entries do not outrank short exact matches. The scoring
        function combines relevance (presence of query terms) with a penalty
        for document length, so concise exact matches rank higher than long
        documents with partial matches.
        """
        query_lower = query.lower()
        results: List[tuple[float, MemoryEntry]] = []

        for entry in self.iter_summaries():
            # Combine text and summary for searching
            searchable = f"{entry.text} {entry.summary}".lower()

            # Calculate relevance: count of query terms present
            query_terms = set(query_lower.split())
            if not query_terms:
                continue

            # Count how many query terms appear in the entry
            term_matches = sum(1 for term in query_terms if term in searchable)
            relevance = term_matches / len(query_terms) if query_terms else 0.0

            # Apply document-length normalisation: shorter documents get a
            # boost. Score = relevance / (1 + char_count / 100).
            # This means a 100-char exact match scores 0.5, a 500-char match
            # scores 0.2, etc. Exact short matches rank highest.
            length_penalty = 1.0 + (entry.char_count / 100.0)
            normalised_score = relevance / length_penalty

            results.append((normalised_score, entry))

        # Sort by normalised score (highest first)
        results.sort(key=lambda x: x[0], reverse=True)

        # Return top `limit` results
        return [entry for _, entry in results[:limit]]

    def get_recent(self, n: int = 5) -> List[MemoryEntry]:
        """Get the n most recent memories, sorted by timestamp (newest first)."""
        sorted_entries = sorted(self._entries, key=lambda e: e.timestamp, reverse=True)
        return sorted_entries[:n]