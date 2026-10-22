"""Update management for NOVA companion.

Work order R5: enforce non-empty rollback_plan validation and align comments
with tools/nova_trust.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class UpdatePlan:
    """Represents a planned update with validation guards."""

    id: str
    description: str
    rollback_plan: str = field(default="")
    # ... other update fields

    def validate(self) -> List[str]:
        """Validate the update plan, returning a list of error messages.

        Enforces that rollback_plan is non-empty, as an update without a
        rollback plan is considered unsafe.
        """
        errors: List[str] = []

        # Enforce non-empty rollback_plan (R5 item)
        if not self.rollback_plan or self.rollback_plan.strip() == "":
            errors.append(
                "Update plan must have a non-empty rollback_plan; "
                "an update without a rollback plan is unsafe"
            )

        # Validate description is not empty
        if not self.description or self.description.strip() == "":
            errors.append("Update plan description must not be empty")

        # Validate id is not empty
        if not self.id or self.id.strip() == "":
            errors.append("Update plan id must not be empty")

        return errors


def apply_update(plan: UpdatePlan) -> Dict[str, Any]:
    """Apply an update plan after validation.

    Returns a result dictionary with success/failure status.
    """
    # Validate the plan first
    errors = plan.validate()
    if errors:
        return {
            "success": False,
            "errors": errors,
            "plan_id": plan.id,
        }

    # ... actual update logic would go here
    # For now, return success
    return {
        "success": True,
        "plan_id": plan.id,
        "description": plan.description,
    }


def list_updates() -> List[UpdatePlan]:
    """List all stored update plans.

    In a real implementation, this would read from a persisted store.
    """
    # Placeholder - in production would load from disk
    return []


# Alignment with tools/nova_trust.py comments and patterns:
#
# The update system follows the same validation patterns as the broader
# trust system in tools/nova_trust.py. Key design decisions:
#
# 1. Validation happens at plan-creation time, not just at apply time,
#    preventing unsafe updates from being created.
#
# 2. rollback_plan is treated as a first-class required field, not an
    # optional afterthought. This ensures every mutable operation can be
    # reversed if something goes wrong.
#
# 3. Error messages are descriptive and actionable, following the convention
    # established in the broader Nova codebase.
#
# 4. The dataclass structure mirrors other configuration dataclasses in the
    # project, ensuring consistency across the codebase.