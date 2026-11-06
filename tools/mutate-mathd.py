#!/usr/bin/env python3
"""
tools/mutate-mathd.py - Mutation testing sweep for the mathd crate (work order A1.10)

AGENTS.md contract:
- Stdlib-only (no third-party dependencies).
- Copies mathd/ to an isolated temporary directory.
- Applies one syntactic/semantic mutation at a time from a fixed list (at most 60 mutants).
- Runs `cargo test` on each mutant.
- Records whether each mutant was KILLED (tests caught it / failed) or SURVIVED (tests passed).
- Outputs a comprehensive transcript and summary report.
- Any surviving mutant is a finding for the lead (indicates gaps in test oracle coverage).
"""

import argparse
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_MATHD_DIR = REPO_ROOT / "mathd"

# ---------------------------------------------------------------------------
# Fixed Mutant Definition List (50 mutants across verifier, evaluator,
# differentiator, canonicalizer, card_store, and ledger)
# ---------------------------------------------------------------------------

MUTANTS = [
    # --- Verifier Mutations (M01-M10) ---
    {
        "id": "M01",
        "file": "src/verifier.rs",
        "desc": "Increase MIN_TEST_POINTS threshold from 8 to 18",
        "target": "pub const MIN_TEST_POINTS: usize = 8;",
        "replacement": "pub const MIN_TEST_POINTS: usize = 18;",
    },
    {
        "id": "M02",
        "file": "src/verifier.rs",
        "desc": "Loosen VERIFIER_TOLERANCE from 1e-9 to 1e-3",
        "target": "pub const VERIFIER_TOLERANCE: f64 = 1e-9;",
        "replacement": "pub const VERIFIER_TOLERANCE: f64 = 1e-3;",
    },
    {
        "id": "M03",
        "file": "src/verifier.rs",
        "desc": "Invert is_verified check",
        "target": "matches!(self, Verdict::Verified { .. })",
        "replacement": "!matches!(self, Verdict::Verified { .. })",
    },
    {
        "id": "M04",
        "file": "src/verifier.rs",
        "desc": "Swap || to && in constant finite evaluation check",
        "target": "if !g.is_finite() || !e.is_finite() {\n                    return Verdict::Unverifiable",
        "replacement": "if !g.is_finite() && !e.is_finite() {\n                    return Verdict::Unverifiable",
    },
    {
        "id": "M05",
        "file": "src/verifier.rs",
        "desc": "Invert tolerance comparison in constant check (> to <)",
        "target": "let diff = (g - e).abs();\n                let scale = e.abs().max(1.0);\n                if diff > VERIFIER_TOLERANCE * scale",
        "replacement": "let diff = (g - e).abs();\n                let scale = e.abs().max(1.0);\n                if diff < VERIFIER_TOLERANCE * scale",
    },
    {
        "id": "M06",
        "file": "src/verifier.rs",
        "desc": "Falsify constant evidence points_tested count from 1 to 2",
        "target": "points_tested: 1,",
        "replacement": "points_tested: 2,",
    },
    {
        "id": "M07",
        "file": "src/verifier.rs",
        "desc": "Change TARGET_SAMPLE_POINTS loop break condition from >= to >",
        "target": "points_tested >= TARGET_SAMPLE_POINTS",
        "replacement": "points_tested > TARGET_SAMPLE_POINTS",
    },
    {
        "id": "M08",
        "file": "src/verifier.rs",
        "desc": "Invert MIN_TEST_POINTS check in verify_equivalence (>= to <)",
        "target": "if points_tested >= MIN_TEST_POINTS {\n        Verdict::Verified",
        "replacement": "if points_tested < MIN_TEST_POINTS {\n        Verdict::Verified",
    },
    {
        "id": "M09",
        "file": "src/verifier.rs",
        "desc": "Invert max to min in max_relative_error accumulation",
        "target": "points_tested += 1;\n                max_relative_error = max_relative_error.max(rel_err);",
        "replacement": "points_tested += 1;\n                max_relative_error = max_relative_error.min(rel_err);",
    },
    {
        "id": "M10",
        "file": "src/verifier.rs",
        "desc": "Invert empty check in verify_factorisation",
        "target": "if claimed_factors.is_empty() {",
        "replacement": "if !claimed_factors.is_empty() {",
    },

    # --- Evaluator Mutations (M11-M20) ---
    {
        "id": "M11",
        "file": "src/evaluator.rs",
        "desc": "Swap Add to Sub in apply_binop",
        "target": "Add => l + r,",
        "replacement": "Add => l - r,",
    },
    {
        "id": "M12",
        "file": "src/evaluator.rs",
        "desc": "Swap Sub to Add in apply_binop",
        "target": "Sub => l - r,",
        "replacement": "Sub => l + r,",
    },
    {
        "id": "M13",
        "file": "src/evaluator.rs",
        "desc": "Swap Mul to Add in apply_binop",
        "target": "Mul => l * r,",
        "replacement": "Mul => l + r,",
    },
    {
        "id": "M14",
        "file": "src/evaluator.rs",
        "desc": "Invert division by zero denominator check (r == 0.0 to != 0.0)",
        "target": "if r == 0.0 {",
        "replacement": "if r != 0.0 {",
    },
    {
        "id": "M15",
        "file": "src/evaluator.rs",
        "desc": "Drop negation in UnaryOp::Neg",
        "target": "UnaryOp::Neg => check_finite(-v),",
        "replacement": "UnaryOp::Neg => check_finite(v),",
    },
    {
        "id": "M16",
        "file": "src/evaluator.rs",
        "desc": "Swap sin to cos in apply_func",
        "target": "Function::Sin => x.sin(),",
        "replacement": "Function::Sin => x.cos(),",
    },
    {
        "id": "M17",
        "file": "src/evaluator.rs",
        "desc": "Swap cos to sin in apply_func",
        "target": "Function::Cos => x.cos(),",
        "replacement": "Function::Cos => x.sin(),",
    },
    {
        "id": "M18",
        "file": "src/evaluator.rs",
        "desc": "Negate result in Function::Abs",
        "target": "Function::Abs => x.abs(),",
        "replacement": "Function::Abs => -x.abs(),",
    },
    {
        "id": "M19",
        "file": "src/evaluator.rs",
        "desc": "Invert domain check in Function::Ln (<= 0.0 to > 0.0)",
        "target": "if x <= 0.0 {",
        "replacement": "if x > 0.0 {",
    },
    {
        "id": "M20",
        "file": "src/evaluator.rs",
        "desc": "Invert domain check in Function::Sqrt (< 0.0 to > 0.0)",
        "target": "if x < 0.0 {",
        "replacement": "if x > 0.0 {",
    },

    # --- Differentiator Mutations (M21-M30) ---
    {
        "id": "M21",
        "file": "src/differentiator.rs",
        "desc": "Invert variable name match in differentiate_raw (== to !=)",
        "target": "if name == var {",
        "replacement": "if name != var {",
    },
    {
        "id": "M22",
        "file": "src/differentiator.rs",
        "desc": "Swap add to sub for derivative of sum",
        "target": "BinOp::Add => add(differentiate_raw(left, var), differentiate_raw(right, var)),",
        "replacement": "BinOp::Add => sub(differentiate_raw(left, var), differentiate_raw(right, var)),",
    },
    {
        "id": "M23",
        "file": "src/differentiator.rs",
        "desc": "Swap sub to add for derivative of difference",
        "target": "BinOp::Sub => sub(differentiate_raw(left, var), differentiate_raw(right, var)),",
        "replacement": "BinOp::Sub => add(differentiate_raw(left, var), differentiate_raw(right, var)),",
    },
    {
        "id": "M24",
        "file": "src/differentiator.rs",
        "desc": "Sign flip in product rule derivative (add to sub)",
        "target": "add(mul(du, v), mul(u, dv))",
        "replacement": "sub(mul(du, v), mul(u, dv))",
    },
    {
        "id": "M25",
        "file": "src/differentiator.rs",
        "desc": "Sign flip in quotient rule derivative (sub to add)",
        "target": "let num = sub(mul(du, v.clone()), mul(u, dv));",
        "replacement": "let num = add(mul(du, v.clone()), mul(u, dv));",
    },
    {
        "id": "M26",
        "file": "src/differentiator.rs",
        "desc": "Increment instead of decrement exponent in power rule",
        "target": "let exp = sub(v, int(1));",
        "replacement": "let exp = add(v, int(1));",
    },
    {
        "id": "M27",
        "file": "src/differentiator.rs",
        "desc": "Derivative of sin mutated to sin",
        "target": "Function::Sin => mul(call(Function::Cos, u), du),",
        "replacement": "Function::Sin => mul(call(Function::Sin, u), du),",
    },
    {
        "id": "M28",
        "file": "src/differentiator.rs",
        "desc": "Sign flip in derivative of cos (neg dropped)",
        "target": "Function::Cos => mul(neg(call(Function::Sin, u)), du),",
        "replacement": "Function::Cos => mul(call(Function::Sin, u), du),",
    },
    {
        "id": "M29",
        "file": "src/differentiator.rs",
        "desc": "Swap div to mul in derivative of ln",
        "target": "Function::Ln => div(du, u),",
        "replacement": "Function::Ln => mul(du, u),",
    },
    {
        "id": "M30",
        "file": "src/differentiator.rs",
        "desc": "Swap mul to div in derivative of exp",
        "target": "Function::Exp => mul(call(Function::Exp, u), du),",
        "replacement": "Function::Exp => div(call(Function::Exp, u), du),",
    },

    # --- Canonicalizer Mutations (M31-M40) ---
    {
        "id": "M31",
        "file": "src/canonicalizer.rs",
        "desc": "Invert add condition in flatten_sum (!add to add)",
        "target": "if !add {",
        "replacement": "if add {",
    },
    {
        "id": "M32",
        "file": "src/canonicalizer.rs",
        "desc": "Drop negation of signs in flatten_sum",
        "target": "if !add {\n        for (sign, _) in right_terms.iter_mut() {\n            *sign = -*sign;\n        }\n    }",
        "replacement": "if !add {\n        for (sign, _) in right_terms.iter_mut() {\n            *sign = *sign;\n        }\n    }",
    },
    {
        "id": "M33",
        "file": "src/canonicalizer.rs",
        "desc": "Invert initial sign accumulator in build_product (1 to -1)",
        "target": "let mut sign: i64 = 1;",
        "replacement": "let mut sign: i64 = -1;",
    },
    {
        "id": "M34",
        "file": "src/canonicalizer.rs",
        "desc": "Invert negative exponent check in canon_pow (< 0 to > 0)",
        "target": "if *e < 0 {",
        "replacement": "if *e > 0 {",
    },
    {
        "id": "M35",
        "file": "src/canonicalizer.rs",
        "desc": "Mutate integer serialization tag from 'I' to 'J'",
        "target": "out.push('I');",
        "replacement": "out.push('J');",
    },
    {
        "id": "M36",
        "file": "src/canonicalizer.rs",
        "desc": "Mutate decimal serialization tag from 'D' to 'E'",
        "target": "out.push('D');",
        "replacement": "out.push('E');",
    },
    {
        "id": "M37",
        "file": "src/canonicalizer.rs",
        "desc": "Replace XOR with wrapping addition in FNV-1a hash",
        "target": "hash ^= u64::from(*byte);",
        "replacement": "hash = hash.wrapping_add(u64::from(*byte));",
    },
    {
        "id": "M38",
        "file": "src/canonicalizer.rs",
        "desc": "Mutate FNV-1a initial offset basis constant",
        "target": "let mut hash: u64 = 0xcbf2_9ce4_8422_2325;",
        "replacement": "let mut hash: u64 = 0xcbf2_9ce4_8422_2326;",
    },
    {
        "id": "M39",
        "file": "src/canonicalizer.rs",
        "desc": "Invert integer comparison order in cmp_expr (x.cmp(y) to y.cmp(x))",
        "target": "(Expr::Integer(x), Expr::Integer(y)) => x.cmp(y),",
        "replacement": "(Expr::Integer(x), Expr::Integer(y)) => y.cmp(x),",
    },
    {
        "id": "M40",
        "file": "src/canonicalizer.rs",
        "desc": "Invert variable comparison order in cmp_expr",
        "target": "(Expr::Variable(x), Expr::Variable(y)) => x.as_bytes().cmp(y.as_bytes()),",
        "replacement": "(Expr::Variable(x), Expr::Variable(y)) => y.as_bytes().cmp(x.as_bytes()),",
    },

    # --- Card Store Mutations (M41-M45) ---
    {
        "id": "M41",
        "file": "src/card_store.rs",
        "desc": "Inflate total_cards count in MethodCardStore::insert (+1 to +2)",
        "target": "self.total_cards += 1;",
        "replacement": "self.total_cards += 2;",
    },
    {
        "id": "M42",
        "file": "src/card_store.rs",
        "desc": "Invert promotion threshold check (== to !=)",
        "target": "if count == self.promotion_threshold {",
        "replacement": "if count != self.promotion_threshold {",
    },
    {
        "id": "M43",
        "file": "src/card_store.rs",
        "desc": "Auto-promote method candidate directly upon threshold (Proposed to Promoted)",
        "target": "status: MethodStatus::Proposed,",
        "replacement": "status: MethodStatus::Promoted,",
    },
    {
        "id": "M44",
        "file": "src/card_store.rs",
        "desc": "Corrupt structural shape abstraction in MethodCardStore::insert",
        "target": "let shape = StructuralShape::from_canonical(card.problem());",
        "replacement": "let shape = StructuralShape::Literal;",
    },
    {
        "id": "M45",
        "file": "src/card_store.rs",
        "desc": "Invert contains key lookup in MethodCardStore",
        "target": "self.cards.contains_key(&hash)",
        "replacement": "!self.cards.contains_key(&hash)",
    },

    # --- Ledger Mutations (M46-M50) ---
    {
        "id": "M46",
        "file": "src/ledger.rs",
        "desc": "Corrupt SHA-256 padding byte from 0x80 to 0x81",
        "target": "self.buf[self.buf_len] = 0x80;",
        "replacement": "self.buf[self.buf_len] = 0x81;",
    },
    {
        "id": "M47",
        "file": "src/ledger.rs",
        "desc": "Corrupt SHA-256 bit length calculation (* 8 to * 16)",
        "target": "let bit_len = self.total_len * 8;",
        "replacement": "let bit_len = self.total_len * 16;",
    },
    {
        "id": "M48",
        "file": "src/ledger.rs",
        "desc": "Invert sequence check in verify_integrity (!= to ==)",
        "target": "let expected_seq = (idx + 1) as u64;\n            if entry.sequence != expected_seq {",
        "replacement": "let expected_seq = (idx + 1) as u64;\n            if entry.sequence == expected_seq {",
    },
    {
        "id": "M49",
        "file": "src/ledger.rs",
        "desc": "Invert prev_hash chain check in verify_integrity (!= to ==)",
        "target": "if entry.prev_hash != expected_prev {",
        "replacement": "if entry.prev_hash == expected_prev {",
    },
    {
        "id": "M50",
        "file": "src/ledger.rs",
        "desc": "Invert hash mismatch check in verify_integrity (!= to ==)",
        "target": "if entry.hash != recomputed {",
        "replacement": "if entry.hash == recomputed {",
    },
]


def run_cargo_test(mathd_dir: Path, timeout_sec: int = 60) -> tuple[int, str]:
    """Run `cargo test --tests` on the given mathd clone."""
    env = os.environ.copy()
    manifest_path = mathd_dir / "Cargo.toml"
    wasi_runner = REPO_ROOT / "mathd/corpus/wasi-runner.mjs"

    # Auto-detect target: if on Windows and no link.exe, use wasm32-wasip1 runner
    if os.name == "nt" and not shutil.which("link.exe") and wasi_runner.exists():
        env["CARGO_TARGET_WASM32_WASIP1_RUNNER"] = f"node {wasi_runner.resolve()}"
        env["FB_PREOPEN"] = str(mathd_dir.resolve())
        cmd = [
            "cargo",
            "test",
            "--target",
            "wasm32-wasip1",
            "--tests",
            "--manifest-path",
            str(manifest_path),
        ]
    else:
        cmd = ["cargo", "test", "--tests", "--manifest-path", str(manifest_path)]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            env=env,
            cwd=str(mathd_dir),
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired as e:
        return -1, f"TIMEOUT after {timeout_sec}s: {e}"


def extract_failure_summary(stdout: str) -> str:
    """Extract first panic or failure reason from cargo test output."""
    lines = stdout.splitlines()
    for line in lines:
        if "panicked at" in line:
            return line.strip()
        if "FAILED" in line and not line.startswith("test result:"):
            return line.strip()
        if "error[" in line or "error: could not compile" in line:
            return line.strip()
    return "test execution returned non-zero exit code"


def main():
    parser = argparse.ArgumentParser(description="Mutation test sweep for mathd")
    parser.add_argument(
        "--mathd-dir",
        type=Path,
        default=DEFAULT_MATHD_DIR,
        help="Path to clean mathd crate",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        default=len(MUTANTS),
        help="Maximum mutants to test (at most 60)",
    )
    parser.add_argument(
        "--run-only",
        type=str,
        default=None,
        help="Comma-separated list of mutant IDs to run (e.g. M01,M14,M24)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Path to write log file (defaults to docs/logs/mutate-<timestamp>.log)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout in seconds for cargo test per mutant",
    )
    args = parser.parse_args()

    mutants_to_run = MUTANTS[: min(args.max_mutants, 60)]
    if args.run_only:
        allowed = {m.strip().upper() for m in args.run_only.split(",") if m.strip()}
        mutants_to_run = [m for m in mutants_to_run if m["id"] in allowed]

    timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = args.log
    if not log_path:
        log_path = REPO_ROOT / f"docs/logs/mutate-{timestamp_str}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log_lines = []

    def log_print(msg: str):
        print(msg)
        log_lines.append(msg)

    log_print(f"===============================================================================")
    log_print(f"mathd Mutation Testing Sweep (Work Order A1.10)")
    log_print(f"Date: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log_print(f"Agent: antigravity")
    log_print(f"Source crate: {args.mathd_dir}")
    log_print(f"Total Mutants Selected: {len(mutants_to_run)}")
    log_print(f"Log output: {log_path}")
    log_print(f"===============================================================================\n")

    # 1. Create scratch directory and copy mathd
    temp_dir = Path(tempfile.mkdtemp(prefix="mathd_mutate_"))
    scratch_mathd = temp_dir / "mathd"
    log_print(f"Copying clean mathd tree to scratch dir: {scratch_mathd}")

    # Copy mathd preserving directory structure
    shutil.copytree(
        args.mathd_dir,
        scratch_mathd,
        ignore=shutil.ignore_patterns("target", ".git"),
    )

    try:
        # 2. Run baseline test on clean tree
        log_print("Running baseline cargo test on clean mathd copy...")
        t0 = time.time()
        rc, out = run_cargo_test(scratch_mathd, timeout_sec=args.timeout)
        t_base = time.time() - t0
        if rc != 0:
            log_print(f"FATAL: Baseline cargo test failed on clean copy (exit code {rc})!")
            log_print(out[-1000:])
            sys.exit(1)
        log_print(f"Baseline clean run passed in {t_base:.2f}s.\n")

        results = []
        killed_count = 0
        survived_count = 0

        # 3. Iterate through mutants
        for idx, mutant in enumerate(mutants_to_run, 1):
            mid = mutant["id"]
            mfile_rel = mutant["file"]
            target_path = scratch_mathd / mfile_rel
            desc = mutant["desc"]
            target = mutant["target"]
            rep = mutant["replacement"]

            if not target_path.exists():
                log_print(f"[{idx:02d}/{len(mutants_to_run):02d}] {mid}: ERROR - target file {mfile_rel} not found!")
                results.append((mid, mfile_rel, desc, "ERROR", "file not found"))
                continue

            content = target_path.read_text(encoding="utf-8")
            count = content.count(target)
            if count != 1:
                log_print(f"[{idx:02d}/{len(mutants_to_run):02d}] {mid}: ERROR - target found {count} times (expected 1) in {mfile_rel}")
                results.append((mid, mfile_rel, desc, "ERROR", f"target count {count} != 1"))
                continue

            # Apply mutation
            mutated_content = content.replace(target, rep, 1)
            target_path.write_text(mutated_content, encoding="utf-8")

            t_start = time.time()
            rc, test_out = run_cargo_test(scratch_mathd, timeout_sec=args.timeout)
            elapsed = time.time() - t_start

            # Restore original file immediately
            target_path.write_text(content, encoding="utf-8")

            if rc != 0:
                killed_count += 1
                reason = extract_failure_summary(test_out)
                status = "KILLED"
                log_print(f"[{idx:02d}/{len(mutants_to_run):02d}] {mid} | {mfile_rel:<22} | {status} ({elapsed:.1f}s) | {desc}")
                log_print(f"     Reason: {reason}")
                results.append((mid, mfile_rel, desc, status, reason))
            else:
                survived_count += 1
                status = "SURVIVED"
                log_print(f"[{idx:02d}/{len(mutants_to_run):02d}] {mid} | {mfile_rel:<22} | {status} ({elapsed:.1f}s) | {desc}")
                log_print(f"     WARNING: Mutant survived test suite unnoticed!")
                results.append((mid, mfile_rel, desc, status, "tests passed without error"))

        # 4. Summary report
        total = len(results)
        score = (killed_count / total * 100.0) if total > 0 else 0.0

        log_print("\n===============================================================================")
        log_print("MUTATION SWEEP SUMMARY REPORT")
        log_print("===============================================================================")
        log_print(f"Total Mutants Tested: {total}")
        log_print(f"Killed (Caught):      {killed_count}")
        log_print(f"Survived (Uncaught):  {survived_count}")
        log_print(f"Mutation Score:       {score:.1f}%")
        log_print("-------------------------------------------------------------------------------")

        if survived_count > 0:
            log_print("\nSURVIVING MUTANTS (Findings for Lead / Test Suite Coverage Gaps):")
            for mid, mfile_rel, desc, status, reason in results:
                if status == "SURVIVED":
                    log_print(f"  - [{mid}] in {mfile_rel}: {desc}")
        else:
            log_print("\nALL MUTANTS KILLED. Test suite exhibits 100% mutation detection rate.")

        log_print("===============================================================================\n")

    finally:
        # Clean up temporary scratch directory
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Write log file
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    print(f"Mutation sweep log written to: {log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
