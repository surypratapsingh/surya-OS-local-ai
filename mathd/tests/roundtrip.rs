//! Round-trip property test — the B1 "done when" gate.
//!
//! Builds random well-formed ASTs directly (never via the parser), prints them
//! with `mathd::print`, re-parses, and requires `parse(print(ast))` to be
//! structurally identical to `ast`. 10,000 generated expressions across four
//! fixed seeds, so a failure reproduces exactly.
//!
//! Independence note: the generator constructs ASTs by hand from a PRNG, not
//! by parsing text, so the property genuinely exercises print/parse against
//! structures that did not originate in the parser. The PRNG is a std-only
//! xorshift64* with committed constants; no randomness dependency.

use mathd::{parse, print, BinOp, Const, Expr, Function, UnaryOp};

struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }
}

const VARS: [&str; 6] = ["x", "y", "z", "n", "alpha_1", "t0"];
const FUNCS: [Function; 7] = [
    Function::Sin,
    Function::Cos,
    Function::Tan,
    Function::Exp,
    Function::Ln,
    Function::Sqrt,
    Function::Abs,
];

fn gen_leaf(rng: &mut Rng) -> Expr {
    match rng.below(4) {
        0 => Expr::Integer(rng.below(1001) as i64),
        // k/8 keeps decimals exactly representable in binary and short when
        // printed, so equality after a round-trip is bit-exact.
        1 => Expr::Decimal((rng.below(10_000) as f64) / 8.0),
        2 => Expr::Constant(if rng.below(2) == 0 {
            Const::Pi
        } else {
            Const::E
        }),
        _ => Expr::Variable(VARS[rng.below(VARS.len() as u64) as usize].to_string()),
    }
}

fn gen(rng: &mut Rng, depth: u32) -> Expr {
    let r = rng.below(100);
    if depth == 0 || r < 30 {
        gen_leaf(rng)
    } else if r < 70 {
        let op = match rng.below(5) {
            0 => BinOp::Add,
            1 => BinOp::Sub,
            2 => BinOp::Mul,
            3 => BinOp::Div,
            _ => BinOp::Pow,
        };
        Expr::BinOp {
            op,
            left: Box::new(gen(rng, depth - 1)),
            right: Box::new(gen(rng, depth - 1)),
        }
    } else if r < 85 {
        let op = if rng.below(2) == 0 {
            UnaryOp::Neg
        } else {
            UnaryOp::Pos
        };
        Expr::UnaryOp {
            op,
            operand: Box::new(gen(rng, depth - 1)),
        }
    } else {
        let func = FUNCS[rng.below(FUNCS.len() as u64) as usize];
        Expr::Call {
            func,
            args: vec![gen(rng, depth - 1)],
        }
    }
}

#[test]
fn round_trip_over_generated_expressions() {
    const CASES_PER_SEED: usize = 2_500;
    const SEEDS: [u64; 4] = [
        0xB17E_5EED_0000_0001,
        0xB17E_5EED_0000_0002,
        0xB17E_5EED_0000_0003,
        0xB17E_5EED_0000_0004,
    ];
    let mut checked = 0usize;

    for seed in SEEDS {
        let mut rng = Rng(seed);
        for _ in 0..CASES_PER_SEED {
            let depth = 1 + rng.below(6) as u32;
            let ast = gen(&mut rng, depth);

            let text = print(&ast);
            let reparsed = parse(&text).unwrap_or_else(|e| {
                panic!(
                    "case {} (seed {:#018x}): {:?} failed to re-parse: {}",
                    checked, seed, text, e
                )
            });
            assert_eq!(
                reparsed, ast,
                "case {} (seed {:#018x}): round-trip changed the AST; printed form was {:?}",
                checked, seed, text
            );

            // The spec's literal form: parse(print(parse(s))) produces a
            // structurally identical AST to parse(s), with s = the printed
            // text of the generated AST.
            let s = print(&reparsed);
            let third = parse(&s).unwrap_or_else(|e| {
                panic!(
                    "case {} (seed {:#018x}): double round-trip failed on {:?}: {}",
                    checked, seed, s, e
                )
            });
            assert_eq!(
                third, ast,
                "case {} (seed {:#018x}): double round-trip changed the AST ({:?})",
                checked, seed, s
            );

            checked += 1;
        }
    }

    assert_eq!(
        checked, 10_000,
        "must exercise exactly 10,000 generated expressions"
    );
}
