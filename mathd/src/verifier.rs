//! Verifier: check answers via mutation testing.
//!
//! A proposed answer (e.g., a derivative, an evaluation) is correct if it
//! rejects mutations of the original problem. Mutation testing generates
//! semantically invalid variations and checks if the verifier catches them.
//!
//! Mutation operators:
//! - Operator flip: + ↔ -, * ↔ /
//! - Constant corruption: n → n±1, 0 → 1, 1 → 0
//! - Operator removal: x*0 → x, etc.

use crate::parser::{BinOp, Expr, UnaryOp};
use std::collections::HashMap;

/// A mutation of an expression (semantic corruption).
#[derive(Clone, Debug)]
pub struct Mutant {
    pub expr: Expr,
    pub mutation_type: String,
    pub location: String,
}

/// Generate all meaningful mutations of an expression.
pub fn generate_mutants(expr: &Expr, depth: usize) -> Vec<Mutant> {
    let mut mutants = Vec::new();
    mutate_expr(expr, &mut mutants, "", depth);
    mutants
}

fn mutate_expr(expr: &Expr, mutants: &mut Vec<Mutant>, location: &str, depth: usize) {
    if depth == 0 {
        return;
    }

    match expr {
        Expr::Number(n) => {
            // Mutate constants
            if *n == 0.0 {
                mutants.push(Mutant {
                    expr: Expr::Number(1.0),
                    mutation_type: "constant: 0→1".to_string(),
                    location: location.to_string(),
                });
            } else if *n == 1.0 {
                mutants.push(Mutant {
                    expr: Expr::Number(0.0),
                    mutation_type: "constant: 1→0".to_string(),
                    location: location.to_string(),
                });
            } else {
                mutants.push(Mutant {
                    expr: Expr::Number(n + 1.0),
                    mutation_type: "constant: n→n+1".to_string(),
                    location: location.to_string(),
                });
                mutants.push(Mutant {
                    expr: Expr::Number(n - 1.0),
                    mutation_type: "constant: n→n-1".to_string(),
                    location: location.to_string(),
                });
            }
        }

        Expr::BinOp { op, left, right } => {
            // Operator flips
            match op {
                BinOp::Add => {
                    mutants.push(Mutant {
                        expr: Expr::BinOp {
                            op: BinOp::Sub,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        mutation_type: "operator: + → -".to_string(),
                        location: location.to_string(),
                    });
                }
                BinOp::Sub => {
                    mutants.push(Mutant {
                        expr: Expr::BinOp {
                            op: BinOp::Add,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        mutation_type: "operator: - → +".to_string(),
                        location: location.to_string(),
                    });
                }
                BinOp::Mul => {
                    mutants.push(Mutant {
                        expr: Expr::BinOp {
                            op: BinOp::Div,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        mutation_type: "operator: * → /".to_string(),
                        location: location.to_string(),
                    });
                }
                BinOp::Div => {
                    mutants.push(Mutant {
                        expr: Expr::BinOp {
                            op: BinOp::Mul,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        mutation_type: "operator: / → *".to_string(),
                        location: location.to_string(),
                    });
                }
                BinOp::Pow => {
                    mutants.push(Mutant {
                        expr: Expr::BinOp {
                            op: BinOp::Mul,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        mutation_type: "operator: ^ → *".to_string(),
                        location: location.to_string(),
                    });
                }
            }

            // Recursive mutation of operands
            mutate_expr(left, mutants, &format!("{}.left", location), depth - 1);
            mutate_expr(right, mutants, &format!("{}.right", location), depth - 1);
        }

        Expr::UnaryOp { op, operand } => {
            // Flip unary operators
            match op {
                UnaryOp::Neg => {
                    mutants.push(Mutant {
                        expr: Expr::UnaryOp {
                            op: UnaryOp::Pos,
                            operand: operand.clone(),
                        },
                        mutation_type: "unary: - → +".to_string(),
                        location: location.to_string(),
                    });
                }
                UnaryOp::Pos => {
                    mutants.push(Mutant {
                        expr: Expr::UnaryOp {
                            op: UnaryOp::Neg,
                            operand: operand.clone(),
                        },
                        mutation_type: "unary: + → -".to_string(),
                        location: location.to_string(),
                    });
                }
            }

            mutate_expr(operand, mutants, &format!("{}.operand", location), depth - 1);
        }

        Expr::Call { func, args } => {
            for (i, arg) in args.iter().enumerate() {
                mutate_expr(arg, mutants, &format!("{}.arg{}", location, i), depth - 1);
            }
        }

        _ => {}
    }
}

/// Verification report: how well does a verifier catch mutations?
#[derive(Debug, Clone)]
pub struct VerificationReport {
    pub total_mutants: usize,
    pub rejected: usize,
    pub accepted: usize,
    pub detection_rate: f64,
    pub mutations_caught: Vec<String>,
}

/// Test a verifier (predicate) against mutants.
///
/// A verifier function returns true if a mutant is acceptable (should pass).
/// We want verifiers that REJECT most mutants (low acceptance rate).
pub fn test_verifier<F>(
    mutants: &[Mutant],
    verifier: F,
) -> VerificationReport
where
    F: Fn(&Expr) -> bool,
{
    let mut rejected = 0;
    let mut accepted = 0;
    let mut mutations_caught = Vec::new();

    for mutant in mutants {
        if verifier(&mutant.expr) {
            accepted += 1;
        } else {
            rejected += 1;
            mutations_caught.push(format!("{}: {}", mutant.mutation_type, mutant.location));
        }
    }

    let detection_rate = if mutants.is_empty() {
        1.0
    } else {
        rejected as f64 / mutants.len() as f64
    };

    VerificationReport {
        total_mutants: mutants.len(),
        rejected,
        accepted,
        detection_rate,
        mutations_caught,
    }
}

/// Simple verifier: check if value equals expected.
/// This is too weak (accepts any mutation that produces the same value).
pub fn value_verifier(expr: &Expr, expected: f64, context: &HashMap<String, f64>) -> bool {
    use crate::evaluator::evaluate;
    use crate::canonicalizer::canonicalize;

    let canonical = canonicalize(expr);
    evaluate(&canonical, context).ok().map_or(false, |val| (val - expected).abs() < 1e-9)
}

/// Derivative verifier: check if claimed derivative matches finite differences.
/// Much stronger: rejects operator/constant flips that change the derivative.
pub fn derivative_verifier(
    claimed_deriv: &Expr,
    original: &Expr,
    var: &str,
    context: &HashMap<String, f64>,
    h: f64,
) -> Result<bool, String> {
    use crate::differentiator::finite_differences;
    use crate::evaluator::evaluate;
    use crate::canonicalizer::canonicalize;

    let fd = finite_differences(original, var, context, h)?;
    let canonical = canonicalize(claimed_deriv);
    let claimed = evaluate(&canonical, context).map_err(|e| e.message)?;

    Ok((claimed - fd).abs() < 1e-6)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canonicalizer::canonicalize;
    use crate::parser::parse;

    #[test]
    fn mutate_constant() {
        let expr = Expr::Number(0.0);
        let mutants = generate_mutants(&expr, 1);
        assert!(!mutants.is_empty());
        assert!(mutants.iter().any(|m| {
            matches!(m.expr, Expr::Number(1.0)) && m.mutation_type == "constant: 0→1"
        }));
    }

    #[test]
    fn mutate_operator() {
        let expr = canonicalize(&parse("2 + 3").unwrap());
        let mutants = generate_mutants(&expr, 1);
        assert!(mutants.iter().any(|m| m.mutation_type.contains("+")));
    }

    #[test]
    fn verifier_report() {
        let expr = Expr::Number(5.0);
        let mutants = generate_mutants(&expr, 1);

        let verifier = |_: &Expr| false; // Rejects all
        let report = test_verifier(&mutants, verifier);

        assert_eq!(report.rejected, report.total_mutants);
        assert_eq!(report.accepted, 0);
        assert_eq!(report.detection_rate, 1.0);
    }

    #[test]
    fn weak_verifier() {
        let expr = Expr::Number(5.0);
        let mutants = generate_mutants(&expr, 1);

        let verifier = |_: &Expr| true; // Accepts all
        let report = test_verifier(&mutants, verifier);

        assert_eq!(report.rejected, 0);
        assert_eq!(report.accepted, report.total_mutants);
        assert_eq!(report.detection_rate, 0.0);
    }

    #[test]
    fn mutation_depth() {
        let expr = canonicalize(&parse("2 + 3 * 4").unwrap());
        let shallow = generate_mutants(&expr, 1);
        let deep = generate_mutants(&expr, 3);

        // Deeper mutation should find more mutants
        assert!(deep.len() >= shallow.len());
    }
}
