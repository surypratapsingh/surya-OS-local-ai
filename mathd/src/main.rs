//! mathd CLI — B1 scope: parse expressions and check print/parse round-trips.
//!
//! Evaluation, differentiation and verification are absent on purpose: the
//! pre-audit implementations were retracted by `docs/audit-2026-09-24.md` and
//! return with work orders B2–B6.
//!
//! Usage:
//!   mathd parse "<expr>"       print the AST and its canonical printed form
//!   mathd roundtrip "<expr>"   parse, print, re-parse; exit 1 on any mismatch
//!   mathd help                 show this message

use std::process::exit;

use mathd::{parse, print};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("parse") if args.len() == 3 => cmd_parse(&args[2]),
        Some("roundtrip") if args.len() == 3 => cmd_roundtrip(&args[2]),
        Some("help") | Some("--help") | Some("-h") | None => usage(),
        _ => {
            eprintln!("mathd: bad arguments");
            usage();
            exit(2);
        }
    }
}

fn usage() {
    println!(
        "mathd — expression parser (B1)\n\n\
         USAGE:\n  \
         mathd parse \"<expr>\"       print the AST and canonical form\n  \
         mathd roundtrip \"<expr>\"   verify print/parse round-trip\n  \
         mathd help                 this message\n\n\
         Supported syntax: integers, decimals, pi/π/e, variables,\n  \
         + - * / ^, unary minus, parentheses, and\n  \
         sin cos tan exp ln sqrt abs (one argument each).\n  \
         Unicode accepted: · ⋅ × ÷ − √ π"
    );
}

fn cmd_parse(input: &str) {
    match parse(input) {
        Ok(expr) => {
            println!("input:   {}", input);
            println!("printed: {}", print(&expr));
            println!("AST: {:#?}", expr);
        }
        Err(e) => {
            eprintln!("parse error: {}", e);
            exit(1);
        }
    }
}

fn cmd_roundtrip(input: &str) {
    let first = match parse(input) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("parse error: {}", e);
            exit(1);
        }
    };
    let text = print(&first);
    match parse(&text) {
        Ok(second) if second == first => {
            println!("round-trip identical: {}", text);
        }
        Ok(second) => {
            eprintln!("ROUND-TRIP MISMATCH");
            eprintln!("  input:    {}", input);
            eprintln!("  printed:  {}", text);
            eprintln!("  ast 1: {:?}", first);
            eprintln!("  ast 2: {:?}", second);
            exit(1);
        }
        Err(e) => {
            eprintln!("round-trip re-parse FAILED on {:?}: {}", text, e);
            exit(1);
        }
    }
}
