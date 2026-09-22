//! mathd: CAS-verified math expression parser, canonicalizer, and evaluator.
//!
//! This is a std-only Rust library that parses mathematical expressions into an
//! AST, canonicalizes them, and evaluates them with external oracle verification.

pub mod parser;
pub mod canonicalizer;
pub mod evaluator;
pub mod differentiator;
pub mod verifier;
pub mod corpus;

pub use parser::{parse, Expr, ParseError, BinOp, UnaryOp};
pub use canonicalizer::canonicalize;
pub use evaluator::{evaluate, verify_with_oracle, OracleRequest, OracleVerification, EvaluationError};
pub use differentiator::{differentiate, finite_differences};
pub use verifier::{generate_mutants, test_verifier, derivative_verifier, VerificationReport, Mutant};
pub use corpus::{build_corpus, TestCase, test_cases_by_category, corpus_summary};
