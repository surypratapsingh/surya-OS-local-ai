//! mathd: CAS-verified math expression parser, canonicalizer, and evaluator.
//!
//! This is a std-only Rust library that parses mathematical expressions into an
//! AST, canonicalizes them, and evaluates them with external oracle verification.

pub mod parser;

pub use parser::{parse, Expr, ParseError};
