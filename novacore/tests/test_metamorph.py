"""Unit tests for Metamorph proposal engine and rollback (work orders A3.7 & A3.8).

Verifies:
1. Tampered chain log yields zero proposals (Rule 5 fail-closed).
2. Determinism: identical input yields byte-identical proposals.
3. Project invariant: proposal.applied is strictly False; any plant setting applied=True fails.
4. Target file safety: code extensions forbidden; only allowed data extensions permitted.
5. Emitted proposals written to UpdateProposalStore with applied=False.
6. A3.8 Sandboxed validation: applies to temp copy, executes tests, records evidence.
7. A3.8 Explicit owner apply: takes versioned snapshot first.
8. A3.8 Rollback: restores exact bytes, verified by SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from novacore.casememory import CaseMemory
from novacore.metamorph import (
    ALLOWED_DATA_EXTENSIONS,
    FORBIDDEN_EXTENSIONS,
    Metamorph,
    MetamorphProposal,
    SandboxedEvidence,
    SnapshotStore,
    apply_proposal_real,
    rollback_to_snapshot,
    validate_proposal_sandboxed,
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

        proposals_intact = engine.generate_proposals()
        self.assertGreater(len(proposals_intact), 0)

        # Tamper content of a record in cases.jsonl
        lines = self.case_path.read_text(encoding="utf-8").splitlines()
        first_record = json.loads(lines[0])
        first_record["answer"] = "tampered_unverified_answer"
        lines[0] = json.dumps(first_record)
        self.case_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tampered_engine = Metamorph(CaseMemory(self.case_path), self.proposal_store)
        self.assertFalse(tampered_engine.verify_integrity())
        proposals_tampered = tampered_engine.generate_proposals()
        self.assertEqual(proposals_tampered, [])

    def test_tampered_anchor_yields_no_proposals(self) -> None:
        """A tampered tail anchor must fail closed and yield zero proposals."""
        _make_sample_cases(self.memory)
        anchor_path = self.case_path.with_name(self.case_path.name + ".anchor")
        self.assertTrue(anchor_path.exists())

        anchor_data = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor_data["count"] = 9999
        anchor_path.write_text(json.dumps(anchor_data), encoding="utf-8")

        engine = Metamorph(CaseMemory(self.case_path), self.proposal_store)
        self.assertFalse(engine.verify_integrity())
        self.assertEqual(engine.generate_proposals(), [])

    def test_determinism_same_input_same_proposals(self) -> None:
        """The same input must always yield identical proposals deterministically."""
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

        paths = engine.emit_proposals()
        self.assertEqual(len(paths), len(proposals))

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


class TestMetamorphPart2(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nova_test_metamorph_p2_"))
        self.data_dir = self.temp_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = self.temp_dir / "snapshots"
        self.snapshot_store = SnapshotStore(self.snapshot_dir)

        # Populate initial data files
        (self.data_dir / "skills").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "skills" / "base.json").write_text('{"base": true}\n', encoding="utf-8")
        (self.data_dir / "config.json").write_text('{"version": 1}\n', encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sandboxed_validation_runs_tests_and_stores_evidence(self) -> None:
        """Sandboxed validation applies proposal to temp copy only and captures test evidence."""
        proposal = MetamorphProposal(
            rule_id="RULE-1",
            title="Add template data",
            summary="New template",
            target_data_file="skills/new_template.json",
            operation="create",
            content='{"template": "algebra"}\n',
            applied=False,
        )

        # Baseline: verify target does NOT exist in real data_dir
        self.assertFalse((self.data_dir / "skills" / "new_template.json").exists())

        # Run sandboxed validation with python test runner command
        evidence = validate_proposal_sandboxed(
            proposal=proposal,
            data_dir=self.data_dir,
            test_command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            timeout=10.0,
        )

        self.assertIsInstance(evidence, SandboxedEvidence)
        self.assertTrue(evidence.test_passed)
        self.assertEqual(evidence.returncode, 0)
        self.assertGreaterEqual(evidence.duration_seconds, 0.0)
        self.assertTrue(len(evidence.evidence_sha256) == 64)

        # INVARIANT: real data_dir was never touched
        self.assertFalse((self.data_dir / "skills" / "new_template.json").exists())

    def test_sandboxed_validation_invalid_payload_fails(self) -> None:
        """Sandboxed validation must fail if proposal payload is invalid syntax for its file type."""
        proposal = MetamorphProposal(
            rule_id="RULE-BAD-JSON",
            title="Corrupted payload",
            summary="Bad json",
            target_data_file="skills/bad.json",
            operation="create",
            content="THIS IS NOT JSON AT ALL",
            applied=False,
        )
        evidence = validate_proposal_sandboxed(
            proposal=proposal,
            data_dir=self.data_dir,
            test_command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            timeout=10.0,
        )
        self.assertFalse(evidence.test_passed)
        self.assertNotEqual(evidence.returncode, 0)
        self.assertIn("syntax", evidence.output.lower())

    def test_sandboxed_validation_exposes_sandbox_path_to_child(self) -> None:
        """Sandboxed validation must expose sandbox paths to child process environment."""
        proposal = MetamorphProposal(
            rule_id="RULE-1",
            title="Test reachability",
            summary="Child reads target",
            target_data_file="skills/reachable.json",
            operation="create",
            content='{"reachable": true}\n',
            applied=False,
        )
        # Child script asserts NOVA_TARGET_DATA_FILE exists and contains payload
        cmd = [
            sys.executable,
            "-c",
            (
                "import os, sys, pathlib; "
                "p = os.environ.get('NOVA_TARGET_DATA_FILE'); "
                "sys.exit(0 if p and pathlib.Path(p).is_file() and 'reachable' in pathlib.Path(p).read_text(encoding='utf-8') else 1)"
            ),
        ]
        evidence = validate_proposal_sandboxed(
            proposal=proposal,
            data_dir=self.data_dir,
            test_command=cmd,
            timeout=10.0,
        )
        self.assertTrue(evidence.test_passed)
        self.assertEqual(evidence.returncode, 0)

    def test_apply_real_saves_snapshot_first(self) -> None:
        """apply_proposal_real is explicit and creates a versioned snapshot before changing data_dir."""
        proposal = MetamorphProposal(
            rule_id="RULE-1",
            title="Add template data",
            summary="New template",
            target_data_file="skills/new_template.json",
            operation="create",
            content='{"template": "algebra"}\n',
            applied=False,
        )

        snapshot, target_path = apply_proposal_real(
            proposal=proposal,
            data_dir=self.data_dir,
            snapshot_store=self.snapshot_store,
        )

        self.assertTrue(target_path.is_file())
        self.assertEqual(target_path.read_text(encoding="utf-8"), '{"template": "algebra"}\n')

        # Verify snapshot was created before the addition
        self.assertIn("config.json", snapshot.file_hashes)
        self.assertIn("skills/base.json", snapshot.file_hashes)
        # Snapshot taken BEFORE must not contain the newly added file
        self.assertNotIn("skills/new_template.json", snapshot.file_hashes)

    def test_apply_real_rejects_code_files(self) -> None:
        """apply_proposal_real strictly forbids modifying code files."""
        p = MetamorphProposal.__new__(MetamorphProposal)
        object.__setattr__(p, "target_data_file", "malicious.py")
        object.__setattr__(p, "content", "print('hacked')")
        with self.assertRaises(ValueError) as ctx:
            apply_proposal_real(
                proposal=p,
                data_dir=self.data_dir,
                snapshot_store=self.snapshot_store,
            )
        self.assertIn("code files", str(ctx.exception))

    def test_rollback_restores_exact_bytes_checked_by_sha256(self) -> None:
        """Rollback restores exact bytes to data_dir, verified by SHA-256."""
        # Initial hashes
        config_hash_before = hashlib.sha256((self.data_dir / "config.json").read_bytes()).hexdigest()
        base_hash_before = hashlib.sha256((self.data_dir / "skills" / "base.json").read_bytes()).hexdigest()

        # Step 1: Apply proposal for real
        proposal = MetamorphProposal(
            rule_id="RULE-1",
            title="Modify config",
            summary="Update config",
            target_data_file="config.json",
            operation="modify",
            content='{"version": 2, "updated": true}\n',
            applied=False,
        )
        snapshot, _ = apply_proposal_real(proposal, self.data_dir, self.snapshot_store)

        # Verify config was modified
        self.assertNotEqual(
            hashlib.sha256((self.data_dir / "config.json").read_bytes()).hexdigest(),
            config_hash_before,
        )

        # Also add an extraneous file that didn't exist before
        (self.data_dir / "unwanted.json").write_text('{"bad": true}\n', encoding="utf-8")

        # Step 2: Rollback
        restored = rollback_to_snapshot(snapshot.snapshot_id, self.data_dir, self.snapshot_store)

        # Step 3: Verify exact byte match via SHA-256
        self.assertFalse((self.data_dir / "unwanted.json").exists())
        config_hash_after = hashlib.sha256((self.data_dir / "config.json").read_bytes()).hexdigest()
        base_hash_after = hashlib.sha256((self.data_dir / "skills" / "base.json").read_bytes()).hexdigest()

        self.assertEqual(config_hash_after, config_hash_before)
        self.assertEqual(base_hash_after, base_hash_before)
        self.assertEqual(restored["config.json"], config_hash_before)
        self.assertEqual(restored["skills/base.json"], base_hash_before)

    def test_rollback_corrupted_backup_fails_sha256_verification(self) -> None:
        """If backup bytes are altered, rollback must fail SHA-256 verification."""
        proposal = MetamorphProposal(
            rule_id="RULE-1",
            title="Modify config",
            summary="Update config",
            target_data_file="config.json",
            operation="modify",
            content='{"version": 2}\n',
            applied=False,
        )
        snapshot, _ = apply_proposal_real(proposal, self.data_dir, self.snapshot_store)

        # Sabotage: corrupt the backup file inside snapshot store
        backup_file = self.snapshot_dir / snapshot.snapshot_id / "files" / "config.json"
        backup_file.write_text('{"corrupted": true}\n', encoding="utf-8")

        # Rollback must detect hash mismatch and raise ValueError
        with self.assertRaises(ValueError) as ctx:
            rollback_to_snapshot(snapshot.snapshot_id, self.data_dir, self.snapshot_store)
        self.assertIn("Rollback byte verification failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
