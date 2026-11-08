#!/usr/bin/env python3
"""
test_team_times.py

Test for team_times.py using a temporary git repository.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_temp_repo():
    """Create a temp git repo with test messages in team.md."""
    tmpdir = Path(tempfile.mkdtemp(prefix="test_team_times_"))
    os.chdir(tmpdir)

    # Init git
    subprocess.run(["git", "init"], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)

    # Create docs/team.md
    docs_dir = tmpdir / "docs"
    docs_dir.mkdir()

    team_md = docs_dir / "team.md"

    # Start with empty team.md
    team_md.write_text("# Team board\n\n")
    subprocess.run(["git", "add", "docs/team.md"], check=True)
    subprocess.run(["git", "commit", "-m", "Empty team.md"], check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": "2026-11-06 10:00:00 +0000"})

    # Add first message - commit at 10:00 UTC (matches stated 10:00)
    team_md.write_text("# Team board\n\n- 2026-11-06 10:00 UTC agent1 to agent2: First message\n")
    subprocess.run(["git", "add", "docs/team.md"], check=True)
    subprocess.run(["git", "commit", "-m", "Add first message"], check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": "2026-11-06 10:00:00 +0000"})

    # Add second message - commit at 14:00 UTC (2 hours after stated 12:00)
    team_md.write_text("# Team board\n\n- 2026-11-06 10:00 UTC agent1 to agent2: First message\n- 2026-11-06 12:00 UTC agent2 to agent1: Second message two hours off\n")
    subprocess.run(["git", "add", "docs/team.md"], check=True)
    subprocess.run(["git", "commit", "-m", "Add second message"], check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": "2026-11-06 14:00:00 +0000"})

    # Add third message - commit at 14:00 UTC (matches stated 14:00)
    team_md.write_text("# Team board\n\n- 2026-11-06 10:00 UTC agent1 to agent2: First message\n- 2026-11-06 12:00 UTC agent2 to agent1: Second message two hours off\n- 2026-11-06 14:00 UTC agent3 to agent1: Third message\n")
    subprocess.run(["git", "add", "docs/team.md"], check=True)
    subprocess.run(["git", "commit", "-m", "Add third message"], check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": "2026-11-06 14:00:00 +0000"})

    return tmpdir, team_md


def test_team_times():
    """Run the test."""
    original_dir = Path.cwd()
    tmpdir, team_md = setup_temp_repo()

    try:
        # Import the tool
        tool_path = original_dir / "tools" / "truth" / "team_times.py"
        # We need to run the tool from the temp dir, but it looks for docs/team.md
        # So we need to modify the tool's TEAM_MD path or run it with the right cwd
        # The tool uses Path("docs/team.md") which is relative to cwd
        # Since we're in the temp dir, it will find the right file

        # Run the tool as a subprocess from the temp dir
        result = subprocess.run(
            [sys.executable, str(tool_path)],
            capture_output=True,
            text=True,
            cwd=tmpdir,
        )

        print("STDOUT:")
        print(result.stdout)
        print("STDERR:")
        print(result.stderr)
        print(f"Exit code: {result.returncode}")

        # Parse output
        lines = result.stdout.strip().split("\n")
        findings = [l for l in lines if "stated" in l and "vs commit" in l]
        not_checkable = [l for l in lines if "not checkable" in l]
        count_line = [l for l in lines if l.startswith("findings:")]

        print(f"\nFindings lines: {findings}")
        print(f"Not checkable lines: {not_checkable}")
        print(f"Count line: {count_line}")

        # Should find exactly one finding (the second message)
        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
        assert "12:00 UTC" in findings[0], "Finding should be for the 12:00 message"
        assert "14:00 UTC" in findings[0], "Commit time should be 14:00"

        # Count line should show 1 finding
        assert count_line, "Missing count line"
        assert "findings: 1" in count_line[0], f"Count line mismatch: {count_line[0]}"

        print("\n✅ Test passed!")
        return True

    finally:
        os.chdir(original_dir)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_team_times()
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)