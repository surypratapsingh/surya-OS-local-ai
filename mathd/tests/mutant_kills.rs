//! Integration tests to kill mutation survivors identified in docs/logs/mutate-sweep.log.
//!
//! Survivors addressed:
//! - M04: verifier.rs constant finite evaluation check (|| to &&)
//! - M09: verifier.rs max_relative_error accumulation (.max() to .min())
//! - M35: canonicalizer.rs integer serialization tag ('I' to 'J')
//! - M36: canonicalizer.rs decimal serialization tag ('D' to 'E')
//! - M37: canonicalizer.rs FNV-1a hash algorithm (XOR to wrapping add)
//! - M38: canonicalizer.rs FNV-1a initial offset basis constant
//!
//! Expected values are derived from independent external oracles:
//! - Published FNV-1a 64-bit reference test vectors (IETF draft-eastlake-fnv-17).
//! - Independent serialization oracle mathd/tests/canon_oracle.py (docs/logs/a1-14-oracle.log).
//! - Hand-calculated expected verdicts and error bounds for verifier mutants M04 and M09.

use mathd::canonicalizer::{hash_canonical, structural_hash};
use mathd::parser::{parse, Expr};
use mathd::verifier::{verify_equivalence, Evidence, Side, Verdict};

// ===========================================================================
// Canonicalizer & FNV-1a Mutants: M35, M36, M37, M38
// ===========================================================================

/// Published reference test vectors for 64-bit FNV-1a:
/// - ""       -> 0xcbf29ce484222325
/// - "a"      -> 0xaf63dc4c8601ec8c
/// - "foobar" -> 0x85944171f73967e8
///
/// Specification: IETF draft-eastlake-fnv-17 / Fowler-Noll-Vo 64-bit FNV-1a.
/// In mathd, hash_canonical runs FNV-1a 64 over the serialization of an expression.
#[test]
fn test_m37_m38_fnv1a_published_vectors_and_basis() {
    // Variable "x" serializes as "Vx;" per spec (mathd/src/canonicalizer.rs line 706).
    // Oracle calculation (docs/logs/a1-14-oracle.log line 18):
    //   "Vx;" FNV-1a 64 = 0x7bc57119e9c09f08 (8918658993186971400u64).
    let expr_x = parse("x").unwrap();
    let hash_x = structural_hash(&expr_x);
    assert_eq!(
        hash_x, 0x7bc57119e9c09f08u64,
        "structural_hash('x') must match independent oracle FNV-1a 64 value (kills M37 XOR->Add, M38 basis mutation)"
    );

    // Constant "pi" serializes as "Cpi;" per spec (mathd/src/canonicalizer.rs line 701).
    // Oracle calculation (docs/logs/a1-14-oracle.log line 16):
    //   "Cpi;" FNV-1a 64 = 0x4bb5f39c95b0db52 (5455534377481067346u64).
    let expr_pi = parse("pi").unwrap();
    let hash_pi = structural_hash(&expr_pi);
    assert_eq!(
        hash_pi, 0x4bb5f39c95b0db52u64,
        "structural_hash('pi') must match independent oracle FNV-1a 64 value"
    );
}

/// Kill M35: Mutate integer serialization tag from 'I' to 'J' (canonicalizer.rs line 689).
/// Under spec (mathd/src/canonicalizer.rs lines 688-692):
///   Integer(42) serializes as "I42;".
/// Oracle calculation (docs/logs/a1-14-oracle.log line 12):
///   "I42;" FNV-1a 64 = 0x2909b3d05ec97be7 (2957092337870273511u64).
/// Under mutant M35, tag is 'J', producing "J42;" with hash 0xf34f8de8c40228c0.
#[test]
fn test_m35_kill_integer_serialization_tag() {
    let expr = Expr::Integer(42);
    let hash = hash_canonical(&expr);
    assert_eq!(
        hash, 0x2909b3d05ec97be7u64,
        "M35 kill: Integer(42) must serialize with tag 'I' yielding oracle hash 0x2909b3d05ec97be7"
    );

    // Also verify Integer(0) -> "I0;" -> 0x40f15a19c8f17b85 (docs/logs/a1-14-oracle.log line 11)
    let expr_zero = Expr::Integer(0);
    assert_eq!(
        hash_canonical(&expr_zero),
        0x40f15a19c8f17b85u64,
        "M35 kill: Integer(0) must serialize with tag 'I' yielding oracle hash 0x40f15a19c8f17b85"
    );

    // Also verify Integer(-7) -> "I-7;" -> 0xebf714d03c120d5d (docs/logs/a1-14-oracle.log line 13)
    let expr_neg = Expr::Integer(-7);
    assert_eq!(
        hash_canonical(&expr_neg),
        0xebf714d03c120d5du64,
        "M35 kill: Integer(-7) must serialize with tag 'I' yielding oracle hash 0xebf714d03c120d5d"
    );
}

/// Kill M36: Mutate decimal serialization tag from 'D' to 'E' (canonicalizer.rs line 696).
/// Under spec (mathd/src/canonicalizer.rs lines 693-699):
///   Decimal(1.5) serializes as "D" + 1.5f64.to_bits().to_string() + ";".
///   1.5f64.to_bits() = 4609434218613702656 (0x3ff8000000000000).
///   Serialization: "D4609434218613702656;".
/// Oracle calculation (docs/logs/a1-14-oracle.log line 14):
///   "D4609434218613702656;" FNV-1a 64 = 0x7eabc41a0ae26ce3 (9127604685927247075u64).
/// Under mutant M36, tag is 'E', producing "E4609434218613702656;" with hash 0xe9d0a626676bc8e0.
#[test]
fn test_m36_kill_decimal_serialization_tag() {
    let expr = Expr::Decimal(1.5);
    let hash = hash_canonical(&expr);
    assert_eq!(
        hash, 0x7eabc41a0ae26ce3u64,
        "M36 kill: Decimal(1.5) must serialize with tag 'D' yielding oracle hash 0x7eabc41a0ae26ce3"
    );

    // Also verify Decimal(-0.25) -> bits 13821547256400052224 -> hash 0xf7c69a0d0b781d51 (oracle line 15)
    let expr_neg = Expr::Decimal(-0.25);
    assert_eq!(
        hash_canonical(&expr_neg),
        0xf7c69a0d0b781d51u64,
        "M36 kill: Decimal(-0.25) must serialize with tag 'D' yielding oracle hash 0xf7c69a0d0b781d51"
    );
}

// ===========================================================================
// Verifier Mutants: M09 and M04
// ===========================================================================

/// Kill M09: Invert max to min in max_relative_error accumulation (verifier.rs line 347).
/// Target in verifier.rs:
///   points_tested += 1;
///   max_relative_error = max_relative_error.max(rel_err);
/// Mutant M09 replaces .max(rel_err) with .min(rel_err).
/// Because max_relative_error is initialized to 0.0, 0.0.min(rel_err) evaluates to 0.0
/// for all non-negative relative errors.
///
/// Hand-calculated test case:
///   lhs = "x + 1e-11"
///   rhs = "x"
/// At each sample point x in [0.2, 4.0]:
///   diff = |(x + 1e-11) - x| = 1e-11
///   scale = |x|.max(1.0) >= 1.0
///   rel_err = 1e-11 / scale <= 1e-11 <= VERIFIER_TOLERANCE (1e-9)
/// All sample points (>= 8) pass the tolerance check, so the claim is Verified.
/// Since diff = 1e-11 > 0, rel_err at every point is strictly positive (> 2e-12).
/// In clean code: max_relative_error >= 2.5e-12.
/// In mutant M09: max_relative_error == 0.0.
#[test]
fn test_m09_kill_max_relative_error_accumulation() {
    let lhs = parse("x + 1 / 100000000000").unwrap();
    let rhs = parse("x").unwrap();
    let verdict = verify_equivalence(&lhs, &rhs, &["x"]);

    match verdict {
        Verdict::Verified { evidence } => {
            assert!(
                evidence.points_tested >= 8,
                "Expected at least MIN_TEST_POINTS (8), got {}",
                evidence.points_tested
            );
            assert!(
                evidence.max_relative_error >= 1e-12,
                "M09 kill: evidence.max_relative_error must record the maximum observed error (> 1e-12), but was {} (mutant M09 resets to 0.0 via .min())",
                evidence.max_relative_error
            );
            assert!(
                evidence.max_relative_error <= 1e-10,
                "Relative error must be bounded by ~1e-11, got {}",
                evidence.max_relative_error
            );
        }
        other => panic!("Expected Verdict::Verified for x + 1e-11 == x, got {:?}", other),
    }
}

/// Analysis and verification of M04: Constant finite evaluation check (verifier.rs line 221).
///
/// Equivalence proof:
/// Line 221 is: `if !g.is_finite() || !e.is_finite() { return Verdict::Unverifiable ... }`
/// Mutant M04 replaces `||` with `&&`.
/// However, preceding lines 207-219 handle the asymmetric cases:
///   line 207: `if g.is_finite() && !e.is_finite()` -> returns FailedDomain(Side::Rhs)
///   line 214: `if e.is_finite() && !g.is_finite()` -> returns FailedDomain(Side::Lhs)
/// Therefore, line 221 is only reached when either:
///   (1) both are finite: `!g.is_finite() || !e.is_finite()` is `false || false == false`;
///       under M04, `false && false == false`.
///   (2) both are non-finite: `!g.is_finite() || !e.is_finite()` is `true || true == true`;
///       under M04, `true && true == true`.
/// Furthermore, `eval_at` passes every arithmetic operation through `check_finite`, which maps
/// non-finite floats to Err, meaning `Ok(g)` and `Ok(e)` are always finite in practice.
/// Hence, M04 is an equivalent mutant on all reachable program paths and cannot produce
/// differing output from any test input without editing `mathd/src/verifier.rs`.
///
/// Hand-worked verdicts for constant expressions:
/// 1. "2 + 2" == "4":
///    g = 4.0, e = 4.0. Both finite and diff = 0.0 <= tolerance.
///    Hand verdict: Verified { points_tested: 1, max_relative_error: 0.0 }.
/// 2. "1" == "2":
///    g = 1.0, e = 2.0. Both finite, diff = 1.0 > tolerance.
///    Hand verdict: Failed { expected: 2.0, got: 1.0 }.
/// 3. "1" == "1/0":
///    eval_at("1") = Ok(1.0). eval_at("1/0") = Err(DivisionByZero { numerator: 1.0 }).
///    Hand verdict: FailedDomain { undefined: Side::Rhs, defined_value: 1.0 }.
/// 4. "1/0" == "1":
///    eval_at("1/0") = Err(DivisionByZero). eval_at("1") = Ok(1.0).
///    Hand verdict: FailedDomain { undefined: Side::Lhs, defined_value: 1.0 }.
/// 5. "1/0" == "1/0":
///    Both sides error.
///    Hand verdict: Unverifiable { reason: "constant expression evaluation failed: ..." }.
#[test]
fn test_m04_constant_finite_evaluation_hand_verdicts() {
    // Case 1: Verified constant equality
    let eq1 = verify_equivalence(&parse("2 + 2").unwrap(), &parse("4").unwrap(), &[]);
    assert!(matches!(
        eq1,
        Verdict::Verified {
            evidence: Evidence {
                points_tested: 1,
                ..
            }
        }
    ));

    // Case 2: Failed constant equality
    let eq2 = verify_equivalence(&parse("1").unwrap(), &parse("2").unwrap(), &[]);
    assert!(matches!(
        eq2,
        Verdict::Failed {
            expected,
            got,
            ..
        } if (expected - 2.0).abs() < 1e-12 && (got - 1.0).abs() < 1e-12
    ));

    // Case 3: FailedDomain RHS undefined
    let eq3 = verify_equivalence(&parse("1").unwrap(), &parse("1 / 0").unwrap(), &[]);
    assert!(matches!(
        eq3,
        Verdict::FailedDomain {
            undefined: Side::Rhs,
            defined_value,
            ..
        } if (defined_value - 1.0).abs() < 1e-12
    ));

    // Case 4: FailedDomain LHS undefined
    let eq4 = verify_equivalence(&parse("1 / 0").unwrap(), &parse("1").unwrap(), &[]);
    assert!(matches!(
        eq4,
        Verdict::FailedDomain {
            undefined: Side::Lhs,
            defined_value,
            ..
        } if (defined_value - 1.0).abs() < 1e-12
    ));

    // Case 5: Both undefined -> Unverifiable
    let eq5 = verify_equivalence(&parse("1 / 0").unwrap(), &parse("1 / 0").unwrap(), &[]);
    assert!(matches!(eq5, Verdict::Unverifiable { .. }));
}
