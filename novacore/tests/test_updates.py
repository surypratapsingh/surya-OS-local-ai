from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import unittest

from novacore.updates import UpdateProposalStore


class UpdateProposalTests(unittest.TestCase):
    def test_proposal_is_unverified_and_never_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proposals = Path(directory) / "proposals"
            path = UpdateProposalStore(proposals).create("Improve console", "Add a safer diagnostic command.")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(path.is_relative_to(proposals))
            self.assertEqual("proposed_unverified", payload["status"])
            self.assertFalse(payload["applied"])
            self.assertTrue(payload["requires_signed_package"])


class RollbackPlanContractTests(unittest.TestCase):
    """R5 item 5: the hardcoded rollback_plan is fail-closed, not a TODO.

    create() accepts no rollback plan, so a proposal cannot arrive carrying a
    plan the model or caller invented; the field records that none was
    supplied and the proposal cannot advance on it. This test pins the
    contract the comment in updates.py states. It is a pin, not a pre-fix
    failure: a comment changes no behaviour, so it passed before the comment
    too (docs/logs/r5-5-rollback-comment.log says so in the log itself).
    """

    def test_create_takes_no_rollback_plan_and_stays_fail_closed(self) -> None:
        self.assertEqual(
            ["self", "title", "summary"],
            list(inspect.signature(UpdateProposalStore.create).parameters),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = UpdateProposalStore(Path(directory) / "proposals").create(
                "Improve console", "Add a safer diagnostic command."
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["rollback_plan"].startswith("Not specified."))
            self.assertEqual("proposed_unverified", payload["status"])
            self.assertFalse(payload["applied"])
            self.assertTrue(payload["requires_owner_confirmation"])
            self.assertTrue(payload["requires_signed_package"])

    def test_rejects_blank_and_oversized_text_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UpdateProposalStore(Path(directory) / "proposals")
            for title, summary in (("", "s"), ("t", "  "), ("t" * 201, "s"), ("t", "s" * 8_001)):
                with self.subTest(title=len(title), summary=len(summary)), self.assertRaises(ValueError):
                    store.create(title, summary)
            self.assertFalse((Path(directory) / "proposals").exists())

    def test_limits_are_inclusive_and_text_is_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UpdateProposalStore(Path(directory))
            path = store.create("  " + "t" * 200 + "  ", "s" * 8_000)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("t" * 200, data["title"])

    def test_each_proposal_gets_its_own_file_and_no_temp_file_remains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = UpdateProposalStore(Path(directory))
            first, second = store.create("a", "b"), store.create("a", "b")
            self.assertNotEqual(first, second)
            self.assertEqual({first.name, second.name}, {p.name for p in Path(directory).iterdir()})
            self.assertEqual(first.stem, json.loads(first.read_text(encoding="utf-8"))["proposal_id"])
