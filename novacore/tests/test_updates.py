from __future__ import annotations

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
