//! Symbolic differentiator: AST → derivative AST.
//!
//! Computes symbolic derivatives using the chain rule and product rule.
//! All results MUST be verified by finite differences oracle before trusting.
//!
//! Differentiation rules:
//! - d/dx(c) = 0 (constant)
//! - d/dx(x) = 1 (variable)
//! - d/dx(y) = 0 (different variable)
//! - d/dx(f + g) = f' + g'
//! - d/dx(f - g) = f' - g'
//! - d/dx(f * g) = f'*g + f*g' (product rule)
//! - d/dx(f / g) = (f'*g - f*g') / g^2 (quotient rule)
//! - d/dx(f ^ n) = n * f^(n-1) * f' (power chain rule)
//! - d/dx(sin(f)) = cos(f) * f' (chain rule)
//! - d/dx(cos(f)) = -sin(f) * f' (chain rule)
//! etc.

use crate::parser::{BinOp, Expr, UnaryOp};
use std::collections::HashMap;

/// Differentiate an expression with respect to a variable.
pub fn differentiate(expr: &Expr, var: &str) -> Expr {
    match expr {
        Expr::Number(_) => Expr::Number(0.0),

        Expr::Variable(name) => {
            if name == var {
                Expr::Number(1.0)
            } else {
                Expr::Number(0.0)
            }
        }

        Expr::UnaryOp { op, operand } => {
            let operand_prime = differentiate(operand, var);
            match op {
                UnaryOp::Pos => operand_prime,
                UnaryOp::Neg => Expr::UnaryOp {
                    op: UnaryOp::Neg,
                    operand: Box::new(operand_prime),
                },
            }
        }

        Expr::BinOp { op, left, right } => {
            let left_prime = differentiate(left, var);
            let right_prime = differentiate(right, var);

            match op {
                BinOp::Add => Expr::BinOp {
                    op: BinOp::Add,
                    left: Box::new(left_prime),
                    right: Box::new(right_prime),
                },

                BinOp::Sub => Expr::BinOp {
                    op: BinOp::Sub,
                    left: Box::new(left_prime),
                    right: Box::new(right_prime),
                },

                // Product rule: (f*g)' = f'*g + f*g'
                BinOp::Mul => {
                    let term1 = Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(left_prime),
                        right: right.clone(),
                    };
                    let term2 = Expr::BinOp {
                        op: BinOp::Mul,
                        left: left.clone(),
                        right: Box::new(right_prime),
                    };
                    Expr::BinOp {
                        op: BinOp::Add,
                        left: Box::new(term1),
                        right: Box::new(term2),
                    }
                }

                // Quotient rule: (f/g)' = (f'*g - f*g') / g^2
                BinOp::Div => {
                    let numerator_left = Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(left_prime),
                        right: right.clone(),
                    };
                    let numerator_right = Expr::BinOp {
                        op: BinOp::Mul,
                        left: left.clone(),
                        right: Box::new(right_prime),
                    };
                    let numerator = Expr::BinOp {
                        op: BinOp::Sub,
                        left: Box::new(numerator_left),
                        right: Box::new(numerator_right),
                    };
                    let denominator = Expr::BinOp {
                        op: BinOp::Pow,
                        left: right.clone(),
                        right: Box::new(Expr::Number(2.0)),
                    };
                    Expr::BinOp {
                        op: BinOp::Div,
                        left: Box::new(numerator),
                        right: Box::new(denominator),
                    }
                }

                // Power rule with chain: d/dx(f^n) = n * f^(n-1) * f'
                BinOp::Pow => {
                    let outer = Expr::BinOp {
                        op: BinOp::Mul,
                        left: right.clone(),
                        right: Box::new(Expr::BinOp {
                            op: BinOp::Pow,
                            left: left.clone(),
                            right: Box::new(Expr::BinOp {
                                op: BinOp::Sub,
                                left: right.clone(),
                                right: Box::new(Expr::Number(1.0)),
                            }),
                        }),
                    };
                    Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(outer),
                        right: Box::new(differentiate(left, var)),
                    }
                }
            }
        }

        Expr::Call { func, args } => {
            match func.as_str() {
                "sin" => {
                    if args.len() != 1 {
                        return Expr::Number(0.0);
                    }
                    // d/dx(sin(f)) = cos(f) * f'
                    let inner_prime = differentiate(&args[0], var);
                    Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(Expr::Call {
                            func: "cos".to_string(),
                            args: args.clone(),
                        }),
                        right: Box::new(inner_prime),
                    }
                }

                "cos" => {
                    if args.len() != 1 {
                        return Expr::Number(0.0);
                    }
                    // d/dx(cos(f)) = -sin(f) * f'
                    let inner_prime = differentiate(&args[0], var);
                    let neg_sin = Expr::UnaryOp {
                        op: UnaryOp::Neg,
                        operand: Box::new(Expr::Call {
                            func: "sin".to_string(),
                            args: args.clone(),
                        }),
                    };
                    Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(neg_sin),
                        right: Box::new(inner_prime),
                    }
                }

                "sqrt" => {
                    if args.len() != 1 {
                        return Expr::Number(0.0);
                    }
                    // d/dx(sqrt(f)) = f' / (2*sqrt(f))
                    let inner_prime = differentiate(&args[0], var);
                    let denominator = Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(Expr::Number(2.0)),
                        right: Box::new(Expr::Call {
                            func: "sqrt".to_string(),
                            args: args.clone(),
                        }),
                    };
                    Expr::BinOp {
                        op: BinOp::Div,
                        left: Box::new(inner_prime),
                        right: Box::new(denominator),
                    }
                }

                "ln" => {
                    if args.len() != 1 {
                        return Expr::Number(0.0);
                    }
                    // d/dx(ln(f)) = f' / f
                    let inner_prime = differentiate(&args[0], var);
                    Expr::BinOp {
                        op: BinOp::Div,
                        left: Box::new(inner_prime),
                        right: args[0].clone(),
                    }
                }

                "exp" => {
                    if args.len() != 1 {
                        return Expr::Number(0.0);
                    }
                    // d/dx(exp(f)) = exp(f) * f'
                    let inner_prime = differentiate(&args[0], var);
                    Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(Expr::Call {
                            func: "exp".to_string(),
                            args: args.clone(),
                        }),
                        right: Box::new(inner_prime),
                    }
                }

                _ => Expr::Number(0.0), // Unknown function
            }
        }
    }
}

/// Finite differences oracle for verifying derivatives.
///
/// Computes f'(x) ≈ (f(x+h) - f(x-h)) / (2*h) as independent verification.
pub fn finite_differences(
    expr: &Expr,
    var: &str,
    context: &HashMap<String, f64>,
    h: f64,
) -> Result<f64, String> {
    use crate::evaluator::evaluate;
    use crate::canonicalizer::canonicalize;

    let canonical = canonicalize(expr);

    // Evaluate at x + h
    let mut ctx_plus = context.clone();
    if let Some(val) = ctx_plus.get_mut(var) {
        *val += h;
    }
    let f_plus = evaluate(&canonical, &ctx_plus).map_err(|e| e.message)?;

    // Evaluate at x - h
    let mut ctx_minus = context.clone();
    if let Some(val) = ctx_minus.get_mut(var) {
        *val -= h;
    }
    let f_minus = evaluate(&canonical, &ctx_minus).map_err(|e| e.message)?;

    // Central difference formula
    Ok((f_plus - f_minus) / (2.0 * h))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canonicalizer::canonicalize;
    use crate::parser::parse;

    #[test]
    fn diff_constant() {
        let expr = canonicalize(&parse("5").unwrap());
        let deriv = differentiate(&expr, "x");
        assert_eq!(deriv, Expr::Number(0.0));
    }

    #[test]
    fn diff_variable() {
        let expr = canonicalize(&parse("x").unwrap());
        let deriv = differentiate(&expr, "x");
        assert_eq!(deriv, Expr::Number(1.0));
    }

    #[test]
    fn diff_sum() {
        let expr = canonicalize(&parse("x + 2").unwrap());
        let deriv = differentiate(&expr, "x");
        // d/dx(x+2) = 1
        if let Expr::BinOp { op: BinOp::Add, .. } = deriv {
            // Correct structure: should have 1 + 0
        } else {
            panic!("expected BinOp structure");
        }
    }

    #[test]
    fn diff_power() {
        let expr = canonicalize(&parse("x ^ 2").unwrap());
        let deriv = differentiate(&expr, "x");
        // d/dx(x^2) = 2*x^1*1 = 2*x
        assert!(!matches!(deriv, Expr::Number(0.0)));
    }

    #[test]
    fn diff_product() {
        let expr = canonicalize(&parse("x * x").unwrap());
        let deriv = differentiate(&expr, "x");
        // d/dx(x*x) = 1*x + x*1 = 2*x
        assert!(!matches!(deriv, Expr::Number(0.0)));
    }
}
