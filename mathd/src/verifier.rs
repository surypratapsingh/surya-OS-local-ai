//! B5 verifier: numerical verification of mathematical claims over independent domains.
//! Work order B5 (`docs/work-orders.md`, "B5 · The verifier").
//!
//! Signatures:
//! - `verify_integral(integrand, claimed_antiderivative, var) -> Verdict`
//! - `verify_equation_solution(equation, claimed_roots, var)  -> Verdict`
//! - `verify_identity(lhs, rhs, vars)                          -> Verdict`
//! - `verify_factorisation(original, claimed_factors)          -> Verdict`
//!
//! Verdict carries:
//! - `Verified { evidence: Evidence }`
//! - `Failed { counterexample: Point, expected: f64, got: f64 }`
//! - `Unverifiable { reason: String }`

use std::collections::{BTreeSet, HashMap};

use crate::differentiator::differentiate;
use crate::evaluator::eval_at;
use crate::parser::{BinOp, Expr, Function, UnaryOp};

pub type Ast = Expr;
pub type Point = HashMap<String, f64>;

/// Minimum number of domain-valid test points required to confirm a claim.
pub const MIN_TEST_POINTS: usize = 8;

/// Default target sample points attempted across domains.
pub const TARGET_SAMPLE_POINTS: usize = 16;

/// Relative and absolute numerical tolerance for verification.
pub const VERIFIER_TOLERANCE: f64 = 1e-9;

/// Evidence recorded on successful verification.
#[derive(Debug, Clone, PartialEq)]
pub struct Evidence {
    pub points_tested: usize,
    pub max_relative_error: f64,
}

/// The three first-class verification outcomes specified by work order B5.
#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    /// The claim was confirmed numerically at >= 8 points across the domain.
    Verified { evidence: Evidence },
    /// The claim failed at a specific counterexample point.
    Failed {
        counterexample: Point,
        expected: f64,
        got: f64,
    },
    /// The claim could not be verified with confidence (e.g. empty domain, unsupported form).
    Unverifiable { reason: String },
}

impl Verdict {
    pub fn is_verified(&self) -> bool {
        matches!(self, Verdict::Verified { .. })
    }

    pub fn is_failed(&self) -> bool {
        matches!(self, Verdict::Failed { .. })
    }

    pub fn is_unverifiable(&self) -> bool {
        matches!(self, Verdict::Unverifiable { .. })
    }
}

/// Representation of an equation LHS = RHS.
#[derive(Debug, Clone, PartialEq)]
pub struct Equation {
    pub lhs: Ast,
    pub rhs: Ast,
}

impl Equation {
    pub fn new(lhs: Ast, rhs: Ast) -> Self {
        Self { lhs, rhs }
    }

    pub fn equals_zero(lhs: Ast) -> Self {
        Self {
            lhs,
            rhs: Expr::Integer(0),
        }
    }
}

impl From<Ast> for Equation {
    fn from(expr: Ast) -> Self {
        Self::equals_zero(expr)
    }
}

// ---------------------------------------------------------------- Variable extraction

/// Collect all variable names referenced in an expression AST into a sorted set.
pub fn extract_variables(expr: &Ast, vars: &mut BTreeSet<String>) {
    match expr {
        Expr::Integer(_) | Expr::Decimal(_) | Expr::Constant(_) => {}
        Expr::Variable(name) => {
            vars.insert(name.clone());
        }
        Expr::UnaryOp { operand, .. } => {
            extract_variables(operand, vars);
        }
        Expr::BinOp { left, right, .. } => {
            extract_variables(left, vars);
            extract_variables(right, vars);
        }
        Expr::Call { args, .. } => {
            for arg in args {
                extract_variables(arg, vars);
            }
        }
    }
}

// ---------------------------------------------------------------- PRNG for deterministic sampling

/// Minimal std-only XorShift64 PRNG.
#[derive(Debug, Clone)]
pub struct VerifierRng {
    state: u64,
}

impl VerifierRng {
    pub fn new(seed: u64) -> Self {
        Self {
            state: if seed == 0 {
                0x517c_c1b7_2722_0a95
            } else {
                seed
            },
        }
    }

    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.state = x;
        x
    }

    pub fn next_f64(&mut self, min: f64, max: f64) -> f64 {
        let t = (self.next_u64() >> 11) as f64 / ((1u64 << 53) as f64);
        min + t * (max - min)
    }
}

// ---------------------------------------------------------------- Core verification engine

/// Verify that two expressions `lhs` and `rhs` evaluate to identical values
/// across at least `MIN_TEST_POINTS` random points drawn from sensible domains.
pub fn verify_equivalence(lhs: &Ast, rhs: &Ast, vars: &[&str]) -> Verdict {
    let mut rng = VerifierRng::new(0x9172_6305_4b5e_1101);
    let mut points_tested = 0;
    let mut max_relative_error: f64 = 0.0;

    // Sensible domain regions:
    // 1. Standard positive [0.2, 4.0] (safe for ln, sqrt, non-integer powers)
    // 2. Small fractions [0.05, 0.95]
    // 3. Moderate positive [1.0, 8.0]
    // 4. Negative domain [-4.0, -0.2] (for expressions without ln/sqrt)
    // 5. General real [-3.0, 3.0]
    let regions: [(f64, f64); 5] = [
        (0.2, 4.0),
        (0.05, 0.95),
        (1.0, 8.0),
        (-4.0, -0.2),
        (-3.0, 3.0),
    ];

    // If there are no variables, verify as constant expressions
    if vars.is_empty() {
        let empty_point = HashMap::new();
        match (eval_at(lhs, &empty_point), eval_at(rhs, &empty_point)) {
            (Ok(g), Ok(e)) => {
                if !g.is_finite() || !e.is_finite() {
                    return Verdict::Unverifiable {
                        reason: "constant expressions evaluated to non-finite value".to_string(),
                    };
                }
                let diff = (g - e).abs();
                let scale = e.abs().max(1.0);
                if diff > VERIFIER_TOLERANCE * scale {
                    return Verdict::Failed {
                        counterexample: empty_point,
                        expected: e,
                        got: g,
                    };
                }
                return Verdict::Verified {
                    evidence: Evidence {
                        // For claims with no free variables, a single evaluation of
                        // LHS and RHS is exhaustive (honest count per AGENTS.md rule 10; V7).
                        points_tested: 1,
                        max_relative_error: diff / scale,
                    },
                };
            }
            (Err(err), _) => {
                return Verdict::Unverifiable {
                    reason: format!("failed to evaluate LHS: {:?}", err),
                };
            }
            (_, Err(err)) => {
                return Verdict::Unverifiable {
                    reason: format!("failed to evaluate RHS: {:?}", err),
                };
            }
        }
    }

    let mut candidate_pool: Vec<Point> = Vec::new();

    // 1. Generate diverse random points round-robin across sensible regions.
    // Round-robin ensures negative and mixed domains are sampled immediately
    // alongside positive ones (fixing finding V1).
    for _ in 0..15 {
        for &(min, max) in &regions {
            let mut pt = HashMap::new();
            for &var in vars {
                pt.insert(var.to_string(), rng.next_f64(min, max));
            }
            candidate_pool.push(pt);
        }
    }

    // 2. Add fixed deterministic points spanning positive and negative domains
    let fixed_values = [
        -2.5,
        -1.414,
        -0.73,
        -0.37,
        0.37,
        0.73,
        1.414,
        std::f64::consts::E,
        std::f64::consts::PI,
        2.5,
    ];
    for (i, &val) in fixed_values.iter().enumerate() {
        let mut pt = HashMap::new();
        for (j, &var) in vars.iter().enumerate() {
            let offset = ((i + j) % fixed_values.len()) as f64 * 0.1;
            pt.insert(var.to_string(), val + offset);
        }
        candidate_pool.push(pt);
    }

    // Evaluate candidates
    for pt in candidate_pool {
        let eval_l = eval_at(lhs, &pt);
        let eval_r = eval_at(rhs, &pt);

        match (eval_l, eval_r) {
            (Ok(g), Ok(e)) => {
                // If either value is non-finite, skip point (outside domain)
                if !g.is_finite() || !e.is_finite() {
                    continue;
                }

                let diff = (g - e).abs();
                let scale = e.abs().max(1.0);
                let rel_err = diff / scale;

                if diff > VERIFIER_TOLERANCE * scale {
                    return Verdict::Failed {
                        counterexample: pt,
                        expected: e,
                        got: g,
                    };
                }

                points_tested += 1;
                max_relative_error = max_relative_error.max(rel_err);

                if points_tested >= TARGET_SAMPLE_POINTS {
                    break;
                }
            }
            (Err(_), _) | (_, Err(_)) => {
                // Point is outside the common domain of the expressions; skip it.
                continue;
            }
        }
    }

    if points_tested >= MIN_TEST_POINTS {
        Verdict::Verified {
            evidence: Evidence {
                points_tested,
                max_relative_error,
            },
        }
    } else {
        Verdict::Unverifiable {
            reason: format!(
                "insufficient domain points tested (tested {} valid points, minimum is {})",
                points_tested, MIN_TEST_POINTS
            ),
        }
    }
}

// ---------------------------------------------------------------- The 4 verifiers

/// Verify that `claimed_antiderivative` is an antiderivative of `integrand` with respect to `var`.
///
/// Method: differentiate `claimed_antiderivative` with respect to `var`, then confirm
/// that the derivative numerically equals `integrand` at at least 8 random points in domain.
pub fn verify_integral(integrand: &Ast, claimed_antiderivative: &Ast, var: &str) -> Verdict {
    let deriv = differentiate(claimed_antiderivative, var);

    let mut vars_set = BTreeSet::new();
    extract_variables(integrand, &mut vars_set);
    extract_variables(&deriv, &mut vars_set);
    vars_set.insert(var.to_string());

    let vars_vec: Vec<&str> = vars_set.iter().map(|s| s.as_str()).collect();
    // LHS: derivative of claimed antiderivative, RHS: integrand
    verify_equivalence(&deriv, integrand, &vars_vec)
}

/// Verify that `lhs` and `rhs` are algebraically equivalent across all named `vars`.
pub fn verify_identity(lhs: &Ast, rhs: &Ast, vars: &[&str]) -> Verdict {
    let mut vars_vec: Vec<&str> = vars.to_vec();
    let mut vars_set = BTreeSet::new();
    if vars_vec.is_empty() {
        extract_variables(lhs, &mut vars_set);
        extract_variables(rhs, &mut vars_set);
        vars_vec = vars_set.iter().map(|s| s.as_str()).collect();
    }
    verify_equivalence(lhs, rhs, &vars_vec)
}

/// Verify that the product of `claimed_factors` identically equals `original`.
pub fn verify_factorisation(original: &Ast, claimed_factors: &[Ast]) -> Verdict {
    if claimed_factors.is_empty() {
        return Verdict::Unverifiable {
            reason: "claimed factors list is empty".to_string(),
        };
    }

    let mut factor_product = claimed_factors[0].clone();
    for factor in &claimed_factors[1..] {
        factor_product = Expr::BinOp {
            op: BinOp::Mul,
            left: Box::new(factor_product),
            right: Box::new(factor.clone()),
        };
    }

    let mut vars_set = BTreeSet::new();
    extract_variables(original, &mut vars_set);
    extract_variables(&factor_product, &mut vars_set);
    let vars_vec: Vec<&str> = vars_set.iter().map(|s| s.as_str()).collect();

    verify_equivalence(original, &factor_product, &vars_vec)
}

/// Verify that every root in `claimed_roots` is a solution of `equation` (LHS = RHS).
pub fn verify_equation_solution(equation: &Equation, claimed_roots: &[f64], var: &str) -> Verdict {
    if claimed_roots.is_empty() {
        return Verdict::Unverifiable {
            reason: "cannot verify completeness of empty root set numerically".to_string(),
        };
    }

    let mut other_vars = BTreeSet::new();
    extract_variables(&equation.lhs, &mut other_vars);
    extract_variables(&equation.rhs, &mut other_vars);
    other_vars.remove(var);

    let other_vars_vec: Vec<&str> = other_vars.iter().map(|s| s.as_str()).collect();
    let mut rng = VerifierRng::new(0x3847_1129_5a2b_c81f);
    let mut total_points_tested = 0;
    let mut max_relative_error: f64 = 0.0;

    for &root in claimed_roots {
        if !root.is_finite() {
            let mut pt = HashMap::new();
            pt.insert(var.to_string(), root);
            return Verdict::Failed {
                counterexample: pt,
                expected: 0.0,
                got: root,
            };
        }

        if other_vars_vec.is_empty() {
            let mut pt = HashMap::new();
            pt.insert(var.to_string(), root);

            match (eval_at(&equation.lhs, &pt), eval_at(&equation.rhs, &pt)) {
                (Ok(lhs_val), Ok(rhs_val)) => {
                    if !lhs_val.is_finite() || !rhs_val.is_finite() {
                        return Verdict::Failed {
                            counterexample: pt,
                            expected: rhs_val,
                            got: lhs_val,
                        };
                    }

                    let diff = (lhs_val - rhs_val).abs();
                    let scale = rhs_val.abs().max(1.0);
                    let rel_err = diff / scale;

                    if diff > VERIFIER_TOLERANCE * scale {
                        return Verdict::Failed {
                            counterexample: pt,
                            expected: rhs_val,
                            got: lhs_val,
                        };
                    }

                    total_points_tested += 1;
                    max_relative_error = max_relative_error.max(rel_err);
                }
                (Err(_), _) | (_, Err(_)) => {
                    return Verdict::Failed {
                        counterexample: pt,
                        expected: 0.0,
                        got: f64::NAN,
                    };
                }
            }
        } else {
            // Test parameter variations with var = root
            // Sample across diverse regions including negative parameter domains
            let param_regions: [(f64, f64); 4] =
                [(0.5, 4.0), (-4.0, -0.5), (0.1, 1.0), (-2.0, 2.0)];
            let mut root_points_tested = 0;
            for round in 0..20 {
                let (p_min, p_max) = param_regions[round % param_regions.len()];
                let mut pt = HashMap::new();
                pt.insert(var.to_string(), root);
                for &p_var in &other_vars_vec {
                    pt.insert(p_var.to_string(), rng.next_f64(p_min, p_max));
                }

                match (eval_at(&equation.lhs, &pt), eval_at(&equation.rhs, &pt)) {
                    (Ok(lhs_val), Ok(rhs_val)) => {
                        if !lhs_val.is_finite() || !rhs_val.is_finite() {
                            continue;
                        }

                        let diff = (lhs_val - rhs_val).abs();
                        let scale = rhs_val.abs().max(1.0);
                        let rel_err = diff / scale;

                        if diff > VERIFIER_TOLERANCE * scale {
                            return Verdict::Failed {
                                counterexample: pt,
                                expected: rhs_val,
                                got: lhs_val,
                            };
                        }

                        root_points_tested += 1;
                        max_relative_error = max_relative_error.max(rel_err);
                        if root_points_tested >= MIN_TEST_POINTS {
                            break;
                        }
                    }
                    _ => continue,
                }
            }

            if root_points_tested < MIN_TEST_POINTS {
                return Verdict::Unverifiable {
                    reason: format!(
                        "insufficient parameter points tested for root {} (tested {})",
                        root, root_points_tested
                    ),
                };
            }
            total_points_tested += root_points_tested;
        }
    }

    Verdict::Verified {
        evidence: Evidence {
            points_tested: total_points_tested,
            max_relative_error,
        },
    }
}

// ---------------------------------------------------------------- Mutation operators for verification testing

/// Mutation operator applied to an expression.
#[derive(Debug, Clone, PartialEq)]
pub enum MutationKind {
    FlipSign,
    ChangeCoefficient,
    SwapNonCommutative,
    DropTerm,
    SubstituteSiblingFunction,
}

/// Generate non-trivial mutations of an expression.
pub fn generate_expression_mutations(expr: &Ast) -> Vec<(Ast, MutationKind)> {
    let mut mutants = Vec::new();

    match expr {
        Expr::Integer(n) => {
            // Change coefficient by 1
            mutants.push((Expr::Integer(n + 1), MutationKind::ChangeCoefficient));
            mutants.push((Expr::Integer(n - 1), MutationKind::ChangeCoefficient));
            // Flip sign if non-zero
            if *n != 0 {
                mutants.push((Expr::Integer(-n), MutationKind::FlipSign));
            }
        }
        Expr::Decimal(f) => {
            mutants.push((Expr::Decimal(f + 1.0), MutationKind::ChangeCoefficient));
            if *f >= 1.0 {
                mutants.push((Expr::Decimal(f - 1.0), MutationKind::ChangeCoefficient));
            }
        }
        Expr::Constant(_) | Expr::Variable(_) => {}
        Expr::UnaryOp { op, operand } => {
            match op {
                UnaryOp::Neg => {
                    // Flip sign: remove negation
                    mutants.push(((**operand).clone(), MutationKind::FlipSign));
                }
                UnaryOp::Pos => {
                    // Flip sign: turn positive into negative
                    mutants.push((
                        Expr::UnaryOp {
                            op: UnaryOp::Neg,
                            operand: operand.clone(),
                        },
                        MutationKind::FlipSign,
                    ));
                }
            }
            for (sub_m, kind) in generate_expression_mutations(operand) {
                mutants.push((
                    Expr::UnaryOp {
                        op: *op,
                        operand: Box::new(sub_m),
                    },
                    kind,
                ));
            }
        }
        Expr::BinOp { op, left, right } => {
            match op {
                BinOp::Add => {
                    // Flip sign: + -> -
                    mutants.push((
                        Expr::BinOp {
                            op: BinOp::Sub,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        MutationKind::FlipSign,
                    ));
                    // Drop term: keep left only, keep right only
                    mutants.push(((**left).clone(), MutationKind::DropTerm));
                    mutants.push(((**right).clone(), MutationKind::DropTerm));
                }
                BinOp::Sub => {
                    // Flip sign: - -> +
                    mutants.push((
                        Expr::BinOp {
                            op: BinOp::Add,
                            left: left.clone(),
                            right: right.clone(),
                        },
                        MutationKind::FlipSign,
                    ));
                    // Swap non-commutative: a - b -> b - a
                    mutants.push((
                        Expr::BinOp {
                            op: BinOp::Sub,
                            left: right.clone(),
                            right: left.clone(),
                        },
                        MutationKind::SwapNonCommutative,
                    ));
                    // Drop term: keep left
                    mutants.push(((**left).clone(), MutationKind::DropTerm));
                }
                BinOp::Mul => {
                    // Drop term in product
                    mutants.push(((**left).clone(), MutationKind::DropTerm));
                    mutants.push(((**right).clone(), MutationKind::DropTerm));
                }
                BinOp::Div => {
                    // Swap non-commutative: a / b -> b / a
                    mutants.push((
                        Expr::BinOp {
                            op: BinOp::Div,
                            left: right.clone(),
                            right: left.clone(),
                        },
                        MutationKind::SwapNonCommutative,
                    ));
                    mutants.push(((**left).clone(), MutationKind::DropTerm));
                }
                BinOp::Pow => {
                    // Swap non-commutative: a ^ b -> b ^ a
                    mutants.push((
                        Expr::BinOp {
                            op: BinOp::Pow,
                            left: right.clone(),
                            right: left.clone(),
                        },
                        MutationKind::SwapNonCommutative,
                    ));
                    mutants.push(((**left).clone(), MutationKind::DropTerm));
                }
            }

            for (sub_m, kind) in generate_expression_mutations(left) {
                mutants.push((
                    Expr::BinOp {
                        op: *op,
                        left: Box::new(sub_m),
                        right: right.clone(),
                    },
                    kind,
                ));
            }

            for (sub_m, kind) in generate_expression_mutations(right) {
                mutants.push((
                    Expr::BinOp {
                        op: *op,
                        left: left.clone(),
                        right: Box::new(sub_m),
                    },
                    kind,
                ));
            }
        }
        Expr::Call { func, args } => {
            // Sibling function substitution
            let sibling = match func {
                Function::Sin => Some(Function::Cos),
                Function::Cos => Some(Function::Sin),
                Function::Tan => Some(Function::Sin),
                Function::Exp => Some(Function::Ln),
                Function::Ln => Some(Function::Exp),
                Function::Sqrt => Some(Function::Abs),
                Function::Abs => Some(Function::Sqrt),
            };

            if let Some(sib) = sibling {
                mutants.push((
                    Expr::Call {
                        func: sib,
                        args: args.clone(),
                    },
                    MutationKind::SubstituteSiblingFunction,
                ));
            }

            for (i, arg) in args.iter().enumerate() {
                for (sub_m, kind) in generate_expression_mutations(arg) {
                    let mut new_args = args.clone();
                    new_args[i] = sub_m;
                    mutants.push((
                        Expr::Call {
                            func: *func,
                            args: new_args,
                        },
                        kind,
                    ));
                }
            }
        }
    }

    mutants
}
