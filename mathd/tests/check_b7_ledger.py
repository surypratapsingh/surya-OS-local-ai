#!/usr/bin/env python3
"""B7 Independent Ledger Oracle & JSON Parser Checker (work order B7).

Uses CPython's standard `json` and `hashlib` modules as external, independent oracles
to verify:
  1. Hand-written JSON escaping correctly serialises and round-trips
     tricky claims containing literal quotes ("), backslashes (\\), and newlines (\\n).
  2. The B7 session mistake ledger contains one line per attempt (both Verified and Failed),
     where each line conforms strictly to the B7 specification:
     {sequence, timestamp, problem_id, claim, answer_given, verdict, time_taken_ms,
      counterexample, prev_hash, hash}.
  3. The cryptographic SHA-256 hash chaining is independently verified using
     Python's standard `hashlib` library.
  4. The explicit `reveal` command retrieves and prints ground truth without guessing.
  5. The `--verify-ledger` command rejects a tampered ledger.

Run:
    python mathd/tests/check_b7_ledger.py
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WASI_RUNNER = REPO_ROOT / "mathd/corpus/wasi-runner.mjs"
CLI_WASM = REPO_ROOT / "mathd/target/wasm32-wasip1/debug/mathd-cli.wasm"

GENESIS_PREV_HASH = "0" * 64

def compute_entry_hash(
    seq: int,
    ts: int,
    problem_id: str,
    claim: str,
    ans: str,
    verdict: str,
    time_ms: int,
    prev_hash: str,
    ce: dict | None,
) -> str:
    s = f"seq:{seq}|ts:{ts}|id:{problem_id}|claim:{claim}|ans:{ans}|verdict:{verdict}|time:{time_ms}|prev:{prev_hash}|ce:"
    if ce is not None:
        pt = ce["point"]
        s += f"exp:{ce['expected']:.10f}|got:{ce['got']:.10f}|pt:["
        for k in sorted(pt.keys()):
            s += f"{k}:{pt[k]:.10f},"
        s += "]"
    else:
        s += "none"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def find_cli_cmd(norm_args: list[str]) -> list[str]:
    # 1. Environment variable override (e.g. set by caller or test harness)
    env_bin = os.environ.get("MATHD_CLI_BIN")
    if env_bin and Path(env_bin).is_file():
        return [str(Path(env_bin))] + norm_args

    # 2. Known standard paths (native debug, Windows .exe, target-specific debug such as musl)
    candidates = [
        REPO_ROOT / "mathd/target/debug/mathd-cli",
        REPO_ROOT / "mathd/target/debug/mathd-cli.exe",
        REPO_ROOT / "mathd/target/x86_64-unknown-linux-musl/debug/mathd-cli",
        REPO_ROOT / "mathd/target/x86_64-unknown-linux-musl/debug/mathd-cli.exe",
    ]
    for c in candidates:
        if c.is_file():
            return [str(c)] + norm_args

    # 3. Fallback: search any binary named mathd-cli in mathd/target (excluding deps)
    target_dir = REPO_ROOT / "mathd/target"
    if target_dir.is_dir():
        for p in target_dir.glob("**/mathd-cli*"):
            if p.is_file() and p.name in ("mathd-cli", "mathd-cli.exe") and "deps" not in p.parts:
                return [str(p)] + norm_args

    # 4. Fallback: wasm32 runner if available
    if CLI_WASM.exists() and WASI_RUNNER.exists():
        return ["node", str(WASI_RUNNER), str(CLI_WASM)] + norm_args

    raise RuntimeError("Neither native mathd-cli nor wasm32-wasip1 runner found! Build mathd-cli first.")

def run_cli_cmd(args: list[str]) -> subprocess.CompletedProcess:
    norm_args = []
    for arg in args:
        p = Path(arg)
        try:
            rel = p.relative_to(REPO_ROOT)
            norm_args.append(str(rel).replace("\\", "/"))
        except ValueError:
            norm_args.append(arg)

    cmd = find_cli_cmd(norm_args)
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res

def test_escaping_roundtrip():
    print("=== 1. Testing JSON Escaping Round-trip (Quotes, Backslashes, Newlines) ===")
    tmpdir = REPO_ROOT / "target/b7_tmp_escape"
    if tmpdir.exists():
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        ledger_file = tmpdir / "test_tricky_ledger.jsonl"
        res = run_cli_cmd(["--test-escaping-ledger", str(ledger_file)])
        assert res.returncode == 0, f"CLI execution failed (rc={res.returncode}): {res.stderr}"

        assert ledger_file.exists(), f"Ledger file '{ledger_file}' was not created!"
        with open(ledger_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
        raw_line = lines[0]
        print(f"Raw ledger JSON line:\n  {raw_line}")

        # Independent parsing via CPython's standard `json` module
        entry = json.loads(raw_line)

        expected_claim = 'identity: "test\\quote"\n == x^2 + 1'
        expected_ans = '"test\\quote"\n'

        assert entry["claim"] == expected_claim, (
            f"Parsed claim does not match!\nExpected: {repr(expected_claim)}\nGot:      {repr(entry['claim'])}"
        )
        assert entry["answer_given"] == expected_ans, (
            f"Parsed answer does not match!\nExpected: {repr(expected_ans)}\nGot:      {repr(entry['answer_given'])}"
        )
        print("  OK: claim and answer contain literal '\"', '\\\\', and '\\n' and parse identically in Python")

        # Validate required schema fields
        assert isinstance(entry["sequence"], int) and entry["sequence"] == 1
        assert isinstance(entry["timestamp"], int) and entry["timestamp"] > 0
        assert entry["problem_id"] == "tricky-001"
        assert entry["verdict"] == "Failed"
        assert isinstance(entry["time_taken_ms"], int) and entry["time_taken_ms"] == 45
        assert "counterexample" in entry and entry["counterexample"] is not None

        ce = entry["counterexample"]
        assert "point" in ce and "expected" in ce and "got" in ce
        assert abs(ce["point"]["x"] - 2.5) < 1e-9
        assert abs(ce["expected"] - 7.25) < 1e-9
        assert abs(ce["got"] - 1.0) < 1e-9
        print(f"  OK: counterexample object verified: x={ce['point']['x']}, expected={ce['expected']}, got={ce['got']}")

        # Independent SHA-256 verification via Python's hashlib
        py_hash = compute_entry_hash(
            entry["sequence"],
            entry["timestamp"],
            entry["problem_id"],
            entry["claim"],
            entry["answer_given"],
            entry["verdict"],
            entry["time_taken_ms"],
            entry["prev_hash"],
            ce,
        )
        assert entry["hash"] == py_hash, f"Hash mismatch: ledger={entry['hash']}, python={py_hash}"
        print(f"  OK: cryptographic hash independently verified via Python hashlib: {py_hash}")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

def test_reveal_command():
    print("\n=== 2. Testing Explicit Reveal Command (Finding B7-L2) ===")
    res = run_cli_cmd(["--reveal", "substitution-001"])
    assert res.returncode == 0, f"--reveal failed (rc={res.returncode}): {res.stderr}"
    assert "substitution-001" in res.stdout, f"missing problem id in output: {res.stdout}"
    assert "x^2 - 2*x" in res.stdout, f"missing ground truth answer in output: {res.stdout}"
    print(f"  OK: --reveal substitution-001 returned ground truth answer: 'x^2 - 2*x'")

    # Reveal invalid ID should fail cleanly
    res_bad = run_cli_cmd(["--reveal", "non-existent-id-999"])
    assert res_bad.returncode != 0, "expected non-zero exit code for invalid problem ID"
    print("  OK: --reveal non-existent-id-999 correctly rejected with non-zero exit code")

def test_session_ledger():
    print("\n=== 3. Testing 20-Problem Session Mistake Ledger (Finding B7-L3 Schema & Hash-Chain) ===")
    tmpdir = REPO_ROOT / "target/b7_tmp_session"
    if tmpdir.exists():
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        ledger_file = tmpdir / "session_mistakes.jsonl"
        fixture_file = REPO_ROOT / "mathd/fixtures/b7_session.json"
        assert fixture_file.exists(), f"Fixture file '{fixture_file}' not found!"

        res = run_cli_cmd(["--session", str(fixture_file), "--ledger", str(ledger_file)])
        assert res.returncode == 0, f"Session harness failed (rc={res.returncode}):\n{res.stdout}\n{res.stderr}"

        assert ledger_file.exists(), f"Ledger file '{ledger_file}' was not created!"
        with open(ledger_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        print(f"Read {len(lines)} lines from mistake ledger.")
        assert len(lines) == 20, f"Expected 20 entries (all attempts per spec B7), found {len(lines)}"

        expected_prev = GENESIS_PREV_HASH
        verified_count = 0
        failed_count = 0

        for idx, line in enumerate(lines, 1):
            entry = json.loads(line)
            assert entry["sequence"] == idx, f"Sequence error: expected {idx}, got {entry['sequence']}"
            assert isinstance(entry["timestamp"], int) and entry["timestamp"] > 0
            assert isinstance(entry["problem_id"], str) and len(entry["problem_id"]) > 0
            assert isinstance(entry["claim"], str) and len(entry["claim"]) > 0
            assert isinstance(entry["answer_given"], str) and len(entry["answer_given"]) > 0
            assert entry["verdict"] in ("Verified", "Failed", "FailedDomain", "Unverifiable")
            assert isinstance(entry["time_taken_ms"], int) and entry["time_taken_ms"] >= 0
            assert entry["prev_hash"] == expected_prev, f"Chain broken at entry {idx}"

            ce = entry.get("counterexample")
            if entry["verdict"] == "Verified":
                verified_count += 1
                assert ce is None, f"Verified entry {idx} must not have counterexample"
            else:
                failed_count += 1
                assert ce is not None, f"Failed entry {idx} must have counterexample"
                assert isinstance(ce["expected"], (int, float))
                assert isinstance(ce["got"], (int, float))
                assert isinstance(ce["point"], dict) and len(ce["point"]) > 0

            py_hash = compute_entry_hash(
                entry["sequence"],
                entry["timestamp"],
                entry["problem_id"],
                entry["claim"],
                entry["answer_given"],
                entry["verdict"],
                entry["time_taken_ms"],
                entry["prev_hash"],
                ce,
            )
            assert entry["hash"] == py_hash, f"Hash mismatch at entry {idx}: {entry['hash']} != {py_hash}"
            expected_prev = entry["hash"]
            print(f"  [{idx:02d}/20] OK: id={entry['problem_id']:<18} verdict={entry['verdict']:<8} time={entry['time_taken_ms']}ms hash={entry['hash'][:12]}...")

        assert verified_count == 15, f"Expected 15 verified entries, got {verified_count}"
        assert failed_count == 5, f"Expected 5 failed entries, got {failed_count}"
        print(f"  OK: All 20 session entries (15 Verified, 5 Failed) validated by Python json and hashlib modules.")

        # Test ledger verification mode
        ver_res = run_cli_cmd(["--verify-ledger", str(ledger_file)])
        assert ver_res.returncode == 0, f"--verify-ledger failed: {ver_res.stderr}"
        print("  OK: CLI --verify-ledger successfully confirmed ledger validity")

        # Test tampered ledger detection
        tampered_file = tmpdir / "tampered_mistakes.jsonl"
        with open(ledger_file, "r", encoding="utf-8") as f:
            content = f.read()
        tampered_content = content.replace('"Verified"', '"Tampered"', 1)
        with open(tampered_file, "w", encoding="utf-8") as f:
            f.write(tampered_content)

        t_res = run_cli_cmd(["--verify-ledger", str(tampered_file)])
        assert t_res.returncode != 0, "CLI --verify-ledger must fail on tampered ledger"
        print("  OK: CLI --verify-ledger correctly failed with non-zero exit code on tampered ledger")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

def main():
    print("Running B7 Mistake Ledger Independent Python Check Script (CPython json + hashlib Oracle)...")
    test_escaping_roundtrip()
    test_reveal_command()
    test_session_ledger()
    print("\nALL B7 INDEPENDENT PYTHON ORACLE CHECKS PASSED (100% agreement with spec).")

if __name__ == "__main__":
    main()
