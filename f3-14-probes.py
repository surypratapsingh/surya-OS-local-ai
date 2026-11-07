"""F3.14 review probes (Freebuff). Read-only against novacore; all state in temp dirs.

Each probe answers a claim made by the code or its tests, using an independent
measurement (filesystem digest, wall-clock id comparison, the child process's own
view of its environment), never the code's own report of itself.

Run: PYTHONPATH=novacore/src python f3-14-probes.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "novacore" / "src"))

from novacore.metamorph import (  # noqa: E402
    MetamorphProposal,
    SnapshotStore,
    apply_proposal_real,
    rollback_to_snapshot,
    validate_proposal_sandboxed,
)


def tree_digest(root: Path) -> str:
    """Independent digest: sha256 over sorted (relpath, len, sha256(bytes))."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            b = p.read_bytes()
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(str(len(b)).encode())
            h.update(b"\0")
            h.update(hashlib.sha256(b).digest())
    return h.hexdigest()


def probe_snapshot_id_collision() -> None:
    print("### PROBE A: two snapshots of one directory inside the same second")
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        store_dir = Path(td) / "snapshots"
        data.mkdir()
        (data / "state.json").write_text('{"v": 0}\n', encoding="utf-8")

        store = SnapshotStore(store_dir)
        first = store.create_snapshot(data)
        (data / "state.json").write_text('{"v": 1}\n', encoding="utf-8")
        second = store.create_snapshot(data)

        print(f"first snapshot id:  {first.snapshot_id}")
        print(f"second snapshot id: {second.snapshot_id}")
        print(f"ids identical:      {first.snapshot_id == second.snapshot_id}")
        print(f"snapshot dirs on disk: {sorted(p.name for p in store_dir.iterdir())}")

        print(f"first snapshot recorded v0 bytes: {first.file_hashes}")
        print(f"second snapshot recorded bytes:   {second.file_hashes}")

        (data / "state.json").write_text('{"v": 2}\n', encoding="utf-8")
        rollback_to_snapshot(first.snapshot_id, data, store)
        print(f"after rollback to the FIRST id, state.json = {(data / 'state.json').read_text().strip()}")
        print("expected if the first snapshot still existed: {\"v\": 0}")


def probe_rollback_restores_whole_tree() -> None:
    print("\n### PROBE B: rollback claim, measured by whole-tree digest")
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        store_dir = Path(td) / "snapshots"
        (data / "skills").mkdir(parents=True)
        (data / "skills" / "a.json").write_text('{"a": 1}\n', encoding="utf-8")
        (data / "notes.txt").write_text("owner notes\n", encoding="utf-8")
        before = tree_digest(data)
        print(f"digest before:                    {before}")

        store = SnapshotStore(store_dir)
        proposal = MetamorphProposal(
            rule_id="probe_rule",
            title="probe",
            summary="probe",
            target_data_file="skills/b.json",
            operation="create",
            content='{"b": 2}\n',
        )
        snap, applied_path = apply_proposal_real(proposal, data, store)
        print(f"applied file exists:              {applied_path.is_file()}")
        (data / "skills" / "extra.json").write_text('{"extra": true}\n', encoding="utf-8")
        (data / "notes.txt").unlink()
        print(f"digest after apply+add+delete:    {tree_digest(data)}")

        rollback_to_snapshot(snap.snapshot_id, data, store)
        after = tree_digest(data)
        print(f"digest after rollback:            {after}")
        print(f"identical to before:              {after == before}")


def probe_sandbox() -> None:
    print("\n### PROBE C: sandboxed validation of a proposal with unusable content")
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        data.mkdir()
        (data / "skills").mkdir()
        (data / "skills" / "x.json").write_text('{"ok": true}\n', encoding="utf-8")

        proposal = MetamorphProposal(
            rule_id="probe_rule",
            title="probe",
            summary="probe",
            target_data_file="skills/x.json",
            operation="modify",
            content="THIS IS NOT JSON AT ALL",
        )
        child = (
            "import os,sys;"
            "print('child_env_mentions_sandbox=' + str(any('nova_sandboxed_metamorph_' in v for v in os.environ.values())));"
            "print('child_cwd=' + os.getcwd());"
            "print('child_cwd_has_data_dir=' + str(os.path.isdir('data')));"
            "print('child_can_read_target=' + str(os.path.isfile('data/skills/x.json')));"
            "sys.exit(0)"
        )
        evidence = validate_proposal_sandboxed(
            proposal, data, test_command=[sys.executable, "-c", child], timeout=30.0
        )
        print(f"test_passed: {evidence.test_passed}")
        print(f"returncode:  {evidence.returncode}")
        for line in evidence.output.strip().splitlines():
            print(f"  child said: {line}")
        print(f"real data dir untouched: {(data / 'skills' / 'x.json').read_text().strip()}")


def probe_study_answer_leak() -> None:
    print("\n### PROBE D: study session, does the student's answer reach disk?")
    from novacore.study import Question, StudySession

    token = "STUDENT_ANSWER_TOKEN_9f3c"
    with tempfile.TemporaryDirectory() as td:
        demands = Path(td) / "demands.jsonl"
        session = StudySession(
            [Question(id="q1", prompt="2+2?", claim_template="{answer} == 4")],
            mathd_cli=None,
            demand_store=demands,
        )
        print(f"mathd_cli_path resolved to: {session.mathd_cli_path}")
        result = session.submit_answer("q1", token)
        print(f"verdict: {result.verdict}")
        for line in demands.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            print(f"demand entry fields: {sorted(entry)}")
            print(f"token present in description: {token in entry.get('description', '')}")
            print(f"token present in title:       {token in entry.get('title', '')}")
        for p in sorted(Path(td).rglob("*")):
            if p.is_file():
                print(f"file written: {p.relative_to(td).as_posix()} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    probe_snapshot_id_collision()
    probe_rollback_restores_whole_tree()
    probe_sandbox()
    probe_study_answer_leak()
