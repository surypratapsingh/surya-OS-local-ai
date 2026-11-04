"""Demand list for NOVA's offline shuttle protocol.

When NOVA lacks knowledge, capability, model weights, or modules, it records a
demand entry describing what is needed, step-by-step instructions for the owner
to fetch it on an internet-connected machine, and text-only reference URLs.

INVARIANT: URLs are plain text strings only. Nothing in novacore fetches, parses
via network resolvers, or opens network sockets for them. All materials arrive
via the physical pendrive shuttle channel, verified before use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class DemandEntry:
    entry_id: str
    title: str
    description: str
    item_type: str  # e.g. "model", "module", "study-pack", "recipe-dependency"
    target_filename: str  # e.g. "maths-class11.pkg"
    urls: tuple[str, ...]  # text-only URLs
    instructions: tuple[str, ...]  # step-by-step instructions for the owner
    created_at: str
    status: str = "open"  # "open", "fulfilled", "dismissed"
    expected_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "title": self.title,
            "description": self.description,
            "item_type": self.item_type,
            "target_filename": self.target_filename,
            "urls": list(self.urls),
            "instructions": list(self.instructions),
            "created_at": self.created_at,
            "status": self.status,
            "expected_sha256": self.expected_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DemandEntry:
        return cls(
            entry_id=str(data["entry_id"]),
            title=str(data["title"]),
            description=str(data["description"]),
            item_type=str(data.get("item_type", "module")),
            target_filename=str(data.get("target_filename", "")),
            urls=tuple(str(u) for u in data.get("urls", [])),
            instructions=tuple(str(s) for s in data.get("instructions", [])),
            created_at=str(data["created_at"]),
            status=str(data.get("status", "open")),
            expected_sha256=data.get("expected_sha256"),
        )

    def render_markdown(self) -> str:
        lines = [
            f"### [{self.status.upper()}] {self.title}",
            f"- **ID:** `{self.entry_id}`",
            f"- **Type:** {self.item_type}",
            f"- **Date Requested:** {self.created_at}",
        ]
        if self.target_filename:
            lines.append(f"- **Expected File:** `{self.target_filename}`")
        if self.expected_sha256:
            lines.append(f"- **Expected SHA-256:** `{self.expected_sha256}`")
        lines.append(f"\n**Reason / Description:**\n{self.description}\n")
        if self.urls:
            lines.append("**Source URLs (text only — copy into browser on an internet machine):**")
            for url in self.urls:
                lines.append(f"- `{url}`")
            lines.append("")
        if self.instructions:
            lines.append("**Step-by-step Instructions for Owner:**")
            for idx, step in enumerate(self.instructions, start=1):
                lines.append(f"{idx}. {step}")
            lines.append("")
        return "\n".join(lines)


class DemandStore:
    """Manages persistent demand entries and export to owner-specified directories."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read_all(self) -> list[DemandEntry]:
        if not self.path.exists():
            return []
        entries = []
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entries.append(DemandEntry.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
                raise ValueError(f"malformed demand store at {self.path}:{line_no}: {err}") from err
        return entries

    def _write_all(self, entries: list[DemandEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True))
                f.write("\n")
        tmp.replace(self.path)

    def add(
        self,
        title: str,
        description: str,
        urls: list[str] | tuple[str, ...],
        instructions: list[str] | tuple[str, ...],
        *,
        item_type: str = "module",
        target_filename: str = "",
        expected_sha256: str | None = None,
    ) -> DemandEntry:
        title = title.strip()
        description = description.strip()
        if not title:
            raise ValueError("demand title cannot be empty")
        if not description:
            raise ValueError("demand description cannot be empty")
        if len(title) > 200:
            raise ValueError("demand title exceeds 200 characters")
        if len(description) > 4000:
            raise ValueError("demand description exceeds 4000 characters")

        cleaned_urls = []
        for u in urls:
            if not isinstance(u, str):
                raise TypeError(f"URL must be a text string, got {type(u)}")
            u_clean = u.strip()
            if u_clean:
                cleaned_urls.append(u_clean)

        cleaned_steps = []
        for s in instructions:
            if not isinstance(s, str):
                raise TypeError(f"Instruction step must be a text string, got {type(s)}")
            s_clean = s.strip()
            if s_clean:
                cleaned_steps.append(s_clean)

        if not cleaned_steps:
            raise ValueError("demand entry requires at least one instruction step")

        if expected_sha256:
            expected_sha256 = expected_sha256.strip().lower()
            if len(expected_sha256) != 64 or not all(c in "0123456789abcdef" for c in expected_sha256):
                raise ValueError(f"expected_sha256 must be a 64-character hex string, got {expected_sha256!r}")

        entry = DemandEntry(
            entry_id=f"demand-{uuid4().hex[:12]}",
            title=title,
            description=description,
            item_type=item_type.strip(),
            target_filename=target_filename.strip(),
            urls=tuple(cleaned_urls),
            instructions=tuple(cleaned_steps),
            created_at=datetime.now(UTC).isoformat(),
            status="open",
            expected_sha256=expected_sha256,
        )

        entries = self._read_all()
        entries.append(entry)
        self._write_all(entries)
        return entry

    def list(self, status: str | None = "open") -> list[DemandEntry]:
        entries = self._read_all()
        if status is None:
            return entries
        return [e for e in entries if e.status == status]

    def all(self) -> list[DemandEntry]:
        return self._read_all()

    def get(self, entry_id: str) -> DemandEntry | None:
        for e in self._read_all():
            if e.entry_id == entry_id:
                return e
        return None

    def update_status(self, entry_id: str, new_status: str) -> bool:
        if new_status not in {"open", "fulfilled", "dismissed"}:
            raise ValueError(f"invalid demand status: {new_status}")
        entries = self._read_all()
        found = False
        updated = []
        for e in entries:
            if e.entry_id == entry_id:
                updated.append(
                    DemandEntry(
                        entry_id=e.entry_id,
                        title=e.title,
                        description=e.description,
                        item_type=e.item_type,
                        target_filename=e.target_filename,
                        urls=e.urls,
                        instructions=e.instructions,
                        created_at=e.created_at,
                        status=new_status,
                        expected_sha256=e.expected_sha256,
                    )
                )
                found = True
            else:
                updated.append(e)
        if found:
            self._write_all(updated)
        return found

    def resolve(self, entry_id: str) -> bool:
        return self.update_status(entry_id, "fulfilled")

    def dismiss(self, entry_id: str) -> bool:
        return self.update_status(entry_id, "dismissed")

    def export(self, target_dir: Path, *, status_filter: str | None = "open") -> tuple[Path, Path]:
        """Export demand list to an owner-specified directory (e.g. on a pendrive).

        Writes:
          1. DEMAND.md: Human-readable guide for the owner to follow on any internet PC.
          2. demands.json: Machine-readable data structure for shuttle packaging tools.

        Returns (markdown_path, json_path).
        """
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        entries = self.list(status=status_filter)

        # 1. Human-readable markdown
        md_lines = [
            "# NOVA Shuttle Demand List",
            "",
            "> Instructions for the owner: NOVA is completely offline.",
            "> Take this file (or pendrive) to any computer connected to the internet.",
            "> Follow the step-by-step instructions below for each item.",
            "> When done, save the signed packages into the shuttle pendrive and insert it into NOVA.",
            "",
            f"**Exported:** {datetime.now(UTC).isoformat()}",
            f"**Open Demands:** {len(entries)}",
            "",
            "---",
            "",
        ]

        if not entries:
            md_lines.append("No open demands currently pending.\n")
        else:
            for entry in entries:
                md_lines.append(entry.render_markdown())
                md_lines.append("\n---\n")

        md_path = target_dir / "DEMAND.md"
        md_path.write_text("\n".join(md_lines), encoding="utf-8")

        # 2. Machine-readable JSON
        json_data = {
            "format": "nova-demand-export-v1",
            "exported_at": datetime.now(UTC).isoformat(),
            "count": len(entries),
            "demands": [e.to_dict() for e in entries],
        }
        json_path = target_dir / "demands.json"
        json_path.write_text(json.dumps(json_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        return md_path, json_path


# Alias DemandList to DemandStore
DemandList = DemandStore
