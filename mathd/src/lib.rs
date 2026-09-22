//! mathd: CAS-verified math expression parser, canonicalizer, and evaluator.
//!
//! This is a std-only Rust library that parses mathematical expressions into an
//! AST, canonicalizes them, and evaluates them with external oracle verification.

pub mod parser;
pub mod canonicalizer;
pub mod evaluator;
pub mod differentiator;

pub use parser::{parse, Expr, ParseError, BinOp, UnaryOp};
pub use canonicalizer::canonicalize;
pub use evaluator::{evaluate, verify_with_oracle, OracleRequest, OracleVerification, EvaluationError};
pub use differentiator::{differentiate, finite_differences};
