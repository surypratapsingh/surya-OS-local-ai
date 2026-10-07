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
pub mod parser;

pub use canonicalizer::{canonicalize, cmp_expr, hash_canonical, structural_hash};
pub use parser::{parse, print, BinOp, Const, Expr, Function, ParseError, UnaryOp};
