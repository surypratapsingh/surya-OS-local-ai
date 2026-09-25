#!/usr/bin/env python3
"""Break tools/nova_trust.py on purpose and require tests/test_trust.py to notice.

Each mutant is one deliberate bug, applied to a throwaway copy of the tools and
tests. The real tree is never touched. A mutant that the suite does not catch
("SURVIVED") means the suite is weaker than it claims. Exit 1 if any survives.

Run: python tests/mutate_trust.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_FILES = ["nova_trust.py", "keygen.py", "create-manifest.py", "sign-manifest.py",
              "verify-manifest.py"]

# (name, text to find in nova_trust.py, replacement). Each must match exactly once.
MUTANTS = [
    ("accept non-canonical S (signature malleability)",
     "if s >= _L:", "if s >= 2**256:"),
    ("verify() always says yes",
     "    if len(public) != 32 or len(signature) != 64:\n        return False\n    a_point",
     "    return True\n    a_point"),
    ("scalar clamping skips bit 254",
     "    a |= 1 << 254        # ...then set bit 254\n", ""),
    ("replay check lets the same release_sequence in twice",
     "if last_accepted_sequence is not None and seq <= last_accepted_sequence:",
     "if last_accepted_sequence is not None and seq < last_accepted_sequence:"),
    ("canonical JSON without sorted keys",
     'return json.dumps(unsigned, sort_keys=True,', 'return json.dumps(unsigned, sort_keys=False,'),
    ("signature does not cover the package list",
     'unsigned = {k: v for k, v in manifest.items() if k != "signature"}\n    return json.dumps',
     'unsigned = {k: v for k, v in manifest.items() if k not in ("signature", "packages")}\n    return json.dumps'),
    ("no domain-separation prefix",
     "return SIGNING_CONTEXT + canonical_bytes(manifest)", "return canonical_bytes(manifest)"),
    ("wildcard capabilities allowed",
     '_CAPABILITY = re.compile(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*")',
     '_CAPABILITY = re.compile(r"[a-z][a-z0-9_]*:[a-z*][a-z0-9_*]*")'),
    ("file names may contain a path",
     '_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")',
     '_FILENAME = re.compile(r"[A-Za-z0-9./][A-Za-z0-9./_-]{0,127}")'),
    ("package files never checked",
     "    if files_dir is not None:\n        for p in", "    if False:\n        for p in"),
    ("missing replay state silently means 0",
     '    obj = parse_json(Path(path).read_text(encoding="utf-8"))',
     '    if not Path(path).exists():\n        return 0\n'
     '    obj = parse_json(Path(path).read_text(encoding="utf-8"))'),
]


def main() -> int:
    source = (ROOT / "tools" / "nova_trust.py").read_text(encoding="utf-8")
    survived = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "tools").mkdir()
        for f in TOOL_FILES:
            shutil.copy2(ROOT / "tools" / f, work / "tools" / f)
        shutil.copytree(ROOT / "tests", work / "tests")

        def run_suite():
            r = subprocess.run([sys.executable, str(work / "tests" / "test_trust.py")],
                               capture_output=True, text=True, timeout=600)
            out = r.stdout + r.stderr
            failed = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\w+)", out, re.M)))
            return r.returncode, out.strip().splitlines()[-1], failed

        rc, last, _ = run_suite()
        print(f"unmutated baseline: {last}")
        if rc != 0:
            print("baseline must pass before any mutant means anything; stopping")
            return 1

        for name, find, replace in MUTANTS:
            count = source.count(find)
            if count != 1:
                print(f"INVALID  {name}: pattern matched {count} times (must be 1)")
                survived += 1
                continue
            (work / "tools" / "nova_trust.py").write_text(source.replace(find, replace), encoding="utf-8")
            rc, last, failed = run_suite()
            verdict = "KILLED  " if rc != 0 else "SURVIVED"
            survived += rc == 0
            print(f"{verdict} {name}: {last}")
            if failed:
                print(f"         caught by: {', '.join(failed)}")
        (work / "tools" / "nova_trust.py").write_text(source, encoding="utf-8")

    print(f"mutants: {len(MUTANTS)}, killed: {len(MUTANTS) - survived}, survived or invalid: {survived}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
