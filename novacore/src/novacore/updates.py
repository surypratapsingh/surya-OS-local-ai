"""Fail-closed OS update proposals; applying an update is intentionally absent."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4


class UpdateProposalStore:
    def __init__(self, proposal_dir: Path) -> None:
        self.proposal_dir = proposal_dir

    def create(self, title: str, summary: str) -> Path:
        title = title.strip()
        summary = summary.strip()
        if not title or not summary:
            raise ValueError("an update proposal needs both a title and a summary")
        if len(title) > 200 or len(summary) > 8_000:
            raise ValueError("update proposal text exceeds the allowed size")

        proposal_id = str(uuid4())
        proposal = {
            "format": "novacore-update-proposal-v1",
            "proposal_id": proposal_id,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "proposed_unverified",
            "applied": False,
            "requires_owner_confirmation": True,
            "requires_signed_package": True,
            "title": title,
            "summary": summary,
            "required_evidence": [
                "signed manifest and payload hash",
                "independent test evidence",
                "reviewed rollback plan",
                "owner confirmation through a trusted capability boundary",
            ],
            # R5 item 5: deliberate fail-closed, not a TODO. create() takes no
            # rollback plan, so a proposal can never arrive carrying a plan
            # the model or caller invented; the field records that none was
            # supplied and the proposal cannot advance on it. novacore/SCOPE.md
            # ("Delivered in this track") says proposals describe a rollback
            # plan "while explicitly remaining unverified and unapplied"; a
            # reviewed plan can only arrive through a signed package, never
            # through this constructor.
            "rollback_plan": "Not specified. Proposal cannot advance until this is supplied.",
        }
        self.proposal_dir.mkdir(parents=True, exist_ok=True)
        target = self.proposal_dir / f"{proposal_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target
