//! mathd — std-only math expression parser and AST (NOVA work order B1).
//!
//! Scope note: this crate currently contains **only** the B1 parser. The
//! previously committed canonicalizer, evaluator, differentiator, verifier,
//! corpus and card store were retracted by `docs/audit-2026-09-24.md`
//! (transcript: `docs/logs/audit-2026-09-24-mathd.log`): they never compiled,
//! depended on non-std crates (`sha2`, `chrono`), and their checks had never
//! run. They are removed rather than stubbed so that everything in the tree
//! has actually been exercised; the git history before the retraction
//! preserves every line. B2–B8 rebuild them against their specified oracles.

pub mod canonicalizer;
pub mod differentiator;
pub mod evaluator;
pub mod ledger;
pub mod parser;
pub mod verifier;

pub use canonicalizer::{canonicalize, cmp_expr, hash_canonical, structural_hash};
pub use differentiator::{differentiate, mentions_var};
pub use evaluator::{eval_at, EvalError};
pub use ledger::{
    escape_json, unescape_json, CounterexampleRecord, LedgerEntry, LedgerVerifyError, MistakeLedger,
};
pub use parser::{parse, print, BinOp, Const, Expr, Function, ParseError, UnaryOp};
pub use verifier::{
    extract_variables, generate_expression_mutations, verify_equation_solution, verify_equivalence,
    verify_factorisation, verify_identity, verify_integral, Equation, Evidence, MutationKind,
    Point, Verdict,
};

/// Type alias for the AST expression root type, matching work-order signatures.
pub type Ast = Expr;
