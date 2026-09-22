//! Numeric evaluator: canonical AST → number, with oracle verification.
//!
//! Evaluates expressions to floating-point numbers. All results must be verified
//! by an independent oracle (SymPy) before being trusted.
//!
//! This module computes the value; the oracle verifies it independently.

use crate::parser::{BinOp, Expr, UnaryOp};
use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct EvaluationError {
    pub message: String,
}

impl std::fmt::Display for EvaluationError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for EvaluationError {}

/// Evaluate a canonical expression to a number.
///
/// Variables must be supplied in the context. All results MUST be verified
/// by an external oracle before trusting.
pub fn evaluate(expr: &Expr, context: &HashMap<String, f64>) -> Result<f64, EvaluationError> {
    match expr {
        Expr::Number(n) => Ok(*n),

        Expr::Variable(name) => context.get(name).copied().ok_or_else(|| EvaluationError {
            message: format!("undefined variable: {}", name),
        }),

        Expr::UnaryOp { op, operand } => {
            let val = evaluate(operand, context)?;
            match op {
                UnaryOp::Pos => Ok(val),
                UnaryOp::Neg => Ok(-val),
            }
        }

        Expr::BinOp { op, left, right } => {
            let lval = evaluate(left, context)?;
            let rval = evaluate(right, context)?;
            match op {
                BinOp::Add => Ok(lval + rval),
                BinOp::Sub => Ok(lval - rval),
                BinOp::Mul => Ok(lval * rval),
                BinOp::Div => {
                    if rval == 0.0 {
                        Err(EvaluationError {
                            message: "division by zero".to_string(),
                        })
                    } else {
                        Ok(lval / rval)
                    }
                }
                BinOp::Pow => Ok(lval.powf(rval)),
            }
        }

        Expr::Call { func, args } => {
            match func.as_str() {
                "sin" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "sin expects 1 argument".to_string(),
                        });
                    }
                    evaluate(&args[0], context).map(|x| x.sin())
                }
                "cos" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "cos expects 1 argument".to_string(),
                        });
                    }
                    evaluate(&args[0], context).map(|x| x.cos())
                }
                "tan" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "tan expects 1 argument".to_string(),
                        });
                    }
                    evaluate(&args[0], context).map(|x| x.tan())
                }
                "sqrt" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "sqrt expects 1 argument".to_string(),
                        });
                    }
                    let val = evaluate(&args[0], context)?;
                    if val < 0.0 {
                        Err(EvaluationError {
                            message: "sqrt of negative number".to_string(),
                        })
                    } else {
                        Ok(val.sqrt())
                    }
                }
                "abs" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "abs expects 1 argument".to_string(),
                        });
                    }
                    evaluate(&args[0], context).map(|x| x.abs())
                }
                "ln" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "ln expects 1 argument".to_string(),
                        });
                    }
                    let val = evaluate(&args[0], context)?;
                    if val <= 0.0 {
                        Err(EvaluationError {
                            message: "ln of non-positive number".to_string(),
                        })
                    } else {
                        Ok(val.ln())
                    }
                }
                "exp" => {
                    if args.len() != 1 {
                        return Err(EvaluationError {
                            message: "exp expects 1 argument".to_string(),
                        });
                    }
                    evaluate(&args[0], context).map(|x| x.exp())
                }
                "max" => {
                    if args.is_empty() {
                        return Err(EvaluationError {
                            message: "max expects at least 1 argument".to_string(),
                        });
                    }
                    args.iter()
                        .try_fold(f64::NEG_INFINITY, |acc, arg| {
                            evaluate(arg, context).map(|val| acc.max(val))
                        })
                }
                "min" => {
                    if args.is_empty() {
                        return Err(EvaluationError {
                            message: "min expects at least 1 argument".to_string(),
                        });
                    }
                    args.iter()
                        .try_fold(f64::INFINITY, |acc, arg| {
                            evaluate(arg, context).map(|val| acc.min(val))
                        })
                }
                _ => Err(EvaluationError {
                    message: format!("unknown function: {}", func),
                }),
            }
        }
    }
}

/// Oracle request: ask external verifier to check a computation.
///
/// In real use, this spawns SymPy via subprocess and verifies the result.
/// For now, it's a stub that documents the interface.
pub struct OracleRequest {
    pub expr_text: String,
    pub context: HashMap<String, f64>,
    pub claimed_result: f64,
}

pub struct OracleVerification {
    pub accepted: bool,
    pub oracle_result: Option<f64>,
    pub reasoning: String,
}

/// Submit to oracle for verification.
///
/// Real implementation: spawn Python subprocess, run SymPy, capture result.
/// This stub always accepts and documents the interface.
pub fn verify_with_oracle(request: OracleRequest) -> OracleVerification {
    OracleVerification {
        accepted: true, // Stub: assumes oracle would accept
        oracle_result: Some(request.claimed_result),
        reasoning: "oracle verification stub (requires SymPy subprocess)".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse;
    use crate::canonicalizer::canonicalize;

    #[test]
    fn eval_number() {
        let expr = canonicalize(&parse("42").unwrap());
        assert_eq!(evaluate(&expr, &HashMap::new()).unwrap(), 42.0);
    }

    #[test]
    fn eval_variable() {
        let expr = canonicalize(&parse("x").unwrap());
        let mut ctx = HashMap::new();
        ctx.insert("x".to_string(), 3.0);
        assert_eq!(evaluate(&expr, &ctx).unwrap(), 3.0);
    }

    #[test]
    fn eval_binary_op() {
        let expr = canonicalize(&parse("2 + 3").unwrap());
        assert_eq!(evaluate(&expr, &HashMap::new()).unwrap(), 5.0);
    }

    #[test]
    fn eval_division_by_zero() {
        let expr = canonicalize(&parse("1 / 0").unwrap());
        assert!(evaluate(&expr, &HashMap::new()).is_err());
    }

    #[test]
    fn eval_function() {
        let expr = canonicalize(&parse("sqrt(4)").unwrap());
        assert_eq!(evaluate(&expr, &HashMap::new()).unwrap(), 2.0);
    }

    #[test]
    fn eval_power() {
        let expr = canonicalize(&parse("2 ^ 3").unwrap());
        assert_eq!(evaluate(&expr, &HashMap::new()).unwrap(), 8.0);
    }
}
