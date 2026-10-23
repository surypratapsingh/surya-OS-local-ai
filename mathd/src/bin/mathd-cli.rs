//! `mathd-cli` — Terminal Prove-it mode and mistake ledger runner (work order B7).
//!
//! Scope:
//! - Terminal only. No GUI, no model, no network. Strictly std-only.
//! - Prove-it is the default mode: student enters problem answers / derivation steps.
//! - Every step is verified by `verifier::verify_equivalence` or `differentiator::differentiate`.
//! - On failure, displays the counterexample point and both values.
//! - Reveal requires an explicit `:reveal` command ("show me your working", not "here is the answer").
//! - Logs every attempt to an append-only hash-chained JSONL mistake ledger.

use std::collections::BTreeSet;
use std::env;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use mathd::differentiator::differentiate;
use mathd::ledger::MistakeLedger;
use mathd::parser::parse;
use mathd::verifier::{extract_variables, verify_equivalence, Verdict};

// ---------------------------------------------------------------------------
// 20-Problem Curated Session Corpus
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
pub enum ProblemKind {
    Equivalence,
    Derivative { var: &'static str },
}

pub struct Problem {
    pub id: &'static str,
    pub title: &'static str,
    pub problem_expr: &'static str,
    pub reference_answer: &'static str,
    pub kind: ProblemKind,
}

pub const PROBLEMS_20: [Problem; 20] = [
    Problem {
        id: "alg-01",
        title: "Expand the square: (x + 1)^2",
        problem_expr: "(x + 1)^2",
        reference_answer: "x^2 + 2*x + 1",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "alg-02",
        title: "Multiply difference of squares: (x - 2)*(x + 2)",
        problem_expr: "(x - 2)*(x + 2)",
        reference_answer: "x^2 - 4",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "alg-03",
        title: "Factor difference of cubes: x^3 - 1",
        problem_expr: "x^3 - 1",
        reference_answer: "(x - 1)*(x^2 + x + 1)",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "trig-04",
        title: "Pythagorean trigonometric identity: sin(x)^2 + cos(x)^2",
        problem_expr: "sin(x)^2 + cos(x)^2",
        reference_answer: "1",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "trig-05",
        title: "Tangent Pythagorean identity: 1 + tan(x)^2",
        problem_expr: "1 + tan(x)^2",
        reference_answer: "1 / cos(x)^2",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "alg-06",
        title: "Simplify algebraic expression: (x + y)^2 - (x - y)^2",
        problem_expr: "(x + y)^2 - (x - y)^2",
        reference_answer: "4*x*y",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "alg-07",
        title: "Simplify rational fraction: (x^2 - 9)/(x - 3)",
        problem_expr: "(x^2 - 9)/(x - 3)",
        reference_answer: "x + 3",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "calc-08",
        title: "Differentiate polynomial: d/dx (x^3 + 2*x^2 - 5*x + 7)",
        problem_expr: "x^3 + 2*x^2 - 5*x + 7",
        reference_answer: "3*x^2 + 4*x - 5",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "calc-09",
        title: "Differentiate product: d/dx (sin(x)*cos(x))",
        problem_expr: "sin(x)*cos(x)",
        reference_answer: "cos(x)^2 - sin(x)^2",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "calc-10",
        title: "Differentiate: d/dx (x * exp(x))",
        problem_expr: "x * exp(x)",
        reference_answer: "exp(x) + x * exp(x)",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "calc-11",
        title: "Differentiate composite function: d/dx (ln(x^2 + 1))",
        problem_expr: "ln(x^2 + 1)",
        reference_answer: "2*x / (x^2 + 1)",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "alg-12",
        title: "Expand binomial: (2*x + 3)^2",
        problem_expr: "(2*x + 3)^2",
        reference_answer: "4*x^2 + 12*x + 9",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "alg-13",
        title: "Factor fourth-power difference: x^4 - y^4",
        problem_expr: "x^4 - y^4",
        reference_answer: "(x^2 + y^2)*(x + y)*(x - y)",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "calc-14",
        title: "Differentiate trigonometric tangent: d/dx (tan(x))",
        problem_expr: "tan(x)",
        reference_answer: "1 / cos(x)^2",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "alg-15",
        title: "Expand cube: (x + 1)^3",
        problem_expr: "(x + 1)^3",
        reference_answer: "x^3 + 3*x^2 + 3*x + 1",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "trig-16",
        title: "Double-angle cosine identity: cos(2*x)",
        problem_expr: "cos(2*x)",
        reference_answer: "cos(x)^2 - sin(x)^2",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "calc-17",
        title: "Differentiate product: d/dx (x^2 * sin(x))",
        problem_expr: "x^2 * sin(x)",
        reference_answer: "2*x*sin(x) + x^2*cos(x)",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "alg-18",
        title: "Sum of rational expressions: 1 / (1 + x) + 1 / (1 - x)",
        problem_expr: "1 / (1 + x) + 1 / (1 - x)",
        reference_answer: "2 / (1 - x^2)",
        kind: ProblemKind::Equivalence,
    },
    Problem {
        id: "calc-19",
        title: "Differentiate radical function: d/dx (sqrt(x^2 + 1))",
        problem_expr: "sqrt(x^2 + 1)",
        reference_answer: "x / sqrt(x^2 + 1)",
        kind: ProblemKind::Derivative { var: "x" },
    },
    Problem {
        id: "alg-20",
        title: "Expand product with parameters: (x + a)*(x + b)",
        problem_expr: "(x + a)*(x + b)",
        reference_answer: "x^2 + (a + b)*x + a*b",
        kind: ProblemKind::Equivalence,
    },
];

// ---------------------------------------------------------------------------
// Step / Problem Verification Engine
// ---------------------------------------------------------------------------

pub fn check_answer(problem: &Problem, answer_text: &str) -> Result<Verdict, String> {
    let ans_ast = parse(answer_text).map_err(|e| format!("parse error in answer: {}", e))?;

    match problem.kind {
        ProblemKind::Equivalence => {
            let prob_ast = parse(problem.problem_expr)
                .map_err(|e| format!("parse error in problem: {}", e))?;

            let mut vars = BTreeSet::new();
            extract_variables(&prob_ast, &mut vars);
            extract_variables(&ans_ast, &mut vars);
            let var_refs: Vec<&str> = vars.iter().map(|s| s.as_str()).collect();

            Ok(verify_equivalence(&prob_ast, &ans_ast, &var_refs))
        }
        ProblemKind::Derivative { var } => {
            let prob_ast = parse(problem.problem_expr)
                .map_err(|e| format!("parse error in problem: {}", e))?;
            let deriv_ast = differentiate(&prob_ast, var);

            let mut vars = BTreeSet::new();
            extract_variables(&deriv_ast, &mut vars);
            extract_variables(&ans_ast, &mut vars);
            let var_refs: Vec<&str> = vars.iter().map(|s| s.as_str()).collect();

            Ok(verify_equivalence(&deriv_ast, &ans_ast, &var_refs))
        }
    }
}

// ---------------------------------------------------------------------------
// End-to-End 20-Problem Session Harness
// ---------------------------------------------------------------------------

pub fn run_session_harness<P: AsRef<Path>>(ledger_path: P) -> Result<(), String> {
    let ledger_path = ledger_path.as_ref();
    if ledger_path.exists() {
        let _ = std::fs::remove_file(ledger_path);
    }

    let mut ledger =
        MistakeLedger::open(ledger_path).map_err(|e| format!("failed to open ledger: {}", e))?;

    println!("=== Running B7 20-Problem Prove-It Session Harness ===");

    let mut correct_count = 0;
    let mut mistake_count = 0;

    // 1. Solve all 20 problems correctly
    for (i, prob) in PROBLEMS_20.iter().enumerate() {
        let start = Instant::now();
        let verdict = check_answer(prob, prob.reference_answer)
            .map_err(|e| format!("problem {}: check failed with error {}", prob.id, e))?;
        let elapsed_ms = start.elapsed().as_millis() as u64;

        match verdict {
            Verdict::Verified { ref evidence } => {
                correct_count += 1;
                println!(
                    "  [{}/20] OK: {} -> Verified ({} points tested)",
                    i + 1,
                    prob.id,
                    evidence.points_tested
                );
                // In prove-it mode, attempts are logged to the ledger
                ledger
                    .record_attempt(
                        prob.id,
                        prob.problem_expr,
                        prob.reference_answer,
                        &verdict,
                        elapsed_ms,
                    )
                    .map_err(|e| format!("ledger write failed: {}", e))?;
            }
            Verdict::Failed { .. } => {
                return Err(format!(
                    "FATAL: reference answer for {} was rejected by verifier!",
                    prob.id
                ));
            }
            Verdict::Unverifiable { ref reason } => {
                return Err(format!(
                    "FATAL: reference answer for {} was unverifiable: {}",
                    prob.id, reason
                ));
            }
        }
    }

    assert_eq!(correct_count, 20, "all 20 reference answers must verify");

    // 2. Inject deliberate mistakes to prove mistake ledger recordings and counterexamples
    println!("\n=== Injecting Deliberate Mistakes (Adversarial Control) ===");
    let mistakes = [
        ("alg-01", "x^2 + 1"),          // dropped linear term
        ("alg-02", "x^2 + 4"),          // sign error
        ("trig-04", "0"),               // wrong constant
        ("calc-08", "3*x^2 + 4*x + 5"), // sign error in derivative
        ("alg-12", "4*x^2 + 6*x + 9"),  // wrong cross-term
    ];

    for (prob_id, bad_answer) in mistakes {
        let prob = PROBLEMS_20
            .iter()
            .find(|p| p.id == prob_id)
            .expect("problem found");

        let start = Instant::now();
        let verdict = check_answer(prob, bad_answer)
            .map_err(|e| format!("unexpected error checking {}: {}", prob_id, e))?;
        let elapsed_ms = start.elapsed().as_millis() as u64;

        match verdict {
            Verdict::Failed {
                ref counterexample,
                expected,
                got,
            } => {
                mistake_count += 1;
                println!(
                    "  [MISTAKE CAUGHT] {}: answer '{}' failed!",
                    prob.id, bad_answer
                );
                print!("    Counterexample: ");
                for (k, v) in counterexample {
                    print!("{} = {:.4} ", k, v);
                }
                println!("| expected = {:.4}, got = {:.4}", expected, got);

                ledger
                    .record_attempt(prob.id, prob.problem_expr, bad_answer, &verdict, elapsed_ms)
                    .map_err(|e| format!("ledger write failed: {}", e))?;
            }
            Verdict::Verified { .. } => {
                return Err(format!(
                    "FATAL: false pass! Bad answer '{}' for {} was verified!",
                    bad_answer, prob.id
                ));
            }
            Verdict::Unverifiable { ref reason } => {
                return Err(format!(
                    "unverifiable on bad answer for {}: {}",
                    prob.id, reason
                ));
            }
        }
    }

    assert_eq!(mistake_count, 5, "all 5 injected mistakes must be caught");

    // 3. Verify ledger integrity
    println!("\n=== Cryptographic Ledger Integrity Check ===");
    let audited_entries = ledger
        .verify_integrity()
        .map_err(|e| format!("ledger verification failed: {}", e))?;

    println!(
        "  OK: Mistake ledger verified: {} total entries, sequence 1..{}, hash-chain intact.",
        audited_entries, audited_entries
    );
    assert_eq!(audited_entries, 25);

    // 4. Sabotage gate: tamper one byte in the ledger file and prove verification catches it
    println!("\n=== Sabotage Gate: Verifying Tampered Ledger Rejection ===");
    let raw = std::fs::read_to_string(ledger_path)
        .map_err(|e| format!("failed to read ledger: {}", e))?;
    let tampered = raw.replacen("x^2 + 1", "x^2 + 2", 1);
    std::fs::write(ledger_path, tampered)
        .map_err(|e| format!("failed to write tampered ledger: {}", e))?;

    let tampered_ledger = MistakeLedger::open(ledger_path)
        .map_err(|e| format!("failed to open tampered ledger: {}", e))?;
    match tampered_ledger.verify_integrity() {
        Ok(_) => return Err("FATAL: Sabotage gate failed: tampered ledger was accepted!".into()),
        Err(e) => {
            println!(
                "  SABOTAGE GATE PASS: Tampered ledger correctly refused with: {}",
                e
            );
        }
    }

    // Clean up test ledger file so no stray artifact is left in working directory
    let _ = std::fs::remove_file(ledger_path);

    println!("\n=== B7 Prove-It 20-Problem Session Harness: ALL 20 PASSED ===");
    Ok(())
}

// ---------------------------------------------------------------------------
// Interactive CLI
// ---------------------------------------------------------------------------

fn run_interactive(ledger_path: &Path) -> io::Result<()> {
    let mut ledger = MistakeLedger::open(ledger_path)?;

    println!("===========================================================");
    println!("  NOVA mathd Prove-It Mode (Work Order B7)");
    println!("  Terminal-only verification engine. Show me your working.");
    println!("  Commands: :reveal, :status, :help, :quit");
    println!("  Ledger: {}", ledger_path.display());
    println!("===========================================================\n");

    let stdin = io::stdin();
    let mut reader = stdin.lock();

    let mut current_idx = 0;
    let mut total_verified = 0;
    let mut total_mistakes = 0;
    let mut total_revealed = 0;

    while current_idx < PROBLEMS_20.len() {
        let prob = &PROBLEMS_20[current_idx];
        println!("-----------------------------------------------------------");
        println!("Problem [{}/20]: {}", current_idx + 1, prob.title);
        println!("Expression: {}", prob.problem_expr);
        println!("-----------------------------------------------------------");

        loop {
            print!("prove-it> ");
            io::stdout().flush()?;

            let mut line = String::new();
            if reader.read_line(&mut line)? == 0 {
                println!("\nSession terminated.");
                return Ok(());
            }

            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }

            match trimmed {
                ":quit" | "exit" | ":q" => {
                    println!("\nExiting prove-it session. Bye!");
                    return Ok(());
                }
                ":help" => {
                    println!("Commands:");
                    println!("  <answer>   Enter your answer or simplified expression");
                    println!("  :reveal    Explicitly reveal reference answer and advance");
                    println!("  :status    Display current session statistics");
                    println!("  :quit      Exit the session");
                    continue;
                }
                ":status" => {
                    println!("Session Status:");
                    println!("  Current Problem: {}/20", current_idx + 1);
                    println!("  Verified Answers: {}", total_verified);
                    println!("  Mistakes Recorded: {}", total_mistakes);
                    println!("  Revealed Problems: {}", total_revealed);
                    continue;
                }
                ":reveal" | "reveal" => {
                    total_revealed += 1;
                    println!("\n[REVEALED] Reference answer: {}", prob.reference_answer);
                    println!("Problem skipped. Moving to next problem.\n");
                    current_idx += 1;
                    break;
                }
                _ => {}
            }

            let start = Instant::now();
            match check_answer(prob, trimmed) {
                Ok(Verdict::Verified { ref evidence }) => {
                    let elapsed_ms = start.elapsed().as_millis() as u64;
                    total_verified += 1;
                    println!(
                        "\n  [VERIFIED] Correct! Equivalent at {} tested points.",
                        evidence.points_tested
                    );
                    ledger.record_attempt(
                        prob.id,
                        prob.problem_expr,
                        trimmed,
                        &Verdict::Verified {
                            evidence: evidence.clone(),
                        },
                        elapsed_ms,
                    )?;
                    println!("Advancing to next problem.\n");
                    current_idx += 1;
                    break;
                }
                Ok(Verdict::Failed {
                    ref counterexample,
                    expected,
                    got,
                }) => {
                    let elapsed_ms = start.elapsed().as_millis() as u64;
                    total_mistakes += 1;
                    println!("\n  [FAILED] Not equivalent. Counterexample point:");
                    for (k, v) in counterexample {
                        println!("    {} = {:.6}", k, v);
                    }
                    println!("    Expected value: {:.6}", expected);
                    println!("    Claimed value:  {:.6}", got);
                    println!("  Logged to mistake ledger. Try again!\n");

                    ledger.record_attempt(
                        prob.id,
                        prob.problem_expr,
                        trimmed,
                        &Verdict::Failed {
                            counterexample: counterexample.clone(),
                            expected,
                            got,
                        },
                        elapsed_ms,
                    )?;
                }
                Ok(Verdict::Unverifiable { ref reason }) => {
                    println!(
                        "\n  [UNVERIFIABLE] Could not verify with confidence: {}",
                        reason
                    );
                }
                Err(e) => {
                    let elapsed_ms = start.elapsed().as_millis() as u64;
                    total_mistakes += 1;
                    println!("\n  [SYNTAX ERROR] {}", e);
                    ledger.record_syntax_error(
                        prob.id,
                        prob.problem_expr,
                        trimmed,
                        &e,
                        elapsed_ms,
                    )?;
                }
            }
        }
    }

    println!("\n===========================================================");
    println!("  Congratulations! You completed all 20 problems.");
    println!("  Total verified: {}", total_verified);
    println!("  Total mistakes: {}", total_mistakes);
    println!("  Total revealed: {}", total_revealed);
    println!("===========================================================");

    Ok(())
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    let args: Vec<String> = env::args().collect();
    let default_ledger = PathBuf::from("mathd-mistakes.jsonl");

    let mut ledger_path = default_ledger;
    let mut test_session_mode = false;
    let mut verify_ledger_mode = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--ledger" => {
                if i + 1 < args.len() {
                    ledger_path = PathBuf::from(&args[i + 1]);
                    i += 1;
                }
            }
            "--test-session" => {
                test_session_mode = true;
            }
            "--verify-ledger" => {
                if i + 1 < args.len() {
                    verify_ledger_mode = Some(PathBuf::from(&args[i + 1]));
                    i += 1;
                }
            }
            "--help" | "-h" => {
                println!("Usage: mathd-cli [options]");
                println!();
                println!("Options:");
                println!("  --ledger <path>         Path to JSONL mistake ledger (default: mathd-mistakes.jsonl)");
                println!(
                    "  --test-session          Run automated 20-problem session harness and exit"
                );
                println!(
                    "  --verify-ledger <path>  Verify integrity of a JSONL mistake ledger and exit"
                );
                println!("  --help                  Show this help");
                return;
            }
            _ => {}
        }
        i += 1;
    }

    if let Some(path) = verify_ledger_mode {
        match MistakeLedger::open(&path).and_then(|l| {
            l.verify_integrity()
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e.to_string()))
        }) {
            Ok(count) => {
                println!(
                    "OK: Ledger '{}' is valid ({} entries verified, chain unbroken).",
                    path.display(),
                    count
                );
                std::process::exit(0);
            }
            Err(e) => {
                eprintln!(
                    "FAIL: Ledger '{}' failed verification: {}",
                    path.display(),
                    e
                );
                std::process::exit(1);
            }
        }
    }

    if test_session_mode {
        let test_ledger = ledger_path;
        match run_session_harness(&test_ledger) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("Session harness failed: {}", e);
                std::process::exit(1);
            }
        }
    }

    if let Err(e) = run_interactive(&ledger_path) {
        eprintln!("Error in CLI session: {}", e);
        std::process::exit(1);
    }
}
