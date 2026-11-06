"""Unit tests for Metamorph proposal engine (work order A3.7).

Verifies:
1. Tampered chain log yields zero proposals (Rule 5 fail-closed).
2. Determinism: identical input yields byte-identical proposals.
3. Project invariant: proposal.applied is strictly False; any plant setting applied=True fails.
4. Target file safety: code extensions forbidden; only allowed data extensions permitted.
5. Emitted proposals written to UpdateProposalStore with applied=False.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from novacore.casememory import CaseMemory
from novacore.metamorph import (
    ALLOWED_DATA_EXTENSIONS,
    FORBIDDEN_EXTENSIONS,
    Metamorph,
    MetamorphProposal,
)
from novacore.updates import UpdateProposalStore


def _make_sample_cases(memory: CaseMemory) -> None:
    """Populate sample cases exercising various deterministic rules."""
    # Rule 1: frequent query (3 occurrences)
    for _ in range(3):
        memory.add(
            question="Solve quadratic equation ax^2 + bx + c = 0",
            method="Apply quadratic formula x = (-b +- sqrt(b^2 - 4ac)) / (2a)",
            answer="x = (-b +- sqrt(b^2 - 4ac)) / (2a)",
            domain="maths",
            evidence={"verdict": "Verified", "points_tested": 16, "max_relative_error": 0.0},
        )

    # Rule 2: numerical precision audit (max_relative_error > 1e-4)
    memory.add(
        question="Compute numerical integral of exp(-x^2)",
        method="Simpson rule approximation with 32 intervals",
        answer="0.746824",
        domain="maths",
        evidence={"verdict": "Verified", "points_tested": 8, "max_relative_error": 5e-4},
    )

    # Rule 4: study topic cluster (calculus queries)
    memory.add(
        question="Find derivative of f(x) = sin(x)*cos(x)",
        method="Product rule: cos^2(x) - sin^2(x)",
        answer="cos(2x)",
        domain="maths",
        evidence={"verdict": "Verified", "points_tested": 10, "max_relative_error": 0.0},
    )


class TestMetamorphPart1(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nova_test_metamorph_"))
        self.case_path = self.temp_dir / "cases.jsonl"
        self.memory = CaseMemory(self.case_path)
        self.proposal_dir = self.temp_dir / "proposals"
        self.proposal_store = UpdateProposalStore(self.proposal_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tampered_chainlog_yields_no_proposals(self) -> None:
        """A tampered chain log or anchor must fail closed and yield zero proposals."""
        _make_sample_cases(self.memory)
        engine = Metamorph(self.memory, self.proposal_store)

        # Baseline: intact log yields proposals
        proposals_intact = engine.generate_proposals()
        self.assertGreater(len(proposals_intact), 0)

        # Tamper 1: Modify content of a record in cases.jsonl
        lines = self.case_path.read_text(encoding="utf-8").splitlines()
        first_record = json.loads(lines[0])
        first_record["answer"] = "tampered_unverified_answer"
        lines[0] = json.dumps(first_record)
        self.case_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Must fail closed: yield zero proposals
        tampered_engine = Metamorph(CaseMemory(self.case_path), self.proposal_store)
        self.assertFalse(tampered_engine.verify_integrity())
        proposals_tampered = tampered_engine.generate_proposals()
        self.assertEqual(proposals_tampered, [])

    def test_tampered_anchor_yields_no_proposals(self) -> None:
        """A tampered tail anchor must fail closed and yield zero proposals."""
        _make_sample_cases(self.memory)
        anchor_path = self.case_path.with_name(self.case_path.name + ".anchor")
        self.assertTrue(anchor_path.exists())

        # Alter the anchor record count
        anchor_data = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor_data["count"] = 9999
        anchor_path.write_text(json.dumps(anchor_data), encoding="utf-8")

        engine = Metamorph(CaseMemory(self.case_path), self.proposal_store)
        self.assertFalse(engine.verify_integrity())
        self.assertEqual(engine.generate_proposals(), [])

    def test_determinism_same_input_same_proposals(self) -> None:
        """The same input must always yield the identical proposals deterministically."""
        _make_sample_cases(self.memory)
        engine1 = Metamorph(self.memory)
        engine2 = Metamorph(self.memory)

        p1 = engine1.generate_proposals()
        p2 = engine2.generate_proposals()

        self.assertGreater(len(p1), 0)
        self.assertEqual(len(p1), len(p2))

        for prop1, prop2 in zip(p1, p2):
            self.assertEqual(prop1.rule_id, prop2.rule_id)
            self.assertEqual(prop1.title, prop2.title)
            self.assertEqual(prop1.summary, prop2.summary)
            self.assertEqual(prop1.target_data_file, prop2.target_data_file)
            self.assertEqual(prop1.operation, prop2.operation)
            self.assertEqual(prop1.content, prop2.content)
            self.assertIs(prop1.applied, False)
            self.assertIs(prop2.applied, False)

    def test_applied_must_be_false(self) -> None:
        """Project invariant: proposal.applied must strictly be False."""
        _make_sample_cases(self.memory)
        engine = Metamorph(self.memory, self.proposal_store)
        proposals = engine.generate_proposals()

        self.assertGreater(len(proposals), 0)
        for p in proposals:
            self.assertIs(p.applied, False)
            self.assertEqual(p.applied, False)

        # Emit to UpdateProposalStore
        paths = engine.emit_proposals()
        self.assertEqual(len(paths), len(proposals))

        # Check files on disk
        for path in paths:
            self.assertTrue(path.is_file())
            proposal_dict = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("applied", proposal_dict)
            self.assertIs(proposal_dict["applied"], False)
            self.assertEqual(proposal_dict["applied"], False)

    def test_applied_true_plant_fails(self) -> None:
        """Constructing a proposal with applied=True must fail ValueError."""
        with self.assertRaises(ValueError) as ctx:
            MetamorphProposal(
                rule_id="RULE-PLANT",
                title="Planted proposal",
                summary="summary",
                target_data_file="data/test.json",
                operation="create",
                content="{}",
                applied=True,
            )
        self.assertIn("applied must be False", str(ctx.exception))

    def test_forbidden_code_extensions_rejected(self) -> None:
        """Proposals attempting to target code files must be rejected immediately."""
        for ext in FORBIDDEN_EXTENSIONS:
            with self.assertRaises(ValueError):
                MetamorphProposal(
                    rule_id="RULE-SECURITY",
                    title="Code attack",
                    summary="test",
                    target_data_file=f"evil{ext}",
                    operation="create",
                    content="evil",
                    applied=False,
                )

    def test_target_traversal_rejected(self) -> None:
        """Proposals targeting path traversal or absolute paths must be rejected."""
        with self.assertRaises(ValueError):
            MetamorphProposal(
                rule_id="RULE-SECURITY",
                title="Traversal attack",
                summary="test",
                target_data_file="../data.json",
                operation="create",
                content="evil",
                applied=False,
            )

    def test_rule3_conflicting_answers_detected(self) -> None:
        """RULE-3: Near-identical question with different answers generates disambiguation."""
        self.memory.add(
            question="How to configure static DNS nameservers?",
            method="Edit /etc/resolv.conf directly",
            answer="nameserver 1.1.1.1",
            domain="general",
        )
        self.memory.add(
            question="How to configure static DNS nameservers?",
            method="Use systemd-resolved via resolvectl",
            answer="resolvectl dns eth0 1.1.1.1",
            domain="general",
        )

        engine = Metamorph(self.memory, self.proposal_store)
        proposals = engine.generate_proposals()
        r3_proposals = [p for p in proposals if p.rule_id == "RULE-3"]
        self.assertEqual(len(r3_proposals), 1)
        self.assertIn("disambiguate", r3_proposals[0].title.lower())
        self.assertEqual(r3_proposals[0].target_data_file, "data/cases/disambiguations.json")
        self.assertIs(r3_proposals[0].applied, False)


if __name__ == "__main__":
    unittest.main()
