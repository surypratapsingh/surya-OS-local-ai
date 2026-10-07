//! Canonicaliser and structural hash. Work order B2
//! (`docs/work-orders.md`, "B2 · Canonicaliser and structural hash").
//!
//! `canonicalize` rewrites an AST into a normal form; `structural_hash`
//! hashes that normal form to a u64. Two expressions the canonicaliser
//! deems equivalent therefore hash identically, which is what the card
//! store (B8) will be keyed on.
//!
//! ## The normal form, rule by rule
//!
//! Each rule below is implemented in `canon` and pinned by a named unit
//! test. They are listed explicitly so nobody has to reverse-engineer them.
//!
//! - **R1 Unary plus disappears.** `+x` → `x`.
//! - **R2 Negation folds into integers** when the result is representable:
//!   `-(3)` → `-3`. `-(i64::MIN)` is preserved as a `Neg` node.
//! - **R3 Double negation cancels:** `--x` → `x`.
//! - **R4 Negation distributes over sums** (and only sums):
//!   `-(a+b)` → `-a - b`. It is never pushed into products, because
//!   `-(a*b)` has no cheaper form than `-(a * b)` without expansion.
//! - **R5 Sums flatten** into a list of signed terms; `Add` chains and
//!   `Sub` merge into one list, a `Neg` term becomes a negative sign.
//!   Integer terms fold with checked arithmetic (`2 + x + 3` → `5 + x`);
//!   a folded sum of `0` disappears (`x + 0` → `x`). On integer overflow
//!   the fold is skipped wholesale — the terms stay unfolded, correctly
//!   sorted, and nothing wraps silently.
//! - **R6 Products flatten** into numerator factors and denominator
//!   factors. `Div(a, b)` contributes `a` to the numerator and `b` to the
//!   denominator; a `Div(1, p)` factor contributes its `1` and `p`
//!   respectively. Integer factors fold (checked); `gcd` cancellation
//!   reduces the folded pair (`6/4` → `3/2`, `8/2` → `4`). A folded
//!   numerator of `0` becomes `Integer(0)` — including `0/0`. That is a
//!   documented decision: domain rejection of `0/0` is B3's job at
//!   evaluation time, and the canonicaliser does not guess.
//! - **R7 Trivial factors vanish:** `x*1` → `x`, `x/1` → `x`, `1` alone
//!   stays `1`.
//! - **R8 `a/b` and `a*b^-1` meet in one form: `Div(a, b)`.** Negative
//!   integer powers canonicalise to `Div(1, Pow(base, n))` (`x^-1` →
//!   `1 / x`), so a product absorbs them into its denominator.
//! - **R9 Integer powers fold when representable:** `2^10` → `1024`,
//!   `(-2)^3` → `-8`, `x^1` → `x`. `b^0` → `1` for **every** base — the
//!   standard convention (`0**0 == 1` in Python, SymPy likewise) — and
//!   `0^-n` → `1/0`, a well-defined normal form that evaluation (B3)
//!   rejects as a domain error. Preserved on overflow, and for exponents
//!   of `i64::MIN` (no positive counterpart).
//! - **R10 Commutative operands sort** into the total order of
//!   [`cmp_expr`]: sum terms by their (unsigned) term, product factors by
//!   the factor itself. Sort order is structural, deterministic, and
//!   independent of input order.
//!
//! There is deliberately **no symbolic cancellation** beyond the integer
//! `gcd` above: `x - x` stays `x - x`, `x/x` stays `x/x`. Deciding those
//! equivalence classes is real algebra (B3+ territory), and a
//! canonicaliser that guesses would corrupt the hash key the card store
//! relies on.
//!
//! ## The hash
//!
//! `hash_canonical` serialises an expression deterministically (variant
//! tag + payload, little-endian, see `serialize`) and runs FNV-1a 64-bit
//! over the bytes. `structural_hash` is the convenience entry point:
//! canonicalise, then hash. Both are pure functions of the expression.
//!
//! ## Independence note
//!
//! Nothing here reuses parser internals beyond the `Expr` type itself;
//! the property tests in `tests/canonical.rs` generate equivalence by
//! construction (commutation, reassociation, identity injection,
//! reciprocal rewriting) with a PRNG unrelated to the parser's, so the
//! agreement gate cannot pass by shared construction.

use std::cmp::Ordering;

use crate::parser::{BinOp, Expr, UnaryOp};

// ---------------------------------------------------------------------------
// canonical form
// ---------------------------------------------------------------------------

/// Rewrite `expr` into the canonical form described in the module header.
pub fn canonicalize(expr: &Expr) -> Expr {
    canon(expr)
}

fn canon(expr: &Expr) -> Expr {
    match expr {
        Expr::Integer(_) | Expr::Decimal(_) | Expr::Constant(_) | Expr::Variable(_) => expr.clone(),
        Expr::Call { func, args } => Expr::Call {
            func: *func,
            args: args.iter().map(canon).collect(),
        },
        Expr::UnaryOp { op, operand } => canon_unary(*op, &canon(operand)),
        Expr::BinOp { op, left, right } => {
            let l = canon(left);
            let r = canon(right);
            match op {
                BinOp::Add | BinOp::Sub => build_sum(flatten_sum(&l, &r, *op == BinOp::Add)),
                BinOp::Mul | BinOp::Div => {
                    build_product(flatten_product(&l, &r, *op == BinOp::Mul))
                }
                BinOp::Pow => canon_pow(l, r),
            }
        }
    }
}

fn canon_unary(op: UnaryOp, operand: &Expr) -> Expr {
    match op {
        // R1
        UnaryOp::Pos => operand.clone(),
        UnaryOp::Neg => canon_neg(operand),
    }
}

/// R2, R3, R4: fold into integers, cancel doubles, distribute over sums.
fn canon_neg(operand: &Expr) -> Expr {
    match operand {
        Expr::Integer(v) => match v.checked_neg() {
            Some(negated) => Expr::Integer(negated),
            None => Expr::UnaryOp {
                op: UnaryOp::Neg,
                operand: Box::new(operand.clone()),
            },
        },
        Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: inner,
        } => (**inner).clone(),
        Expr::BinOp {
            op: BinOp::Add | BinOp::Sub,
            ..
        } => {
            // R4: negate a sum by flipping every term's sign. The operand is
            // canonical, so its terms never carry a top-level Neg.
            let terms = flatten_sum_root(operand);
            let flipped: Vec<(i64, Expr)> = terms.into_iter().map(|(s, t)| (-s, t)).collect();
            build_sum(flipped)
        }
        other => Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(other.clone()),
        },
    }
}

/// R8 + R9: trivial and integer powers, negative powers become Div(1, ..).
fn canon_pow(base: Expr, exp: Expr) -> Expr {
    // x^1 -> x, for every x (0^1 = 0 included).
    if exp == Expr::Integer(1) {
        return base;
    }
    // b^0 -> 1 for every base, including 0 (R9: standard convention).
    if exp == Expr::Integer(0) {
        return Expr::Integer(1);
    }
    if let Expr::Integer(e) = &exp {
        if *e < 0 {
            // R8: b^-n is the RECIPROCAL 1 / b^n, for every base. The sign
            // of a negative base falls out of the denominator — e.g.
            // (-2)^-3 = 1 / (-8) -> -(1 / 8) — so there is no special case
            // here. e == i64::MIN has no positive i64 counterpart: keep
            // such powers unfolded rather than wrapping the magnitude.
            return match i64::try_from(e.unsigned_abs()) {
                Ok(n) => {
                    let den = canon(&Expr::BinOp {
                        op: BinOp::Pow,
                        left: Box::new(base.clone()),
                        right: Box::new(Expr::Integer(n)),
                    });
                    build_product(Factors {
                        num: vec![Expr::Integer(1)],
                        den: vec![den],
                    })
                }
                Err(_) => Expr::BinOp {
                    op: BinOp::Pow,
                    left: Box::new(base),
                    right: Box::new(exp),
                },
            };
        }
        // Integer base with positive exponent: fold when representable.
        // checked_pow handles the sign itself ((-2)^3 = -8) and returns
        // None on overflow or for |i64::MIN|^n, which stay unfolded.
        if let Expr::Integer(b) = &base {
            if let Ok(n) = u32::try_from(*e) {
                if let Some(v) = b.checked_pow(n) {
                    return Expr::Integer(v);
                }
            }
        }
    }
    // Symbolic bases, overflow, huge exponents and non-integer exponents
    // stay unfolded.
    Expr::BinOp {
        op: BinOp::Pow,
        left: Box::new(base),
        right: Box::new(exp),
    }
}

// ---------------------------------------------------------------------------
// sums: signed term lists
// ---------------------------------------------------------------------------

type Terms = Vec<(i64, Expr)>;

/// Flatten an Add/Sub whose operands are already canonical. `add` selects
/// the operator; Sub negates the right operand's signs. The result is a
/// term list whose entries never carry a top-level `Neg` (R4/R5 absorb it).
fn flatten_sum(left: &Expr, right: &Expr, add: bool) -> Terms {
    let mut terms = flatten_sum_root(left);
    let mut right_terms = flatten_sum_root(right);
    if !add {
        for (sign, _) in right_terms.iter_mut() {
            *sign = -*sign;
        }
    }
    terms.append(&mut right_terms);
    terms
}

/// Flatten one already-canonical expression as the root of a term list.
fn flatten_sum_root(expr: &Expr) -> Terms {
    match expr {
        Expr::BinOp {
            op: BinOp::Add,
            left,
            right,
        } => {
            let mut terms = flatten_sum_root(left);
            terms.append(&mut flatten_sum_root(right));
            terms
        }
        Expr::BinOp {
            op: BinOp::Sub,
            left,
            right,
        } => {
            let mut terms = flatten_sum_root(left);
            let mut right_terms = flatten_sum_root(right);
            for (sign, _) in right_terms.iter_mut() {
                *sign = -*sign;
            }
            terms.append(&mut right_terms);
            terms
        }
        // A term whose top node is Neg becomes a negative sign (R5). This is
        // safe because the expression is canonical: Neg never wraps a sum.
        Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand,
        } => vec![(-1, (**operand).clone())],
        other => vec![(1, other.clone())],
    }
}

/// Sort, fold integer terms (R5, R10), and rebuild.
fn build_sum(mut terms: Terms) -> Expr {
    terms.sort_by(|a, b| cmp_expr(&a.1, &b.1).then(a.0.cmp(&b.0)));

    // Fold integer terms with checked arithmetic; on overflow keep them all.
    let mut int_sum: Option<i64> = Some(0);
    for (sign, term) in &terms {
        if let Expr::Integer(v) = term {
            let signed = if *sign < 0 { v.checked_neg() } else { Some(*v) };
            match (int_sum, signed) {
                (Some(acc), Some(v)) => int_sum = acc.checked_add(v),
                _ => int_sum = None,
            }
            if int_sum.is_none() {
                break;
            }
        }
    }

    if let Some(total) = int_sum {
        let mut folded: Terms = terms
            .into_iter()
            .filter(|(_, t)| !matches!(t, Expr::Integer(_)))
            .collect();
        if total != 0 || folded.is_empty() {
            // A folded 0 disappears unless it is the whole sum (1 - 1 -> 0).
            folded.push((1, Expr::Integer(total)));
        }
        return sum_from_terms(folded);
    }
    sum_from_terms(terms)
}

/// Rebuild an expression from a signed term list. A leading negative term
/// becomes `Neg(term)` — except a negative *integer*, which folds into the
/// literal per R2, so canonicalize stays idempotent (`0 - 3` must not
/// rebuild as `Neg(Integer(3))`, which a fresh canon would re-fold).
fn sum_from_terms(terms: Terms) -> Expr {
    let mut iter = terms.into_iter();
    let (s0, t0) = iter.next().expect("term list is never empty");
    let mut acc = match (s0 < 0, t0) {
        (true, Expr::Integer(v)) => match v.checked_neg() {
            Some(negated) => Expr::Integer(negated),
            None => Expr::UnaryOp {
                op: UnaryOp::Neg,
                operand: Box::new(Expr::Integer(v)),
            },
        },
        (true, other) => Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(other),
        },
        (false, other) => other,
    };
    for (sign, term) in iter {
        acc = Expr::BinOp {
            op: if sign < 0 { BinOp::Sub } else { BinOp::Add },
            left: Box::new(acc),
            right: Box::new(term),
        };
    }
    acc
}

// ---------------------------------------------------------------------------
// products: numerator/denominator factor lists
// ---------------------------------------------------------------------------

#[derive(Default)]
struct Factors {
    num: Vec<Expr>,
    den: Vec<Expr>,
}

/// Flatten a Mul/Div whose operands are already canonical. A Div factor
/// contributes its numerator to `num` and its denominator to `den`, which
/// is what makes `a/b` and `a*b^-1` meet in one form (R6, R8).
fn flatten_product(left: &Expr, right: &Expr, mul: bool) -> Factors {
    let mut f = flatten_product_root(left);
    let right_f = flatten_product_root(right);
    if mul {
        f.num.extend(right_f.num);
        f.den.extend(right_f.den);
    } else {
        f.num.extend(right_f.den);
        f.den.extend(right_f.num);
    }
    f
}

fn flatten_product_root(expr: &Expr) -> Factors {
    match expr {
        Expr::BinOp {
            op: BinOp::Mul,
            left,
            right,
        } => {
            let mut f = flatten_product_root(left);
            let r = flatten_product_root(right);
            f.num.extend(r.num);
            f.den.extend(r.den);
            f
        }
        Expr::BinOp {
            op: BinOp::Div,
            left,
            right,
        } => {
            let mut f = flatten_product_root(left);
            let r = flatten_product_root(right);
            f.num.extend(r.den);
            f.den.extend(r.num);
            f
        }
        // Canonical Neg factors become a product-wide sign via the rebuild
        // step; keep the Neg here and let fold_product see it.
        other => Factors {
            num: vec![other.clone()],
            den: Vec::new(),
        },
    }
}

/// Fold integer factors, cancel by gcd (R6, R7), sort (R10), rebuild.
fn build_product(mut f: Factors) -> Expr {
    let mut sign: i64 = 1;

    // Pull Neg factors into the product-wide sign (they are never Neg over a
    // sum: R4 removed those before products are built). The operand is a
    // canonical sub-product or atom, so it is FLATTENED back into the lists:
    // a Neg over a Mul/Div chain pushed as one factor would stay nested and
    // reassociation would break agreement. A numerator-side Neg keeps its
    // sub-numerators up here and sends its sub-denominators down.
    let mut pulled_den: Vec<Expr> = Vec::new();
    let mut plain_num = Vec::with_capacity(f.num.len());
    for factor in f.num.drain(..) {
        match factor {
            Expr::UnaryOp {
                op: UnaryOp::Neg,
                operand,
            } => {
                sign = -sign;
                let sub = flatten_product_root(&operand);
                plain_num.extend(sub.num);
                pulled_den.extend(sub.den);
            }
            other => plain_num.push(other),
        }
    }
    f.num = plain_num;
    f.den.extend(pulled_den);

    // Denominator side: pull Negs out of divisor factors. A pull here flips
    // the product sign too; the operand's sub-numerators stay divisors and
    // its sub-denominators come up into the numerator (1 / (n / d) = d / n).
    // The existing numerator never moves.
    let mut plain_den = Vec::with_capacity(f.den.len());
    let mut up_num: Vec<Expr> = Vec::new();
    for factor in f.den.drain(..) {
        match factor {
            Expr::UnaryOp {
                op: UnaryOp::Neg,
                operand,
            } => {
                sign = -sign;
                let sub = flatten_product_root(&operand);
                plain_den.extend(sub.num);
                up_num.extend(sub.den);
            }
            other => plain_den.push(other),
        }
    }
    f.den = plain_den;
    f.num.extend(up_num);

    // Fold numerator and denominator integers; all-or-nothing on overflow.
    // An empty or purely symbolic list folds to 1, so a product with
    // integers on one side only still reaches the folding branch.
    let num_int = fold_ints(&f.num);
    let den_int = fold_ints(&f.den);

    match (num_int, den_int) {
        (Some(np), Some(dp)) => {
            if np == 0 {
                // Documented in R6: 0 * anything (even 0/0) folds to 0.
                return Expr::Integer(0);
            }
            // |i64::MIN| does not exist; keep such factors unfolded. The
            // pulled-out sign (from Neg factors above) must still be applied.
            if np == i64::MIN || dp == i64::MIN {
                return apply_product_sign(sign, product_from_factors(f));
            }
            let mut np = np;
            let mut dp = dp;
            if np < 0 {
                sign = -sign;
                np = -np;
            }
            if dp < 0 {
                sign = -sign;
                dp = -dp;
            }
            // The folded pair now carries every integer factor; remove the
            // originals from the lists so they cannot appear twice.
            f.num.retain(|t| !matches!(t, Expr::Integer(_)));
            f.den.retain(|t| !matches!(t, Expr::Integer(_)));
            if dp == 0 {
                // 0 in the denominator: no cancellation (would divide by 0).
                if np > 1 {
                    f.num.push(Expr::Integer(np));
                }
                f.den.push(Expr::Integer(0));
                return apply_product_sign(sign, product_from_factors(f));
            }
            let g = gcd(np, dp);
            let (np, dp) = (np / g, dp / g);
            if np > 1 {
                f.num.push(Expr::Integer(np));
            }
            if dp > 1 {
                f.den.push(Expr::Integer(dp));
            }
            apply_product_sign(sign, product_from_factors(f))
        }
        // Overflow (or no integer factors at all): no folding, no
        // cancellation (documented in R6) — but the sign pulled from Neg
        // factors must still be applied, or (-x)/2 would lose its minus.
        _ => apply_product_sign(sign, product_from_factors(f)),
    }
}

/// Product of every Integer factor with checked arithmetic. An empty or
/// purely symbolic list is the identity 1; `None` on overflow, in which
/// case the caller keeps every factor unfolded (all-or-nothing).
fn fold_ints(factors: &[Expr]) -> Option<i64> {
    let mut acc: Option<i64> = Some(1);
    for factor in factors {
        if let Expr::Integer(v) = factor {
            acc = acc.and_then(|a| a.checked_mul(*v));
        }
    }
    acc
}

fn gcd(mut a: i64, mut b: i64) -> i64 {
    while b != 0 {
        let r = a % b;
        a = b;
        b = r;
    }
    a
}

/// Apply a product-wide negative sign without ever wrapping a sum directly:
/// negate integer factors when present (R2-style fold), else flip a lone
/// sum's term signs (R4), else wrap in Neg.
fn apply_product_sign(sign: i64, body: Expr) -> Expr {
    if sign > 0 {
        return body;
    }
    match body {
        Expr::Integer(v) => match v.checked_neg() {
            Some(n) => Expr::Integer(n),
            None => Expr::UnaryOp {
                op: UnaryOp::Neg,
                operand: Box::new(Expr::Integer(v)),
            },
        },
        Expr::BinOp {
            op: BinOp::Add | BinOp::Sub,
            ..
        } => {
            let terms = flatten_sum_root(&body);
            let flipped: Vec<(i64, Expr)> = terms.into_iter().map(|(s, t)| (-s, t)).collect();
            build_sum(flipped)
        }
        // A lone sum factor body is rebuilt above; multi-factor bodies are
        // Mul chains, and Neg over a Mul chain is a legitimate normal form.
        other => Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(other),
        },
    }
}

/// Sort factors (R10) and rebuild as Mul chains over Div. Trivial
/// `Integer(1)` factors are dropped here too (R7), so the overflow path in
/// `build_product` cannot leave a literal `1` behind.
fn product_from_factors(f: Factors) -> Expr {
    let mut num = f.num;
    let mut den = f.den;
    num.retain(|factor| *factor != Expr::Integer(1));
    den.retain(|factor| *factor != Expr::Integer(1));
    num.sort_by(cmp_expr);
    den.sort_by(cmp_expr);
    let numer = mul_chain(num);
    if den.is_empty() {
        return numer;
    }
    Expr::BinOp {
        op: BinOp::Div,
        left: Box::new(numer),
        right: Box::new(mul_chain(den)),
    }
}

fn mul_chain(mut factors: Vec<Expr>) -> Expr {
    match factors.len() {
        0 => Expr::Integer(1),
        1 => factors.pop().expect("len checked"),
        _ => {
            let mut iter = factors.into_iter();
            let mut acc = iter.next().expect("len checked");
            for factor in iter {
                acc = Expr::BinOp {
                    op: BinOp::Mul,
                    left: Box::new(acc),
                    right: Box::new(factor),
                };
            }
            acc
        }
    }
}

// ---------------------------------------------------------------------------
// total order over Expr (R10)
// ---------------------------------------------------------------------------

/// A hand-written total order over expressions. `f64` inside `Decimal`
/// blocks a derived `Ord` (no total order on floats), and NaN cannot occur
/// (the parser only produces finite decimals), so this order is defined on
/// the bit pattern, which is total for every value that can appear.
///
/// Variant rank: Integer < Decimal < Constant < Variable < Call < UnaryOp
/// < BinOp. Within a variant: integers by value, decimals by bit pattern,
/// constants by discriminant (Pi < E), variables and function names by
/// bytes, operators by fixed discriminant order, children left-to-right.
pub fn cmp_expr(a: &Expr, b: &Expr) -> Ordering {
    fn rank(e: &Expr) -> u8 {
        match e {
            Expr::Integer(_) => 0,
            Expr::Decimal(_) => 1,
            Expr::Constant(_) => 2,
            Expr::Variable(_) => 3,
            Expr::Call { .. } => 4,
            Expr::UnaryOp { .. } => 5,
            Expr::BinOp { .. } => 6,
        }
    }
    let (ra, rb) = (rank(a), rank(b));
    if ra != rb {
        return ra.cmp(&rb);
    }
    match (a, b) {
        (Expr::Integer(x), Expr::Integer(y)) => x.cmp(y),
        (Expr::Decimal(x), Expr::Decimal(y)) => x.to_bits().cmp(&y.to_bits()),
        (Expr::Constant(x), Expr::Constant(y)) => (*x as u8).cmp(&(*y as u8)),
        (Expr::Variable(x), Expr::Variable(y)) => x.as_bytes().cmp(y.as_bytes()),
        (Expr::Call { func: f, args: a2 }, Expr::Call { func: g, args: b2 }) => {
            let fa: usize = *f as usize;
            let ga: usize = *g as usize;
            fa.cmp(&ga).then_with(|| {
                // Same function => same arity, so zip is safe; different
                // arities cannot occur for the closed function set.
                a2.iter()
                    .zip(b2.iter())
                    .map(|(x, y)| cmp_expr(x, y))
                    .find(|o| o.is_ne())
                    .unwrap_or(Ordering::Equal)
            })
        }
        (Expr::UnaryOp { op: x, operand: xa }, Expr::UnaryOp { op: y, operand: ya }) => {
            (*x as u8).cmp(&(*y as u8)).then_with(|| cmp_expr(xa, ya))
        }
        (
            Expr::BinOp {
                op: x,
                left: xl,
                right: xr,
            },
            Expr::BinOp {
                op: y,
                left: yl,
                right: yr,
            },
        ) => (*x as u8)
            .cmp(&(*y as u8))
            .then_with(|| cmp_expr(xl, yl))
            .then_with(|| cmp_expr(xr, yr)),
        // Ranks differ or identical single-variant constructors matched
        // above; equal-rank mismatched constructors cannot occur.
        _ => Ordering::Equal,
    }
}

// ---------------------------------------------------------------------------
// structural hash
// ---------------------------------------------------------------------------

/// Hash of the canonical form: `structural_hash(a) == structural_hash(b)`
/// exactly when the canonicaliser deems `a` and `b` equivalent (up to
/// 64-bit collisions).
pub fn structural_hash(expr: &Expr) -> u64 {
    hash_canonical(&canonicalize(expr))
}

/// FNV-1a 64 over the canonical serialisation of an already-canonical
/// expression. Prefer [`structural_hash`] unless the input is known to be
/// canonical already.
pub fn hash_canonical(expr: &Expr) -> u64 {
    let mut out = String::new();
    serialize(expr, &mut out);
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in out.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// Deterministic byte serialisation: variant tag, then payload, then
/// children. Documents the exact format the hash depends on.
fn serialize(expr: &Expr, out: &mut String) {
    match expr {
        Expr::Integer(v) => {
            out.push('I');
            out.push_str(&v.to_string());
            out.push(';');
        }
        Expr::Decimal(v) => {
            // Bit pattern, not Display: two different spellings of the same
            // f64 must hash alike, and the parser produces finite values only.
            out.push('D');
            out.push_str(&v.to_bits().to_string());
            out.push(';');
        }
        Expr::Constant(c) => {
            out.push('C');
            out.push_str(c.name());
            out.push(';');
        }
        Expr::Variable(name) => {
            out.push('V');
            out.push_str(name);
            out.push(';');
        }
        Expr::Call { func, args } => {
            out.push('F');
            out.push_str(func.name());
            out.push('(');
            for arg in args {
                serialize(arg, out);
            }
            out.push(')');
        }
        Expr::UnaryOp { op, operand } => {
            out.push('U');
            out.push_str(op.symbol());
            out.push('(');
            serialize(operand, out);
            out.push(')');
        }
        Expr::BinOp { op, left, right } => {
            out.push('B');
            out.push_str(op.symbol());
            out.push('(');
            serialize(left, out);
            out.push(',');
            serialize(right, out);
            out.push(')');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn canon_s(input: &str) -> Expr {
        canonicalize(&crate::parser::parse(input).expect(input))
    }

    fn s(expr: &Expr) -> String {
        crate::parser::print(expr)
    }

    fn assert_canon(input: &str, expected: &str) {
        let got = canon_s(input);
        let want = crate::parser::parse(expected).expect(expected);
        assert_eq!(
            got,
            want,
            "canonicalize({:?}): got {}, want {}",
            input,
            s(&got),
            expected
        );
    }

    // R1: unary plus disappears.
    #[test]
    fn r1_unary_plus_disappears() {
        assert_canon("+x", "x");
        assert_canon("+(x + 1)", "x + 1");
        assert_canon("2 * +3", "2 * 3");
    }

    // R2: negation folds into integers when representable.
    #[test]
    fn r2_negation_folds_into_integers() {
        assert_canon("-(3)", "-3");
        assert_canon("-(x)", "-x");
        // i64::MIN has no positive counterpart; the Neg node is preserved.
        // (Cannot be written as a literal: the lexer rejects the 19-digit
        // magnitude as out of range, so build the AST directly.)
        let min = Expr::UnaryOp {
            op: UnaryOp::Neg,
            operand: Box::new(Expr::Integer(i64::MIN)),
        };
        assert_eq!(canonicalize(&min), min);
    }

    // R3: double negation cancels.
    #[test]
    fn r3_double_negation_cancels() {
        assert_canon("--x", "x");
        assert_canon("- - 5", "5");
        assert_canon("- - -x", "-x");
    }

    // R4: negation distributes over sums only.
    #[test]
    fn r4_negation_distributes_over_sums() {
        assert_canon("-(x + y)", "-x - y");
        assert_canon("-(x - y)", "-x + y");
        // ...but not over products.
        assert_canon("-(x * y)", "-(x * y)");
        assert_canon("-(x / y)", "-(x / y)");
    }

    // R5: sums flatten and fold; +0 disappears; overflow stays unfolded.
    #[test]
    fn r5_sums_flatten_and_fold() {
        assert_canon("x + 0", "x");
        assert_canon("0 + x", "x");
        assert_canon("2 + x + 3", "5 + x");
        assert_canon("(1 - 3) + x", "-2 + x");
        assert_canon("x - (y - z)", "x - y + z");
        assert_canon("1 - 1", "0");
        assert_canon("3 - 5 + 2", "0");
        // Associative flattening across a subtraction:
        assert_canon("a - b - c", "a - b - c");
        assert_canon("a - (b + c)", "a - b - c");
        assert_canon("a - (b - c)", "a - b + c");
    }

    #[test]
    fn r5_integer_overflow_stays_unfolded() {
        // 9223372036854775807 + 1 overflows: the terms must survive, sorted,
        // with no silent wrap. Sorted order is 1, then i64::MAX, then x.
        let c = canon_s("9223372036854775807 + x + 1");
        assert_eq!(s(&c), "1 + 9223372036854775807 + x");
        // The unwrapped pair is still there, not folded into a wrong value.
        assert!(matches!(c, Expr::BinOp { op: BinOp::Add, .. }));
    }

    // R6: products flatten into num/den, fold, and cancel by gcd.
    #[test]
    fn r6_products_flatten_fold_cancel() {
        assert_canon("6 / 4", "3 / 2");
        assert_canon("8 / 2", "4");
        assert_canon("2 * x * 3 * y", "6 * x * y");
        assert_canon("(2 * x) / (4 * y)", "x / (2 * y)");
        assert_canon("x * 2 / y * 3", "6 * x / y");
        // Deliberate limitation: no symbolic cancellation. Only folded
        // integers cancel by gcd; x*x/(x) is real algebra, not list bookkeeping.
        assert_canon("(x * y) / (y * z)", "x * y / (y * z)");
    }

    #[test]
    fn r6_zero_and_zero_denominator() {
        assert_canon("0 * x", "0");
        assert_canon("0 / x", "0");
        assert_canon("x * 0", "0");
        assert_canon("0 / 0", "0"); // documented decision in R6
        assert_canon("x / 0", "x / 0"); // nothing to cancel against
        assert_canon("5 / (2 * 0)", "5 / 0");
    }

    #[test]
    fn r6_sign_algebra_in_products() {
        assert_canon("(-x) * y", "-(x * y)");
        assert_canon("(-x) / 2", "-(x / 2)"); // the sign-loss bug's test
        assert_canon("(-x) * (-y)", "x * y");
        // A folded integer's sign lives in the literal; the pulled-out sign
        // reattaches as Neg over the sorted product (one documented form).
        assert_canon("(-2) * x", "-(2 * x)");
        assert_canon("(-2) * (-3)", "6");
        assert_canon("x / (-2)", "-(x / 2)");
        assert_canon("(-x) / 2 * y", "-(x * y / 2)"); // sign must survive the Div
    }

    #[test]
    fn r6_product_overflow_stays_unfolded() {
        let c = canon_s("9223372036854775807 * 3 * x");
        // Must not wrap; factors stay (sorted: 3 < i64::MAX < x).
        let text = s(&c);
        assert!(
            text.contains("3 * 9223372036854775807"),
            "integer factors vanished or wrapped: {}",
            text
        );
        assert!(text.contains("x"), "got: {}", text);
    }

    // R7: trivial factors vanish; bare 1 stays 1.
    #[test]
    fn r7_trivial_factors_vanish() {
        assert_canon("x * 1", "x");
        assert_canon("1 * x", "x");
        assert_canon("x / 1", "x");
        assert_canon("1 * 1", "1");
        assert_canon("1", "1");
        assert_canon("x * 1 * y * 1", "x * y");
    }

    // R8: a/b and a*b^-1 meet in Div; negative powers become reciprocals.
    #[test]
    fn r8_reciprocal_forms_meet() {
        assert_canon("x ^ -1", "1 / x");
        assert_canon("x * y ^ -1", "x / y");
        assert_canon("a / b", "a / b");
        assert_canon("a / b / c", "a / (b * c)");
        assert_canon("a * b ^ -1 * c ^ -1", "a / (b * c)");
        assert_canon("2 ^ -3", "1 / 8");
        // Negative odd base: the sign falls out of the negative denominator.
        assert_canon("(-2) ^ -3", "-(1 / 8)");
        assert_canon("(-2) ^ -2", "1 / 4");
    }

    // R9: integer powers fold; corners preserved.
    #[test]
    fn r9_integer_powers_fold() {
        assert_canon("2 ^ 10", "1024");
        assert_canon("(-2) ^ 3", "-8");
        assert_canon("(-2) ^ 2", "4");
        assert_canon("x ^ 1", "x");
        assert_canon("x ^ 0", "1"); // b^0 -> 1 for every base (R9)
        assert_canon("5 ^ 1", "5");
        // 0^-n has one well-defined normal form, 1/0, which evaluation (B3)
        // rejects as a domain error. The canonicaliser does not hide it.
        assert_canon("0 ^ -2", "1 / 0");
        assert_canon("0 ^ 0", "1"); // same convention as Python/SymPy
    }

    #[test]
    fn r9_power_overflow_stays_unfolded() {
        let c = canon_s("10 ^ 30");
        assert!(
            matches!(c, Expr::BinOp { op: BinOp::Pow, .. }),
            "got: {}",
            s(&c)
        );
    }

    // R10: commutative operands sort into a total order.
    #[test]
    fn r10_commutative_operands_sort() {
        assert_canon("y + x", "x + y");
        assert_canon("y * x", "x * y");
        assert_canon("3 + 2 + 1", "6"); // folding beats sorting here
        assert_canon("z + y + x", "x + y + z");
        assert_canon("c * b * a", "a * b * c");
        // Rank order: Variable(3) < Call(4) < UnaryOp(5) < BinOp(6).
        assert_canon("sin(x) + y", "y + sin(x)");
        assert_canon("(a + b) + x", "x + (a + b)");
        // Same-rank Div terms compare by their right operand: 3 < 4, so
        // 1/3 sorts before 1/4.
        assert_canon("1 / 3 + 1 / 4", "1 / 3 + 1 / 4");
        // Constants sort by discriminant: Pi(0) before E(1).
        assert_canon("e + pi", "pi + e");
    }

    #[test]
    fn total_order_is_total_and_consistent() {
        let exprs = [
            canon_s("2"),
            canon_s("2.5"),
            canon_s("pi"),
            canon_s("x"),
            canon_s("x"),
            canon_s("sin(x)"),
            canon_s("-x"),
            canon_s("x + y"),
            canon_s("x * y"),
            canon_s("x ^ y"),
        ];
        for a in &exprs {
            for b in &exprs {
                let ab = cmp_expr(a, b);
                let ba = cmp_expr(b, a);
                // Antisymmetry.
                assert_eq!(ab, ba.reverse(), "not antisymmetric: {} vs {}", s(a), s(b));
                if a == b {
                    assert_eq!(ab, Ordering::Equal, "equal exprs must compare equal");
                }
            }
        }
        // Transitivity spot check on the sorted list.
        let mut sorted = exprs.clone();
        sorted.sort_by(cmp_expr);
        for w in sorted.windows(2) {
            assert!(
                cmp_expr(&w[0], &w[1]) != Ordering::Greater,
                "sort left an inversion: {} > {}",
                s(&w[0]),
                s(&w[1])
            );
        }
    }

    // ---- hash ----

    #[test]
    fn hash_is_stable_and_equivalence_sensitive() {
        assert_eq!(
            structural_hash(&canon_s("x + y")),
            structural_hash(&canon_s("y + x"))
        );
        assert_eq!(
            structural_hash(&canon_s("x * 1")),
            structural_hash(&canon_s("x"))
        );
        assert_ne!(
            structural_hash(&canon_s("x + y")),
            structural_hash(&canon_s("x - y"))
        );
        assert_ne!(
            structural_hash(&canon_s("2 / 4")),
            structural_hash(&canon_s("3 / 4"))
        );
        // Same value, different spelling: 2.50 == 2.5.
        assert_eq!(
            structural_hash(&canon_s("2.50")),
            structural_hash(&canon_s("2.5"))
        );
        // Hash is deterministic across calls.
        let e = canon_s("x + y * z");
        assert_eq!(hash_canonical(&e), hash_canonical(&e));
    }

    #[test]
    fn canonical_form_is_idempotent() {
        for input in [
            "x + y * z",
            "(a - b) / (c * d) + 3",
            "-(x + y) * 2 ^ -3",
            "6 / 4 * x + y - z ^ -2",
            "9223372036854775807 + x + 1",
            "x / 0",
            "0 ^ 0",
        ] {
            let once = canon_s(input);
            let twice = canonicalize(&once);
            assert_eq!(
                twice,
                once,
                "canonicalize is not idempotent on {:?}: {} -> {}",
                input,
                s(&once),
                s(&twice)
            );
        }
    }

    #[test]
    fn canonical_form_round_trips_through_print_parse() {
        for input in [
            "x + y * z",
            "(a - b) / (c * d) + 3",
            "-(x + y) * 2 ^ -3",
            "6 / 4 * x + y - z ^ -2",
            "(-2) ^ -3",
            "x / 0",
        ] {
            let c = canon_s(input);
            let reparsed = crate::parser::parse(&s(&c)).expect("canonical forms must re-parse");
            assert_eq!(
                reparsed, c,
                "round-trip changed the canonical form of {:?}",
                input
            );
        }
    }
}
