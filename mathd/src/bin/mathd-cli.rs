//! mathd-cli: Prove-it mode and mistake ledger (work order B7).
//!
//! Terminal-only mathematical verification tool with zero external crates.
//!
//! Evaluates student claims against the B5 verifier:
//! - Identity / algebraic equivalence: verifier::verify_identity
//! - Antiderivatives / integrals: verifier::verify_integral
//! - Equation solutions: verifier::verify_equation_solution
//! - Factorisations: verifier::verify_factorisation
//!
//! Invariant: Never print "Verified" for anything that is not Verdict::Verified.
//! On failure, prints the counterexample and appends to the JSONL mistake ledger.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use mathd::ledger::{CounterexampleRecord, MistakeLedger};
use mathd::parse;
use mathd::verifier::{
    verify_equation_solution, verify_factorisation, verify_identity, verify_integral, Equation,
    Verdict,
};

#[derive(Debug, Clone)]
pub enum Claim {
    Identity {
        lhs_str: String,
        rhs_str: String,
        vars: Vec<String>,
    },
    Integral {
        integrand_str: String,
        antideriv_str: String,
        var: String,
    },
    Equation {
        lhs_str: String,
        rhs_str: String,
        roots: Vec<f64>,
        var: String,
    },
    Factorisation {
        original_str: String,
        factors_str: Vec<String>,
    },
}

impl Claim {
    pub fn claim_string(&self) -> String {
        match self {
            Claim::Identity {
                lhs_str, rhs_str, ..
            } => format!("identity: {} == {}", lhs_str, rhs_str),
            Claim::Integral {
                integrand_str,
                antideriv_str,
                var,
            } => format!("integral ({}): {} -> {}", var, integrand_str, antideriv_str),
            Claim::Equation {
                lhs_str,
                rhs_str,
                roots,
                var,
            } => {
                let roots_str = roots
                    .iter()
                    .map(|r| r.to_string())
                    .collect::<Vec<_>>()
                    .join(", ");
                format!(
                    "equation ({}) : {} == {} [roots: {}]",
                    var, lhs_str, rhs_str, roots_str
                )
            }
            Claim::Factorisation {
                original_str,
                factors_str,
            } => {
                let f_str = factors_str
                    .iter()
                    .map(|f| format!("({})", f))
                    .collect::<Vec<_>>()
                    .join(" * ");
                format!("factorisation: {} == {}", original_str, f_str)
            }
        }
    }
}

pub fn parse_claim_str(raw: &str) -> Result<Claim, String> {
    let s = raw.trim();
    if let Some(rest) = s.strip_prefix("identity:") {
        let (lhs, rhs) = rest
            .split_once("==")
            .or_else(|| rest.split_once('='))
            .ok_or_else(|| "identity requires '==' separating lhs and rhs".to_string())?;
        Ok(Claim::Identity {
            lhs_str: lhs.trim().to_string(),
            rhs_str: rhs.trim().to_string(),
            vars: Vec::new(),
        })
    } else if let Some(rest) = s.strip_prefix("integral:") {
        let (integrand, antideriv) = rest
            .split_once("->")
            .or_else(|| rest.split_once(';'))
            .ok_or_else(|| {
                "integral requires '->' separating integrand and antiderivative".to_string()
            })?;
        Ok(Claim::Integral {
            integrand_str: integrand.trim().to_string(),
            antideriv_str: antideriv.trim().to_string(),
            var: "x".to_string(),
        })
    } else if let Some(rest) = s.strip_prefix("equation:") {
        let (eq_part, roots_part) = rest
            .split_once("[roots:")
            .ok_or_else(|| "equation requires '[roots: r1, r2, ...]' suffix".to_string())?;
        let roots_raw = roots_part.trim_end_matches(']').trim();
        let mut roots = Vec::new();
        for r_str in roots_raw.split(',') {
            let r_str = r_str.trim();
            if !r_str.is_empty() {
                let r: f64 = r_str
                    .parse()
                    .map_err(|e| format!("invalid root float '{}': {}", r_str, e))?;
                roots.push(r);
            }
        }
        let (lhs, rhs) = eq_part
            .split_once("==")
            .or_else(|| eq_part.split_once('='))
            .ok_or_else(|| "equation requires '==' separating lhs and rhs".to_string())?;
        Ok(Claim::Equation {
            lhs_str: lhs.trim().to_string(),
            rhs_str: rhs.trim().to_string(),
            roots,
            var: "x".to_string(),
        })
    } else if let Some(rest) = s.strip_prefix("factorisation:") {
        let (orig, factors_raw) = rest
            .split_once("==")
            .or_else(|| rest.split_once('='))
            .ok_or_else(|| {
                "factorisation requires '==' separating original and factors".to_string()
            })?;
        let mut factors = Vec::new();
        for f in factors_raw.split('*') {
            let f = f
                .trim()
                .trim_start_matches('(')
                .trim_end_matches(')')
                .trim();
            if !f.is_empty() {
                factors.push(f.to_string());
            }
        }
        Ok(Claim::Factorisation {
            original_str: orig.trim().to_string(),
            factors_str: factors,
        })
    } else if s.contains("==") {
        let (lhs, rhs) = s.split_once("==").unwrap();
        Ok(Claim::Identity {
            lhs_str: lhs.trim().to_string(),
            rhs_str: rhs.trim().to_string(),
            vars: Vec::new(),
        })
    } else {
        Err("unknown claim format; prefix with 'identity:', 'integral:', 'equation:', or 'factorisation:'".to_string())
    }
}

pub fn evaluate_claim(claim: &Claim) -> Result<Verdict, String> {
    match claim {
        Claim::Identity {
            lhs_str,
            rhs_str,
            vars,
        } => {
            let lhs_ast = parse(lhs_str).map_err(|e| format!("parse error on LHS: {}", e))?;
            let rhs_ast = parse(rhs_str).map_err(|e| format!("parse error on RHS: {}", e))?;
            let vars_refs: Vec<&str> = vars.iter().map(|s| s.as_str()).collect();
            Ok(verify_identity(&lhs_ast, &rhs_ast, &vars_refs))
        }
        Claim::Integral {
            integrand_str,
            antideriv_str,
            var,
        } => {
            let int_ast =
                parse(integrand_str).map_err(|e| format!("parse error on integrand: {}", e))?;
            let anti_ast = parse(antideriv_str)
                .map_err(|e| format!("parse error on antiderivative: {}", e))?;
            Ok(verify_integral(&int_ast, &anti_ast, var))
        }
        Claim::Equation {
            lhs_str,
            rhs_str,
            roots,
            var,
        } => {
            let lhs_ast = parse(lhs_str).map_err(|e| format!("parse error on LHS: {}", e))?;
            let rhs_ast = parse(rhs_str).map_err(|e| format!("parse error on RHS: {}", e))?;
            let eq = Equation {
                lhs: lhs_ast,
                rhs: rhs_ast,
            };
            Ok(verify_equation_solution(&eq, roots, var))
        }
        Claim::Factorisation {
            original_str,
            factors_str,
        } => {
            let orig_ast =
                parse(original_str).map_err(|e| format!("parse error on original: {}", e))?;
            let mut factor_asts = Vec::with_capacity(factors_str.len());
            for f in factors_str {
                let ast = parse(f).map_err(|e| format!("parse error on factor '{}': {}", f, e))?;
                factor_asts.push(ast);
            }
            Ok(verify_factorisation(&orig_ast, &factor_asts))
        }
    }
}

pub fn print_verdict(verdict: &Verdict) {
    match verdict {
        Verdict::Verified { evidence } => {
            println!("Verdict: Verified");
            println!("Evidence:");
            println!("  points_tested: {}", evidence.points_tested);
            println!("  max_relative_error: {:.10}", evidence.max_relative_error);
        }
        Verdict::Failed {
            counterexample,
            expected,
            got,
        } => {
            println!("Verdict: Failed");
            println!("Evidence:");
            println!("  counterexample:");
            for (k, v) in counterexample {
                println!("    {} = {:.10}", k, v);
            }
            println!("  expected: {:.10}", expected);
            println!("  got: {:.10}", got);
        }
        Verdict::Unverifiable { reason } => {
            println!("Verdict: Unverifiable");
            println!("Reason: {}", reason);
        }
    }
}

pub fn run_claim_and_log(claim: &Claim, ledger: &mut MistakeLedger) -> Result<Verdict, String> {
    let verdict = evaluate_claim(claim)?;
    print_verdict(&verdict);

    if let Verdict::Failed {
        ref counterexample,
        expected,
        got,
    } = verdict
    {
        let mut sorted_pt = BTreeMap::new();
        for (k, v) in counterexample {
            sorted_pt.insert(k.clone(), *v);
        }
        let record = CounterexampleRecord {
            point: sorted_pt,
            expected,
            got,
        };
        let claim_str = claim.claim_string();
        ledger
            .record_failed(&claim_str, &record)
            .map_err(|e| format!("failed to write mistake ledger: {}", e))?;
        println!("Logged failure to mistake ledger.");
    }

    Ok(verdict)
}

// ---------------------------------------------------------------------------
// 20-Problem Session Harness
// ---------------------------------------------------------------------------

pub fn run_session_harness<P: AsRef<Path>, L: AsRef<Path>>(
    fixture_path: P,
    ledger_path: L,
) -> Result<(), String> {
    let fixture_path = fixture_path.as_ref();
    let ledger_path = ledger_path.as_ref();

    if ledger_path.exists() {
        let _ = fs::remove_file(ledger_path);
    }

    let mut ledger = MistakeLedger::open(ledger_path)
        .map_err(|e| format!("failed to open mistake ledger: {}", e))?;

    let fixture_raw = fs::read_to_string(fixture_path)
        .map_err(|e| format!("cannot read fixture '{}': {}", fixture_path.display(), e))?;

    let problems = parse_fixture_json(&fixture_raw)
        .map_err(|e| format!("failed to parse fixture JSON: {}", e))?;

    if problems.len() < 20 {
        return Err(format!(
            "expected at least 20 problems, found {}",
            problems.len()
        ));
    }

    println!("=== Running B7 20-Problem Prove-It Session Harness ===");
    println!("Fixture: {}", fixture_path.display());

    let mut verified_count = 0;
    let mut failed_count = 0;

    for (idx, p) in problems.iter().enumerate() {
        println!(
            "\n--- Problem {}/{}: [{}] {} ---",
            idx + 1,
            problems.len(),
            p.id,
            p.claim_str
        );
        let claim = parse_claim_str(&p.claim_str)
            .map_err(|e| format!("failed to parse claim '{}': {}", p.claim_str, e))?;

        let verdict = run_claim_and_log(&claim, &mut ledger)?;

        match (&verdict, p.expected_verdict.as_str()) {
            (Verdict::Verified { .. }, "Verified") => {
                verified_count += 1;
            }
            (Verdict::Failed { .. }, "Failed") => {
                failed_count += 1;
            }
            _ => {
                return Err(format!(
                    "FATAL: Problem {} [{}] expected verdict '{}', but got '{:?}'",
                    idx + 1,
                    p.id,
                    p.expected_verdict,
                    verdict
                ));
            }
        }
    }

    println!("\n=== Session Execution Summary ===");
    println!("Total problems evaluated: {}", problems.len());
    println!("Verified claims: {}", verified_count);
    println!("Failed claims (deliberate mistakes): {}", failed_count);

    if failed_count < 5 {
        return Err(format!(
            "expected at least 5 failed claims, found {}",
            failed_count
        ));
    }

    println!("\n=== Cryptographic Ledger Integrity Check ===");
    let entries = ledger
        .read_entries()
        .map_err(|e| format!("failed to read ledger entries: {}", e))?;
    if entries.len() != failed_count {
        return Err(format!(
            "ledger entry count ({}) does not match failed claims count ({})",
            entries.len(),
            failed_count
        ));
    }

    let verified_entries = ledger
        .verify_integrity()
        .map_err(|e| format!("ledger integrity verification failed: {}", e))?;
    println!(
        "  OK: Mistake ledger verified: {} total entries, chain unbroken.",
        verified_entries
    );

    println!("\n=== Sabotage Gate: Verifying Tampered Ledger Rejection ===");
    let raw_ledger =
        fs::read_to_string(ledger_path).map_err(|e| format!("failed to read ledger: {}", e))?;
    let tampered = raw_ledger
        .replacen("Failed", "Tampered", 1)
        .replacen("x^2", "x^3", 1);
    fs::write(ledger_path, tampered)
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

    // Clean up test ledger
    let _ = fs::remove_file(ledger_path);

    println!("\n=== B7 Prove-It 20-Problem Session Harness: ALL PASSED ===");
    Ok(())
}

// ---------------------------------------------------------------------------
// Minimal Fixture JSON Parser (zero external crates)
// ---------------------------------------------------------------------------

struct FixtureProblem {
    id: String,
    claim_str: String,
    expected_verdict: String,
}

fn parse_fixture_json(raw: &str) -> Result<Vec<FixtureProblem>, String> {
    let trimmed = raw.trim();
    if !trimmed.starts_with('[') || !trimmed.ends_with(']') {
        return Err("fixture must be a JSON array".to_string());
    }
    let inner = &trimmed[1..trimmed.len() - 1];

    let mut problems = Vec::new();
    let mut depth = 0;
    let mut in_str = false;
    let mut escaped = false;
    let mut obj_start = None;

    for (i, c) in inner.char_indices() {
        if in_str {
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_str = false;
            }
        } else if c == '"' {
            in_str = true;
        } else if c == '{' {
            if depth == 0 {
                obj_start = Some(i);
            }
            depth += 1;
        } else if c == '}' {
            depth -= 1;
            if depth == 0 {
                if let Some(s) = obj_start {
                    let obj_str = &inner[s..=i];
                    let id = extract_field(obj_str, "id")?;
                    let claim_str = extract_field(obj_str, "claim_str")?;
                    let expected_verdict = extract_field(obj_str, "expected_verdict")?;
                    problems.push(FixtureProblem {
                        id,
                        claim_str,
                        expected_verdict,
                    });
                }
                obj_start = None;
            }
        }
    }

    Ok(problems)
}

fn extract_field(obj: &str, field: &str) -> Result<String, String> {
    let target = format!("\"{}\":", field);
    let idx = obj
        .find(&target)
        .ok_or_else(|| format!("field '{}' not found in problem object", field))?;
    let rem = &obj[idx + target.len()..].trim_start();
    if !rem.starts_with('"') {
        return Err(format!("field '{}' is not a string", field));
    }
    let after_quote = &rem[1..];
    let mut escaped = false;
    let mut end_idx = None;
    for (i, c) in after_quote.char_indices() {
        if escaped {
            escaped = false;
        } else if c == '\\' {
            escaped = true;
        } else if c == '"' {
            end_idx = Some(i);
            break;
        }
    }
    let end = end_idx.ok_or_else(|| format!("unterminated string for '{}'", field))?;
    mathd::ledger::unescape_json(&after_quote[..end])
}

// ---------------------------------------------------------------------------
// Interactive Prove-It CLI Mode
// ---------------------------------------------------------------------------

pub fn run_interactive<P: AsRef<Path>>(ledger_path: P) -> io::Result<()> {
    let ledger_path = ledger_path.as_ref();
    let mut ledger = MistakeLedger::open(ledger_path)?;

    println!("========================================================");
    println!("  mathd Prove-it Mode (Terminal Only)");
    println!("  Type a mathematical claim to prove it.");
    println!("  Examples:");
    println!("    identity: (x + 2)^2 == x^2 + 4*x + 4");
    println!("    integral: 3*x^2 + 4*x -> x^3 + 2*x^2");
    println!("    equation: x^2 - 5*x + 6 == 0 [roots: 2.0, 3.0]");
    println!("    factorisation: x^2 - 9 == (x - 3) * (x + 3)");
    println!("  Commands: :help, :ledger, :verify-ledger, :quit");
    println!("========================================================");

    let stdin = io::stdin();
    let mut stdout = io::stdout();

    loop {
        print!("prove-it> ");
        stdout.flush()?;
        let mut line = String::new();
        if stdin.read_line(&mut line)? == 0 {
            break;
        }
        let input = line.trim();
        if input.is_empty() {
            continue;
        }

        match input {
            ":quit" | ":exit" | ":q" => {
                println!("Exiting Prove-it mode.");
                break;
            }
            ":help" => {
                println!("Commands:");
                println!("  :ledger         - View mistake ledger summary");
                println!("  :verify-ledger  - Verify cryptographic ledger integrity");
                println!("  :quit           - Exit");
                println!("Claims:");
                println!("  identity: <lhs> == <rhs>");
                println!("  integral: <integrand> -> <antiderivative>");
                println!("  equation: <lhs> == <rhs> [roots: r1, r2, ...]");
                println!("  factorisation: <original> == <f1> * <f2>");
            }
            ":ledger" => match ledger.read_entries() {
                Ok(entries) => {
                    println!("Mistake ledger contains {} entries:", entries.len());
                    for e in &entries {
                        println!(
                            "  [{}] seq: {}, claim: '{}'",
                            e.sequence, e.timestamp, e.claim
                        );
                    }
                }
                Err(e) => println!("Error reading ledger: {}", e),
            },
            ":verify-ledger" => match ledger.verify_integrity() {
                Ok(count) => println!("OK: Ledger is valid. {} entries verified.", count),
                Err(e) => println!("FAIL: Ledger verification error: {}", e),
            },
            claim_text => match parse_claim_str(claim_text) {
                Ok(claim) => {
                    if let Err(e) = run_claim_and_log(&claim, &mut ledger) {
                        println!("Evaluation error: {}", e);
                    }
                }
                Err(e) => {
                    println!("Invalid claim syntax: {}", e);
                }
            },
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Main Entrypoint
// ---------------------------------------------------------------------------

fn main() {
    let args: Vec<String> = env::args().collect();
    let mut ledger_path = PathBuf::from("mathd-mistakes.jsonl");
    let mut test_session_mode = false;
    let mut session_fixture = PathBuf::from("mathd/fixtures/b7_session.json");
    if !session_fixture.exists() && Path::new("fixtures/b7_session.json").exists() {
        session_fixture = PathBuf::from("fixtures/b7_session.json");
    }
    let mut verify_ledger_mode = None;
    let mut single_claim = None;

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
            "--test-escaping-ledger" => {
                if i + 1 < args.len() {
                    let path = PathBuf::from(&args[i + 1]);
                    let mut ledger = MistakeLedger::open(&path).expect("open ledger");
                    let mut pt = BTreeMap::new();
                    pt.insert("x".to_string(), 2.5);
                    let ce = CounterexampleRecord {
                        point: pt,
                        expected: 7.25,
                        got: 1.0,
                    };
                    let tricky_claim = "identity: \"test\\quote\"\n == x^2 + 1";
                    ledger
                        .record_failed(tricky_claim, &ce)
                        .expect("record failed");
                    println!("Successfully wrote tricky claim to '{}'", path.display());
                    std::process::exit(0);
                }
            }
            "--session" => {
                test_session_mode = true;
                if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                    session_fixture = PathBuf::from(&args[i + 1]);
                    i += 1;
                }
            }
            "--verify-ledger" => {
                if i + 1 < args.len() {
                    verify_ledger_mode = Some(PathBuf::from(&args[i + 1]));
                    i += 1;
                }
            }
            "--claim" => {
                if i + 1 < args.len() {
                    single_claim = Some(args[i + 1].clone());
                    i += 1;
                }
            }
            "--identity" => {
                if i + 1 < args.len() {
                    single_claim = Some(format!("identity: {}", args[i + 1]));
                    i += 1;
                }
            }
            "--help" | "-h" => {
                println!("Usage: mathd-cli [options] [claim]");
                println!();
                println!("Options:");
                println!("  --ledger <path>         Path to JSONL mistake ledger (default: mathd-mistakes.jsonl)");
                println!("  --test-session          Run 20-problem session harness and exit");
                println!("  --session <fixture>     Run session harness using specified fixture and exit");
                println!(
                    "  --verify-ledger <path>  Verify integrity of a JSONL mistake ledger and exit"
                );
                println!("  --claim <claim>         Evaluate a single claim and exit");
                println!("  --help                  Show this help message");
                return;
            }
            other => {
                if !other.starts_with("--") && single_claim.is_none() {
                    single_claim = Some(other.to_string());
                }
            }
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
        match run_session_harness(&session_fixture, &ledger_path) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("Session harness failed: {}", e);
                std::process::exit(1);
            }
        }
    }

    if let Some(claim_raw) = single_claim {
        let mut ledger = match MistakeLedger::open(&ledger_path) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("Failed to open ledger: {}", e);
                std::process::exit(1);
            }
        };
        match parse_claim_str(&claim_raw) {
            Ok(claim) => match run_claim_and_log(&claim, &mut ledger) {
                Ok(Verdict::Verified { .. }) => std::process::exit(0),
                Ok(Verdict::Failed { .. }) => std::process::exit(2),
                Ok(Verdict::Unverifiable { .. }) => std::process::exit(3),
                Err(e) => {
                    eprintln!("Evaluation error: {}", e);
                    std::process::exit(1);
                }
            },
            Err(e) => {
                eprintln!("Claim parsing error: {}", e);
                std::process::exit(1);
            }
        }
    }

    if let Err(e) = run_interactive(&ledger_path) {
        eprintln!("Error in CLI session: {}", e);
        std::process::exit(1);
    }
}
