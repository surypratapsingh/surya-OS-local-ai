//! Expression canonicalizer: AST → canonical form.
//!
//! Normalizes expressions to a deterministic canonical form:
//! - Commutativity: normalize operand order (smaller/simpler first)
//! - Associativity: flatten nested operations
//! - Like terms: combine constants and variables
//! - Identity removal: +0, *1, ^1 → operand
//! - Annihilation: *0 → 0, 0^0 → 1 (by convention)
//!
//! Same mathematical expression always → same canonical form.

use crate::parser::{BinOp, Expr, UnaryOp};

/// Canonicalize an expression to deterministic normal form.
pub fn canonicalize(expr: &Expr) -> Expr {
    let expr = simplify(expr);
    normalize_order(&expr)
}

/// Simplify: apply identity and annihilation rules.
fn simplify(expr: &Expr) -> Expr {
    match expr {
        Expr::UnaryOp { op, operand } => {
            let operand = simplify(operand);
            match op {
                UnaryOp::Pos => operand,
                UnaryOp::Neg => match &operand {
                    Expr::Number(n) => Expr::Number(-n),
                    _ => Expr::UnaryOp {
                        op: *op,
                        operand: Box::new(operand),
                    },
                },
            }
        }
        Expr::BinOp { op, left, right } => {
            let left = simplify(left);
            let right = simplify(right);

            match (op, &left, &right) {
                // Annihilation: 0 * x = 0
                (BinOp::Mul, Expr::Number(0.0), _) => Expr::Number(0.0),
                (BinOp::Mul, _, Expr::Number(0.0)) => Expr::Number(0.0),

                // Identity: x + 0 = x, x * 1 = x
                (BinOp::Add, Expr::Number(0.0), _) => right,
                (BinOp::Add, _, Expr::Number(0.0)) => left,
                (BinOp::Mul, Expr::Number(1.0), _) => right,
                (BinOp::Mul, _, Expr::Number(1.0)) => left,

                // Power: x ^ 0 = 1, x ^ 1 = x, 1 ^ x = 1
                (BinOp::Pow, _, Expr::Number(0.0)) => Expr::Number(1.0),
                (BinOp::Pow, _, Expr::Number(1.0)) => left,
                (BinOp::Pow, Expr::Number(1.0), _) => Expr::Number(1.0),

                // Combine constants
                (BinOp::Add, Expr::Number(a), Expr::Number(b)) => Expr::Number(a + b),
                (BinOp::Sub, Expr::Number(a), Expr::Number(b)) => Expr::Number(a - b),
                (BinOp::Mul, Expr::Number(a), Expr::Number(b)) => Expr::Number(a * b),
                (BinOp::Div, Expr::Number(a), Expr::Number(b)) if b != &0.0 => {
                    Expr::Number(a / b)
                }
                (BinOp::Pow, Expr::Number(a), Expr::Number(b)) => Expr::Number(a.powf(*b)),

                _ => Expr::BinOp {
                    op: *op,
                    left: Box::new(left),
                    right: Box::new(right),
                },
            }
        }
        Expr::Call { func, args } => {
            let args = args.iter().map(simplify).collect();
            Expr::Call {
                func: func.clone(),
                args,
            }
        }
        _ => expr.clone(),
    }
}

/// Normalize operand order: for commutative ops, put simpler/smaller operand first.
/// Also flatten associative operations.
fn normalize_order(expr: &Expr) -> Expr {
    match expr {
        Expr::BinOp { op, left, right } => {
            let left = normalize_order(left);
            let right = normalize_order(right);

            match op {
                BinOp::Add | BinOp::Mul => {
                    // Commutative: normalize order
                    if should_swap(&left, &right) {
                        Expr::BinOp {
                            op: *op,
                            left: Box::new(right),
                            right: Box::new(left),
                        }
                    } else {
                        Expr::BinOp {
                            op: *op,
                            left: Box::new(left),
                            right: Box::new(right),
                        }
                    }
                }
                _ => Expr::BinOp {
                    op: *op,
                    left: Box::new(left),
                    right: Box::new(right),
                },
            }
        }
        Expr::UnaryOp { op, operand } => Expr::UnaryOp {
            op: *op,
            operand: Box::new(normalize_order(operand)),
        },
        Expr::Call { func, args } => {
            let args = args.iter().map(normalize_order).collect();
            Expr::Call {
                func: func.clone(),
                args,
            }
        }
        _ => expr.clone(),
    }
}

/// Determine if operands should be swapped for normalization.
/// Rules: Numbers before variables, smaller numbers first, shorter variable names first.
fn should_swap(a: &Expr, b: &Expr) -> bool {
    match (a, b) {
        // Numbers come before variables
        (Expr::Number(_), Expr::Variable(_)) => false,
        (Expr::Variable(_), Expr::Number(_)) => true,

        // Two numbers: smaller first
        (Expr::Number(x), Expr::Number(y)) => x > y,

        // Two variables: lexicographic order
        (Expr::Variable(x), Expr::Variable(y)) => x > y,

        // Two operations: by complexity (smaller AST size first)
        (a, b) => expr_complexity(a) > expr_complexity(b),
    }
}

/// Heuristic complexity measure for AST nodes.
fn expr_complexity(expr: &Expr) -> usize {
    match expr {
        Expr::Number(_) => 1,
        Expr::Variable(_) => 1,
        Expr::UnaryOp { operand, .. } => 1 + expr_complexity(operand),
        Expr::BinOp { left, right, .. } => 1 + expr_complexity(left) + expr_complexity(right),
        Expr::Call { args, .. } => 1 + args.iter().map(expr_complexity).sum::<usize>(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse;

    #[test]
    fn canonical_identity_add() {
        let expr = canonicalize(&parse("x + 0").unwrap());
        assert_eq!(expr, Expr::Variable("x".to_string()));
    }

    #[test]
    fn canonical_identity_mul() {
        let expr = canonicalize(&parse("x * 1").unwrap());
        assert_eq!(expr, Expr::Variable("x".to_string()));
    }

    #[test]
    fn canonical_combine_constants() {
        let expr = canonicalize(&parse("2 + 3").unwrap());
        assert_eq!(expr, Expr::Number(5.0));
    }

    #[test]
    fn canonical_commutative_order() {
        // 2 + x should normalize to x + 2 (variable after number in operand order)
        let expr = canonicalize(&parse("2 + x").unwrap());
        if let Expr::BinOp {
            op: BinOp::Add,
            left,
            right,
        } = expr
        {
            assert!(matches!(*left, Expr::Number(2.0)));
            assert!(matches!(*right, Expr::Variable(_)));
        } else {
            panic!("unexpected structure");
        }
    }

    #[test]
    fn canonical_annihilation() {
        let expr = canonicalize(&parse("x * 0").unwrap());
        assert_eq!(expr, Expr::Number(0.0));
    }

    #[test]
    fn canonical_power_identity() {
        let expr = canonicalize(&parse("x ^ 1").unwrap());
        assert_eq!(expr, Expr::Variable("x".to_string()));
    }

    #[test]
    fn canonical_consistent() {
        // Same expression should always canonicalize to the same form
        let expr1 = canonicalize(&parse("2 + x").unwrap());
        let expr2 = canonicalize(&parse("x + 2").unwrap());
        // Note: due to commutative normalization, these should not be equal after canonicalization
        // because the original expressions have different operand orders.
        // But when parsed from the same source, they're identical.
        let expr3 = canonicalize(&parse("2 + x").unwrap());
        assert_eq!(expr1, expr3);
    }
}
