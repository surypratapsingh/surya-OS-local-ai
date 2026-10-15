//! Symbolic differentiator: AST + var -> AST. Work order B4
//! (`docs/work-orders.md`, "B4 · Differentiator").
//!
//! Rules implemented:
//! - Constants (integers, decimals, named constants pi and e): derivative is 0
//! - Variables: d/dx(x) = 1; d/dx(y) = 0 for y != x
//! - Linearity:
//!   - d/dx(+u) = +u'
//!   - d/dx(-u) = -u'
//!   - d/dx(u + v) = u' + v'
//!   - d/dx(u - v) = u' - v'
//! - Product rule:
//!   - d/dx(u * v) = u' * v + u * v'
//! - Quotient rule:
//!   - d/dx(u / v) = (u' * v - u * v') / v^2
//! - Power rules:
//!   - Neither base nor exponent mentions var: 0
//!   - Base mentions var, constant exponent: d/dx(u^c) = c * u^(c - 1) * u'
//!   - Constant base, exponent mentions var: d/dx(c^v) = c^v * ln(c) * v'
//!   - General power: d/dx(u^v) = u^v * (v' * ln(u) + v * u' / u)
//! - Chain rule for all 7 supported functions:
//!   - d/dx(sin(u)) = cos(u) * u'
//!   - d/dx(cos(u)) = -sin(u) * u'
//!   - d/dx(tan(u)) = (1 + tan(u)^2) * u'
//!   - d/dx(exp(u)) = exp(u) * u'
//!   - d/dx(ln(u)) = u' / u
//!   - d/dx(sqrt(u)) = u' / (2 * sqrt(u))
//!   - d/dx(abs(u)) = (u / abs(u)) * u'
//!
//! The output is canonicalised via [`canonicalize`] to fold constant
//! terms, eliminate trivial factors (0 * u -> 0, 1 * u -> u), and produce
//! the deterministic canonical form.

use crate::canonicalizer::canonicalize;
use crate::parser::{BinOp, Expr, Function, UnaryOp};

/// Check whether an expression tree contains a reference to the named variable.
pub fn mentions_var(expr: &Expr, var: &str) -> bool {
    match expr {
        Expr::Integer(_) | Expr::Decimal(_) | Expr::Constant(_) => false,
        Expr::Variable(name) => name == var,
        Expr::UnaryOp { operand, .. } => mentions_var(operand, var),
        Expr::BinOp { left, right, .. } => mentions_var(left, var) || mentions_var(right, var),
        Expr::Call { args, .. } => args.iter().any(|arg| mentions_var(arg, var)),
    }
}

fn int(n: i64) -> Expr {
    Expr::Integer(n)
}

fn add(a: Expr, b: Expr) -> Expr {
    Expr::BinOp {
        op: BinOp::Add,
        left: Box::new(a),
        right: Box::new(b),
    }
}

fn sub(a: Expr, b: Expr) -> Expr {
    Expr::BinOp {
        op: BinOp::Sub,
        left: Box::new(a),
        right: Box::new(b),
    }
}

fn mul(a: Expr, b: Expr) -> Expr {
    Expr::BinOp {
        op: BinOp::Mul,
        left: Box::new(a),
        right: Box::new(b),
    }
}

fn div(a: Expr, b: Expr) -> Expr {
    Expr::BinOp {
        op: BinOp::Div,
        left: Box::new(a),
        right: Box::new(b),
    }
}

fn pow(a: Expr, b: Expr) -> Expr {
    Expr::BinOp {
        op: BinOp::Pow,
        left: Box::new(a),
        right: Box::new(b),
    }
}

fn neg(a: Expr) -> Expr {
    Expr::UnaryOp {
        op: UnaryOp::Neg,
        operand: Box::new(a),
    }
}

fn call(func: Function, arg: Expr) -> Expr {
    Expr::Call {
        func,
        args: vec![arg],
    }
}

/// Differentiate an expression symbolically with respect to `var`, without
/// canonicalisation.
fn differentiate_raw(expr: &Expr, var: &str) -> Expr {
    if !mentions_var(expr, var) {
        return int(0);
    }

    match expr {
        Expr::Integer(_) | Expr::Decimal(_) | Expr::Constant(_) => int(0),
        Expr::Variable(name) => {
            if name == var {
                int(1)
            } else {
                int(0)
            }
        }
        Expr::UnaryOp { op, operand } => match op {
            UnaryOp::Pos => differentiate_raw(operand, var),
            UnaryOp::Neg => neg(differentiate_raw(operand, var)),
        },
        Expr::BinOp { op, left, right } => match op {
            BinOp::Add => add(differentiate_raw(left, var), differentiate_raw(right, var)),
            BinOp::Sub => sub(differentiate_raw(left, var), differentiate_raw(right, var)),
            BinOp::Mul => {
                let u = (**left).clone();
                let v = (**right).clone();
                let du = differentiate_raw(left, var);
                let dv = differentiate_raw(right, var);
                add(mul(du, v), mul(u, dv))
            }
            BinOp::Div => {
                let u = (**left).clone();
                let v = (**right).clone();
                let du = differentiate_raw(left, var);
                let dv = differentiate_raw(right, var);
                let num = sub(mul(du, v.clone()), mul(u, dv));
                let den = pow(v, int(2));
                div(num, den)
            }
            BinOp::Pow => {
                let u = (**left).clone();
                let v = (**right).clone();
                let u_has = mentions_var(&u, var);
                let v_has = mentions_var(&v, var);
                if u_has && !v_has {
                    // Power rule: d/dx(u^c) = c * u^(c - 1) * u'
                    let du = differentiate_raw(&u, var);
                    let c = v.clone();
                    let exp = sub(v, int(1));
                    mul(mul(c, pow(u, exp)), du)
                } else if !u_has && v_has {
                    // Exponential rule: d/dx(c^v) = c^v * ln(c) * v'
                    let dv = differentiate_raw(&v, var);
                    let term = pow(u.clone(), v);
                    let log_c = call(Function::Ln, u);
                    mul(mul(term, log_c), dv)
                } else {
                    // General power rule: d/dx(u^v) = u^v * (v' * ln(u) + v * u' / u)
                    let du = differentiate_raw(&u, var);
                    let dv = differentiate_raw(&v, var);
                    let term = pow(u.clone(), v.clone());
                    let part1 = mul(dv, call(Function::Ln, u.clone()));
                    let part2 = div(mul(v, du), u);
                    mul(term, add(part1, part2))
                }
            }
        },
        Expr::Call { func, args } => {
            if args.is_empty() {
                return int(0);
            }
            let u = args[0].clone();
            let du = differentiate_raw(&u, var);
            match func {
                Function::Sin => mul(call(Function::Cos, u), du),
                Function::Cos => mul(neg(call(Function::Sin, u)), du),
                Function::Tan => {
                    let tan_sq = pow(call(Function::Tan, u), int(2));
                    mul(add(int(1), tan_sq), du)
                }
                Function::Exp => mul(call(Function::Exp, u), du),
                Function::Ln => div(du, u),
                Function::Sqrt => div(du, mul(int(2), call(Function::Sqrt, u))),
                Function::Abs => mul(div(u.clone(), call(Function::Abs, u)), du),
            }
        }
    }
}

/// Differentiate an expression symbolically with respect to `var`, producing
/// a canonicalised AST.
pub fn differentiate(expr: &Expr, var: &str) -> Expr {
    let raw = differentiate_raw(expr, var);
    canonicalize(&raw)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evaluator::eval_at;
    use crate::parser::parse;
    use std::collections::HashMap;

    fn p(s: &str) -> Expr {
        parse(s).unwrap_or_else(|e| panic!("failed to parse '{s}': {e:?}"))
    }

    fn c(s: &str) -> Expr {
        canonicalize(&p(s))
    }

    fn check_deriv_at(expr_str: &str, var: &str, point: f64, expected: f64) {
        let ast = p(expr_str);
        let d = differentiate(&ast, var);
        let mut bindings = HashMap::new();
        bindings.insert(var.to_string(), point);
        let val = eval_at(&d, &bindings)
            .unwrap_or_else(|e| panic!("eval of d/d{var}({expr_str}) at {point} failed: {e:?}"));
        let tol = 1e-10;
        let diff = (val - expected).abs();
        let scale = expected.abs().max(1.0);
        assert!(
            diff <= tol * scale,
            "d/d{var}({expr_str}) at {point}: got {val}, expected {expected}, diff {diff}"
        );
    }

    #[test]
    fn test_constants_and_variables() {
        assert_eq!(differentiate(&p("42"), "x"), c("0"));
        assert_eq!(differentiate(&p("3.14"), "x"), c("0"));
        assert_eq!(differentiate(&p("pi"), "x"), c("0"));
        assert_eq!(differentiate(&p("e"), "x"), c("0"));
        assert_eq!(differentiate(&p("x"), "x"), c("1"));
        assert_eq!(differentiate(&p("y"), "x"), c("0"));
    }

    #[test]
    fn test_arithmetic_rules() {
        // Linearity
        assert_eq!(differentiate(&p("x + 5"), "x"), c("1"));
        assert_eq!(differentiate(&p("5 - x"), "x"), c("-1"));
        assert_eq!(differentiate(&p("-x"), "x"), c("-1"));
        assert_eq!(differentiate(&p("+x"), "x"), c("1"));
        assert_eq!(differentiate(&p("3 * x"), "x"), c("3"));
        assert_eq!(differentiate(&p("x * y"), "x"), c("y"));

        // Product rule: (x * x)' = x + x
        assert_eq!(differentiate(&p("x * x"), "x"), c("x + x"));
        check_deriv_at("x * x", "x", 3.0, 6.0);

        // Quotient rule: (1 / x)' at x=2 is -0.25
        check_deriv_at("1 / x", "x", 2.0, -0.25);
        check_deriv_at("(x - 1) / (x + 1)", "x", 3.0, 2.0 / 16.0);
    }

    #[test]
    fn test_power_rules() {
        // Polynomial power rule
        assert_eq!(differentiate(&p("x^2"), "x"), c("2 * x"));
        assert_eq!(differentiate(&p("x^3"), "x"), c("3 * x^2"));
        assert_eq!(differentiate(&p("x^1"), "x"), c("1"));
        assert_eq!(differentiate(&p("x^0"), "x"), c("0"));

        // Exponential power rule: 2^x -> 2^x * ln(2)
        check_deriv_at("2^x", "x", 3.0, 8.0 * (2.0f64).ln());

        // General power rule: x^x -> x^x * (ln(x) + 1)
        check_deriv_at("x^x", "x", 2.0, 4.0 * ((2.0f64).ln() + 1.0));
    }

    #[test]
    fn test_trigonometric() {
        // sin'(x) = cos(x)
        assert_eq!(differentiate(&p("sin(x)"), "x"), c("cos(x)"));
        // cos'(x) = -sin(x)
        assert_eq!(differentiate(&p("cos(x)"), "x"), c("-sin(x)"));
        // tan'(x) = 1 + tan(x)^2
        check_deriv_at("tan(x)", "x", 0.5, 1.0 + (0.5f64).tan().powi(2));
    }

    #[test]
    fn test_transcendental_and_special() {
        // exp'(x) = exp(x)
        assert_eq!(differentiate(&p("exp(x)"), "x"), c("exp(x)"));
        // ln'(x) = 1 / x
        assert_eq!(differentiate(&p("ln(x)"), "x"), c("1 / x"));
        // sqrt'(x) = 1 / (2 * sqrt(x))
        check_deriv_at("sqrt(x)", "x", 4.0, 0.25);
        // abs'(x) = x / abs(x)
        check_deriv_at("abs(x)", "x", 3.0, 1.0);
        check_deriv_at("abs(x)", "x", -3.0, -1.0);
    }

    #[test]
    fn test_chain_rule() {
        // sin(x^2)' = 2 * x * cos(x^2)
        check_deriv_at("sin(x^2)", "x", 1.5, 2.0 * 1.5 * (1.5f64.powi(2)).cos());
        // exp(3 * x)' = 3 * exp(3 * x)
        check_deriv_at("exp(3 * x)", "x", 0.5, 3.0 * (1.5f64).exp());
        // ln(x^2 + 1)' = 2 * x / (x^2 + 1)
        check_deriv_at("ln(x^2 + 1)", "x", 2.0, 4.0 / 5.0);
        // sqrt(x^2 + 1)' = x / sqrt(x^2 + 1)
        check_deriv_at("sqrt(x^2 + 1)", "x", 3.0, 3.0 / (10.0f64).sqrt());
    }

    #[test]
    fn test_unrelated_variable() {
        // d/dy(sin(x) + x^3) = 0
        assert_eq!(differentiate(&p("sin(x) + x^3"), "y"), c("0"));
    }
}
