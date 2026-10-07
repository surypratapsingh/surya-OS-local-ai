//! B2 property gates: agreement and separation over 10,000 cases each
//! (`docs/work-orders.md`, "B2 · Canonicaliser and structural hash").
//!
//! - **Agreement** — pairs that are equivalent *by construction* (commuted
//!   operands, reassociated terms, `x+0`, `x*1`, double negation, `a/b` vs
//!   `a*b^-1`, `(n+c)-c`, `(n/c)*c`) must hash identically. Zero exceptions
//!   permitted.
//! - **Separation** — pairs drawn to be *canonically* distinct must hash
//!   differently. The hash is specified over the canonical form, so two
//!   spellings that canonicalise to the same form are one object, not a
//!   collision (measured: e.g. `1 * pi` and `pi ^ 1` both merge to `pi`).
//!   Such spellings are redrawn and their count reported. Real collisions —
//!   distinct canonical forms with equal hashes — are counted; more than 1
//!   in 10,000 means the hash is wrong, and the test fails per the work
//!   order.
//!
//! Independence: the generator and PRNG here are unrelated to the parser's
//! tests; equivalence is constructed from rewriting laws applied to random
//! base expressions, never by calling the canonicaliser itself.

use mathd::{canonicalize, parse, print, structural_hash, BinOp, Const, Expr, Function, UnaryOp};

struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 13;
        x ^= x << 7;
        x ^= x >> 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }
}

const VARS: [&str; 5] = ["a", "b", "c", "m", "q"];
const FUNCS: [Function; 5] = [
    Function::Sin,
    Function::Cos,
    Function::Exp,
    Function::Ln,
    Function::Sqrt,
];

fn leaf(rng: &mut Rng) -> Expr {
    match rng.below(4) {
        0 => Expr::Integer(rng.below(12) as i64 + 1), // 1..=12, never 0
        1 => Expr::Decimal((rng.below(40) as f64 + 1.0) / 8.0),
        2 => Expr::Constant(if rng.below(2) == 0 {
            Const::Pi
        } else {
            Const::E
        }),
        _ => Expr::Variable(VARS[rng.below(VARS.len() as u64) as usize].to_string()),
    }
}

/// Random expression over + - * / ^ with unary minus, depth-limited.
/// Integer literals are kept small so folds stay in range and equivalence
/// pairs are not swamped by overflow-preserved (unfolded) forms.
fn base_expr(rng: &mut Rng, depth: u32) -> Expr {
    let r = rng.below(100);
    if depth == 0 || r < 35 {
        return leaf(rng);
    }
    match r {
        35..=54 => {
            let op = [BinOp::Add, BinOp::Sub, BinOp::Mul, BinOp::Div][rng.below(4) as usize];
            Expr::BinOp {
                op,
                left: Box::new(base_expr(rng, depth - 1)),
                right: Box::new(base_expr(rng, depth - 1)),
            }
        }
        55..=64 => Expr::BinOp {
            op: BinOp::Pow,
            left: Box::new(leaf(rng)),
            right: Box::new(Expr::Integer(rng.below(4) as i64 + 1)), // 1..=4
        },
        65..=79 => Expr::Call {
            func: FUNCS[rng.below(FUNCS.len() as u64) as usize],
            args: vec![base_expr(rng, depth - 1)],
        },
        _ => Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(base_expr(rng, depth - 1)),
        },
    }
}

fn to_text(expr: &Expr) -> String {
    print(expr)
}

fn hash_of(expr: &Expr) -> u64 {
    structural_hash(expr)
}

// ---------------------------------------------------------------------------
// equivalence constructors: each returns (variant_a, variant_b) of the same
// mathematical object, built by rewriting, never by canonicalising.
// ---------------------------------------------------------------------------

/// Commute a binary op's operands; for Sub/Div the right operand is moved
/// under a negation/reciprocal so the VALUE is preserved, not the spelling:
/// a - b  ->  (-b) + a is a different law; here we only use truly
/// commutative rewrites for Sub/Div (handled by the dedicated constructors).
fn commuted(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    let op = [BinOp::Add, BinOp::Mul][rng.below(2) as usize];
    let l = base_expr(rng, depth);
    let r = base_expr(rng, depth);
    (
        Expr::BinOp {
            op,
            left: Box::new(l.clone()),
            right: Box::new(r.clone()),
        },
        Expr::BinOp {
            op,
            left: Box::new(r),
            right: Box::new(l),
        },
    )
}

/// Reassociate: (x + y) + z  <->  x + (y + z), likewise for Mul.
fn reassociated(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    let op = [BinOp::Add, BinOp::Mul][rng.below(2) as usize];
    let x = base_expr(rng, depth);
    let y = base_expr(rng, depth);
    let z = base_expr(rng, depth);
    (
        Expr::BinOp {
            op,
            left: Box::new(Expr::BinOp {
                op,
                left: Box::new(x.clone()),
                right: Box::new(y.clone()),
            }),
            right: Box::new(z.clone()),
        },
        Expr::BinOp {
            op,
            left: Box::new(x),
            right: Box::new(Expr::BinOp {
                op,
                left: Box::new(y),
                right: Box::new(z),
            }),
        },
    )
}

/// Identity injection: x + 0, 0 + x, x * 1, 1 * x.
fn identity_joined(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    let x = base_expr(rng, depth);
    match rng.below(4) {
        0 => (
            x.clone(),
            Expr::BinOp {
                op: BinOp::Add,
                left: Box::new(x),
                right: Box::new(Expr::Integer(0)),
            },
        ),
        1 => (
            Expr::BinOp {
                op: BinOp::Add,
                left: Box::new(Expr::Integer(0)),
                right: Box::new(x.clone()),
            },
            x,
        ),
        2 => (
            x.clone(),
            Expr::BinOp {
                op: BinOp::Mul,
                left: Box::new(x),
                right: Box::new(Expr::Integer(1)),
            },
        ),
        _ => (
            Expr::BinOp {
                op: BinOp::Mul,
                left: Box::new(Expr::Integer(1)),
                right: Box::new(x.clone()),
            },
            x,
        ),
    }
}

/// Double negation: x  <->  --x; also +x spellings.
fn double_negated(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    let x = base_expr(rng, depth);
    let inner = Expr::UnaryOp {
        op: UnaryOp::Neg,
        operand: Box::new(x.clone()),
    };
    (
        x,
        Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(inner),
        },
    )
}

/// Negated sum vs distributed signs: -(x + y)  <->  -x - y (and Sub forms).
fn negated_sum_distributed(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    let x = base_expr(rng, depth);
    let y = base_expr(rng, depth);
    let sum = Expr::BinOp {
        op: BinOp::Add,
        left: Box::new(x.clone()),
        right: Box::new(y.clone()),
    };
    let distributed = Expr::BinOp {
        op: BinOp::Sub,
        left: Box::new(Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(x),
        }),
        right: Box::new(y),
    };
    (
        Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(sum),
        },
        distributed,
    )
}

/// Reciprocal spelling: a / b  <->  a * b^-1.
fn reciprocal_spelling(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    let a = base_expr(rng, depth);
    let b = leaf(rng); // a leaf keeps the denominator simple
    (
        Expr::BinOp {
            op: BinOp::Div,
            left: Box::new(a.clone()),
            right: Box::new(b.clone()),
        },
        Expr::BinOp {
            op: BinOp::Mul,
            left: Box::new(a),
            right: Box::new(Expr::BinOp {
                op: BinOp::Pow,
                left: Box::new(b),
                right: Box::new(Expr::Integer(-1)),
            }),
        },
    )
}

/// Foldable constant: (n + c) - c  <->  n, and (n * c) / c  <->  n, with
/// small c and n chosen so every fold stays far inside i64.
fn foldable_constant(rng: &mut Rng) -> (Expr, Expr) {
    let n = rng.below(50) as i64 + 1;
    let c = rng.below(9) as i64 + 1;
    let var = Expr::Variable(VARS[rng.below(VARS.len() as u64) as usize].to_string());
    match rng.below(2) {
        0 => (
            Expr::Integer(n),
            Expr::BinOp {
                op: BinOp::Sub,
                left: Box::new(Expr::BinOp {
                    op: BinOp::Add,
                    left: Box::new(Expr::Integer(n)),
                    right: Box::new(Expr::Integer(c)),
                }),
                right: Box::new(Expr::Integer(c)),
            },
        ),
        _ => (
            var.clone(),
            Expr::BinOp {
                op: BinOp::Div,
                left: Box::new(Expr::BinOp {
                    op: BinOp::Mul,
                    left: Box::new(var),
                    right: Box::new(Expr::Integer(c)),
                }),
                right: Box::new(Expr::Integer(c)),
            },
        ),
    }
}

fn equivalence_pair(rng: &mut Rng, depth: u32) -> (Expr, Expr) {
    match rng.below(7) {
        0 => commuted(rng, depth),
        1 => reassociated(rng, depth),
        2 => identity_joined(rng, depth),
        3 => double_negated(rng, depth),
        4 => negated_sum_distributed(rng, depth),
        5 => reciprocal_spelling(rng, depth),
        _ => foldable_constant(rng),
    }
}

/// A structurally-distinct expression: pair it with a *different* shape so
/// the two are not equal by construction. Two independent draws are unequal
/// unless they collide as raw ASTs (checked and re-drawn below), so every
/// hash collision the test reports is a genuine canonicaliser/hash failure,
/// not an artifact of drawing the same tree twice.
/// Two canonically-distinct expressions. `merged` counts spellings the
/// canonicaliser folded to an already-drawn form and caused a redraw —
/// reported so the merging behaviour stays visible, never hidden.
fn distinct_pair(rng: &mut Rng, depth: u32, merged: &mut usize) -> (Expr, Expr) {
    loop {
        let a = base_expr(rng, depth);
        let b = base_expr(rng, depth);
        if canonicalize(&a) != canonicalize(&b) {
            return (a, b);
        }
        *merged += 1;
    }
}

#[test]
fn agreement_equivalent_by_construction_hash_identically() {
    const CASES: usize = 10_000;
    let mut rng = Rng(0xC0DE_BABE_0000_0002);
    for i in 0..CASES {
        let depth = 1 + rng.below(3) as u32;
        let (a, b) = equivalence_pair(&mut rng, depth);
        let (ha, hb) = (hash_of(&a), hash_of(&b));
        assert_eq!(
            ha,
            hb,
            "case {}: agreement failed\n  a = {}\n    = {}\n  b = {}\n    = {}",
            i,
            to_text(&a),
            to_text(&canonicalize(&a)),
            to_text(&b),
            to_text(&canonicalize(&b)),
        );
    }
}

#[test]
fn separation_structurally_different_exprs_collide_rarely() {
    const CASES: usize = 10_000;
    const MAX_COLLISIONS: usize = 1; // spec: > 1 in 10,000 means wrong
    let mut rng = Rng(0x5EED_5EED_0000_0003);
    let mut collisions = 0usize;
    let mut merged = 0usize;
    for i in 0..CASES {
        let depth = 1 + rng.below(4) as u32;
        let (a, b) = distinct_pair(&mut rng, depth, &mut merged);
        if hash_of(&a) == hash_of(&b) {
            collisions += 1;
            eprintln!(
                "collision at case {}: {} vs {}",
                i,
                to_text(&a),
                to_text(&b)
            );
        }
    }
    eprintln!(
        "separation: {} collisions in {} canonically-distinct pairs \
         ({} spellings merged by the canonicaliser and were redrawn)",
        collisions, CASES, merged
    );
    assert!(
        collisions <= MAX_COLLISIONS,
        "separation gate failed: {} collisions in {} cases (max {})",
        collisions,
        CASES,
        MAX_COLLISIONS
    );
}

#[test]
fn hashes_are_deterministic_across_runs() {
    let mut rng = Rng(0xDEAD_BEEF_0000_0004);
    for _ in 0..100 {
        let e = base_expr(&mut rng, 3);
        let h1 = hash_of(&e);
        let h2 = hash_of(&e);
        assert_eq!(h1, h2);
        // Re-parse -> canonicalise -> hash must also agree: the hash is a
        // function of the expression, not of this particular AST allocation.
        let text = to_text(&canonicalize(&e));
        let reparsed = parse(&text).unwrap_or_else(|err| panic!("{:?}: {}", text, err));
        assert_eq!(structural_hash(&reparsed), h1, "via {:?}", text);
    }
}
