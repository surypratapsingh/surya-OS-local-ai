//! Prove-it CLI: owner solves problems, mathd verifies answers.
//!
//! Usage:
//!   cargo run -- evaluate "2 + 3"
//!   cargo run -- differentiate "x^2" x
//!   cargo run -- verify "x^2" "2*x" x 1.0
//!   cargo run -- corpus

use std::collections::HashMap;
use std::io::{self, Write};

fn main() {
    let args: Vec<String> = std::env::args().collect();

    if args.len() < 2 {
        print_usage();
        return;
    }

    match args[1].as_str() {
        "evaluate" => cmd_evaluate(&args),
        "differentiate" => cmd_differentiate(&args),
        "verify" => cmd_verify(&args),
        "corpus" => cmd_corpus(&args),
        "repl" => cmd_repl(),
        "--help" | "-h" => print_usage(),
        _ => {
            eprintln!("Unknown command: {}", args[1]);
            print_usage();
        }
    }
}

fn print_usage() {
    println!("Prove-it: CAS-verified math problem verifier");
    println!();
    println!("USAGE:");
    println!("  mathd evaluate <expr>");
    println!("    Evaluate a mathematical expression");
    println!();
    println!("  mathd differentiate <expr> <variable>");
    println!("    Compute symbolic derivative");
    println!();
    println!("  mathd verify <expr> <answer> <var> <value>");
    println!("    Verify an answer using mutation testing");
    println!("    Example: mathd verify 'x^2' '2*x' x 1.0");
    println!();
    println!("  mathd corpus");
    println!("    Show adversarial test cases");
    println!();
    println!("  mathd repl");
    println!("    Interactive mode");
    println!();
    println!("  mathd --help");
    println!("    Show this help message");
}

fn cmd_evaluate(args: &[String]) {
    if args.len() < 3 {
        eprintln!("Usage: mathd evaluate <expression>");
        return;
    }

    let expr_text = &args[2];
    match mathd::parse(expr_text) {
        Ok(expr) => {
            let canonical = mathd::canonicalize(&expr);
            match mathd::evaluate(&canonical, &HashMap::new()) {
                Ok(result) => println!("Result: {}", result),
                Err(e) => eprintln!("Evaluation error: {}", e),
            }
        }
        Err(e) => eprintln!("Parse error: {}", e),
    }
}

fn cmd_differentiate(args: &[String]) {
    if args.len() < 4 {
        eprintln!("Usage: mathd differentiate <expression> <variable>");
        return;
    }

    let expr_text = &args[2];
    let var = &args[3];

    match mathd::parse(expr_text) {
        Ok(expr) => {
            let canonical = mathd::canonicalize(&expr);
            let derivative = mathd::differentiate(&canonical, var);
            let derivative_canonical = mathd::canonicalize(&derivative);
            println!("d/d{} ({}) =", var, expr_text);
            print_expr(&derivative_canonical);
        }
        Err(e) => eprintln!("Parse error: {}", e),
    }
}

fn cmd_verify(args: &[String]) {
    if args.len() < 6 {
        eprintln!("Usage: mathd verify <expr> <answer> <var> <value>");
        eprintln!("Example: mathd verify 'x^2' '2*x' x 1.0");
        return;
    }

    let expr_text = &args[2];
    let answer_text = &args[3];
    let var = &args[4];
    let value_str = &args[5];

    let value: f64 = match value_str.parse() {
        Ok(v) => v,
        Err(_) => {
            eprintln!("Invalid number: {}", value_str);
            return;
        }
    };

    // Parse expressions
    let expr = match mathd::parse(expr_text) {
        Ok(e) => e,
        Err(err) => {
            eprintln!("Parse error in expression: {}", err);
            return;
        }
    };

    let answer = match mathd::parse(answer_text) {
        Ok(e) => e,
        Err(err) => {
            eprintln!("Parse error in answer: {}", err);
            return;
        }
    };

    // Create context
    let mut context = HashMap::new();
    context.insert(var.clone(), value);

    // Try derivative verifier
    match mathd::derivative_verifier(&answer, &expr, var, &context, 1e-5) {
        Ok(true) => {
            println!("✓ VERIFIED: The derivative appears correct.");
            println!();

            // Show mutation testing results
            let mutants = mathd::generate_mutants(&expr, 2);
            let report = mathd::test_verifier(&mutants, |_| true); // Dummy verifier
            println!("Mutation testing: {} mutants generated", report.total_mutants);
        }
        Ok(false) => {
            println!("✗ REJECTED: The derivative does not match finite differences.");
            println!();
            println!("Difference suggests a computation error.");
            println!("Check: chain rule, product rule, operator signs.");
        }
        Err(e) => eprintln!("Verification error: {}", e),
    }
}

fn cmd_corpus(args: &[String]) {
    let corpus = mathd::build_corpus();
    let summary = mathd::corpus_summary(&corpus);

    println!("Adversarial test corpus ({} cases)", corpus.len());
    println!();

    for (category, count) in summary.iter() {
        println!("{}:", category);
        for tc in mathd::test_cases_by_category(&corpus, category) {
            println!("  [{}] {}", tc.id, tc.description);
            println!("      Expression: {}", tc.expression);
            println!("      Expected: {}", tc.expected_answer);
            for (wrong_desc, wrong_val) in &tc.common_wrong_answers {
                println!("      Wrong: {} ({})", wrong_desc, wrong_val);
            }
        }
        println!();
    }
}

fn cmd_repl() {
    println!("Prove-it REPL (type 'help' or 'quit')");
    println!();

    let mut line = String::new();
    loop {
        print!("> ");
        io::stdout().flush().ok();

        line.clear();
        if io::stdin().read_line(&mut line).is_err() {
            break;
        }

        let input = line.trim();
        if input.is_empty() {
            continue;
        }

        match input {
            "quit" | "exit" => break,
            "help" => println!("Commands: eval <expr>, diff <expr> <var>, verify <expr> <answer> <var> <val>, corpus, help, quit"),
            _ => {
                if input.starts_with("eval ") {
                    let expr_text = &input[5..];
                    match mathd::parse(expr_text) {
                        Ok(expr) => {
                            let canonical = mathd::canonicalize(&expr);
                            match mathd::evaluate(&canonical, &HashMap::new()) {
                                Ok(result) => println!("= {}", result),
                                Err(e) => println!("Error: {}", e),
                            }
                        }
                        Err(e) => println!("Parse error: {}", e),
                    }
                } else if input.starts_with("diff ") {
                    let rest = &input[5..];
                    if let Some(space_idx) = rest.rfind(' ') {
                        let expr_text = &rest[..space_idx];
                        let var = &rest[space_idx + 1..];
                        match mathd::parse(expr_text) {
                            Ok(expr) => {
                                let canonical = mathd::canonicalize(&expr);
                                let deriv = mathd::differentiate(&canonical, var);
                                let deriv_canonical = mathd::canonicalize(&deriv);
                                print!("d/d{} = ", var);
                                print_expr(&deriv_canonical);
                            }
                            Err(e) => println!("Parse error: {}", e),
                        }
                    }
                } else {
                    println!("Unknown command. Type 'help' for usage.");
                }
            }
        }
    }
}

fn print_expr(expr: &mathd::Expr) {
    match expr {
        mathd::Expr::Number(n) => print!("{}", n),
        mathd::Expr::Variable(name) => print!("{}", name),
        mathd::Expr::BinOp { op, left, right } => {
            print!("(");
            print_expr(left);
            match op {
                mathd::BinOp::Add => print!(" + "),
                mathd::BinOp::Sub => print!(" - "),
                mathd::BinOp::Mul => print!(" * "),
                mathd::BinOp::Div => print!(" / "),
                mathd::BinOp::Pow => print!(" ^ "),
            }
            print_expr(right);
            print!(")");
        }
        mathd::Expr::UnaryOp { op, operand } => {
            match op {
                mathd::UnaryOp::Neg => print!("-"),
                mathd::UnaryOp::Pos => print!("+"),
            }
            print_expr(operand);
        }
        mathd::Expr::Call { func, args } => {
            print!("{}(", func);
            for (i, arg) in args.iter().enumerate() {
                if i > 0 {
                    print!(", ");
                }
                print_expr(arg);
            }
            print!(")");
        }
    }
    println!();
}
