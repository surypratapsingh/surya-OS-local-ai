#!/usr/bin/env python3
"""tools/mutate-mathd.py - Mutation testing sweep for the mathd crate (work order A1.10)

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
        "desc": "Remove finite check entirely",
        "target": "if !g.is_finite() {\n                    return Verdict::Unverifiable",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M06",
        "file": "src/verifier.rs",
        "desc": "Change verdict from Unverifiable to Verifiable",
        "target": "return Verdict::Unverifiable",
        "replacement": "return Verdict::Verified",
    },
    {
        "id": "M07",
        "file": "src/verifier.rs",
        "desc": "Swap Verdict::Verified and Verdict::Unverifiable variants",
        "target": "enum Verdict {\n    Verifiable,\n    Unverifiable,\n}",
        "replacement": "enum Verdict {\n    Unverifiable,\n    Verifiable,\n}",
    },
    {
        "id": "M08",
        "file": "src/verifier.rs",
        "desc": "Add extra unreachable branch",
        "target": "Verdict::Unverifiable",
        "replacement": "Verdict::Verifiable",
    },
    {
        "id": "M09",
        "file": "src/verifier.rs",
        "desc": "Replace finite check with always-false condition",
        "target": "if !g.is_finite() || !e.is_finite()",
        "replacement": "if false",
    },
    {
        "id": "M10",
        "file": "src/verifier.rs",
        "desc": "Change comparison operator in finite check",
        "target": "g.is_finite() && e.is_finite()",
        "replacement": "g.is_finite() || e.is_finite()",
    },

    # --- Evaluator Mutations (M11-M20) ---
    {
        "id": "M11",
        "file": "src/evaluator.rs",
        "desc": "Change + to - in binary operation eval",
        "target": "left + right",
        "replacement": "left - right",
    },
    {
        "id": "M12",
        "file": "src/evaluator.rs",
        "desc": "Change * to / in binary operation eval",
        "replacement": "left * right",
    },
    {
        "id": "M13",
        "file": "src/evaluator.rs",
        "desc": "Change / to * in binary operation eval",
        "replacement": "left / right",
    },
    {
        "id": "M14",
        "file": "src/evaluator.rs",
        "desc": "Change > to < in comparison",
        "replacement": "left > right",
    },
    {
        "id": "M15",
        "file": "src/evaluator.rs",
        "desc": "Change < to > in comparison",
        "replacement": "left < right",
    },
    {
        "id": "M16",
        "file": "src/evaluator.rs",
        "desc": "Remove division by zero check",
        "target": "right != 0",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M17",
        "file": "src/evaluator.rs",
        "desc": "Change f64::is_nan check",
        "target": "x.is_nan()",
        "replacement": "!x.is_nan()",
    },
    {
        "id": "M18",
        "file": "src/evaluator.rs",
        "desc": "Swap float comparison direction",
        "target": "x > y",
        "replacement": "x < y",
    },
    {
        "id": "M19",
        "file": "src/evaluator.rs",
        "desc": "Change literal from 1.0 to 0.0",
        "target": "1.0",
        "replacement": "0.0",
    },
    {
        "id": "M20",
        "file": "src/evaluator.rs",
        "desc": "Remove overflow check",
        "target": "checked_add",
        "replacement": "saturated_add",
    },

    # --- Differentiator Mutations (M21-M30) ---
    {
        "id": "M21",
        "file": "src/differentiator.rs",
        "desc": "Change differentiation rule",
        "target": "differentiate_power",
        "replacement": "differentiate_constant",
    },
    {
        "id": "M22",
        "file": "src/differentiator.rs",
        "desc": "Remove product rule term",
        "target": "u * dv + v * du",
        "replacement": "u * dv",
    },
    {
        "id": "M23",
        "file": "src/differentiator.rs",
        "desc": "Swap sin and cos derivative",
        "target": "cos(x)",
        "replacement": "-sin(x)",
    },
    {
        "id": "M24",
        "file": "src/differentiator.rs",
        "desc": "Change variable differentiation to constant",
        "target": "differentiate(x)",
        "replacement": "differentiate(5)",
    },
    {
        "id": "M25",
        "file": "src/differentiator.rs",
        "desc": "Change simplification rule",
        "target": "structural_simplify",
        "replacement": "trivial_simplify",
    },
    {
        "id": "M26",
        "file": "src/differentiator.rs",
        "desc": "Remove chain rule term",
        "target": "chain_rule(f, g)",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M27",
        "file": "src/differentiator.rs",
        "desc": "Swap differentiation order",
        "target": "d2/dx2",
        "replacement": "d2/dx1",
    },
    {
        "id": "M28",
        "file": "src/differentiator.rs",
        "desc": "Change sign of second derivative",
        "target": "d2f/dx2 > 0",
        "replacement": "d2f/dx2 < 0",
    },
    {
        "id": "M29",
        "file": "src/differentiator.rs",
        "desc": "Change test assertion tolerance",
        "target": "approx 1e-9",
        "replacement": "approx 1e-3",
    },
    {
        "id": "M30",
        "file": "src/differentiator.rs",
        "desc": "Add unnecessary intermediate step",
        "target": "# skip simplification",
        "replacement": "# keep simplification",
    },

    # --- Canonicalizer Mutations (M31-M40) ---
    {
        "id": "M31",
        "file": "src/canonicalizer.rs",
        "desc": "Change sort order of operands",
        "target": "structural_hash",
        "replacement": "hash_swap",
    },
    {
        "id": "M32",
        "file": "src/canonicalizer.rs",
        "desc": "Remove operand sorting",
        "target": "sort_operands",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M33",
        "file": "src/canonicalizer.rs",
        "desc": "Change hash algorithm",
        "target": "blake2b_512",
        "replacement": "sha256",
    },
    {
        "id": "M34",
        "file": "src/canonicalizer.rs",
        "desc": "Swap simplification strategies",
        "target": "canonicalize",
        "replacement": "partial_canonicalize",
    },
    {
        "id": "M35",
        "file": "src/canonicalizer.rs",
        "desc": "Replace hash comparison with identity check",
        "target": "blake2b_512(kernel) != parsed[\"blake2b\"]",
        "replacement": "# [mutated away - always passes]",
    },
    {
        "id": "M36",
        "file": "src/canonicalizer.rs",
        "desc": "Change normalization function",
        "target": "normalize_expr",
        "replacement": "normalize_expr_loose",
    },
    {
        "id": "M37",
        "file": "src/canonicalizer.rs",
        "desc": "Swap reduction order",
        "target": "reduce_then_hash",
        "replacement": "hash_then_reduce",
    },
    {
        "id": "M38",
        "file": "src/canonicalizer.rs",
        "desc": "Remove hash chaining check",
        "target": "verify_identity",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M39",
        "file": "src/canonicalizer.rs",
        "desc": "Change constant folding approach",
        "target": "fold_constants",
        "replacement": "no_fold_constants",
    },
    {
        "id": "M40",
        "file": "src/canonicalizer.rs",
        "desc": "Swap evaluation order in let binding",
        "target": "let y = x * 2",
        "replacement": "let y = 2 * x",
    },

    # --- Card Store Mutations (M41-M46) ---
    {
        "id": "M41",
        "file": "src/card_store.rs",
        "desc": "Change card comparison operator",
        "target": "card_a > card_b",
        "replacement": "card_a < card_b",
    },
    {
        "id": "M42",
        "file": "src/card_store.rs",
        "desc": "Remove card validation",
        "target": "validate_card",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M43",
        "file": "src/card_store.rs",
        "desc": "Change card hash algorithm",
        "target": "blake2b_256",
        "replacement": "sha256",
    },
    {
        "id": "M44",
        "file": "src/card_store.rs",
        "desc": "Swap card order in registry",
        "target": "registry.insert",
        "replacement": "registry.push",
    },
    {
        "id": "M45",
        "file": "src/card_store.rs",
        "desc": "Remove duplicate check",
        "target": "if not exists",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M46",
        "file": "src/card_store.rs",
        "desc": "Change card sort key",
        "target": "sort_by_id",
        "replacement": "sort_by_name",
    },

    # --- Ledger Mutations (M47-M50) ---
    {
        "id": "M47",
        "file": "src/ledger.rs",
        "desc": "Change entry hash computation",
        "target": "compute_entry_hash",
        "replacement": "compute_entry_hash_swap",
    },
    {
        "id": "M48",
        "file": "src/ledger.rs",
        "desc": "Remove hash chain verification",
        "target": "verify_ledger_chain",
        "replacement": "# [mutated away]",
    },
    {
        "id": "M49",
        "file": "src/ledger.rs",
        "desc": "Change sequence numbering",
        "target": "sequence: idx",
        "replacement": "sequence: idx + 1",
    },
    {
        "id": "M50",
        "file": "src/ledger.rs",
        "desc": "Change timestamp format",
        "target": "ts: unix_now()",
        "replacement": "ts: system_time()",
    },
]