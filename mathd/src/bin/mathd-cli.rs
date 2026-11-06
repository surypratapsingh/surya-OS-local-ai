//! mathd-cli: Prove-it mode and mistake ledger (work order B7).
//!
//! Terminal-only mathematical verification tool with zero external crates.
//!
//! Prove-it is the default mode (work order B7):
//!   1. Presents problems from the corpus (mathd/corpus/ground_truth.jsonl).
//!   2. Takes the user's answer.
//!   3. Verifies it against the mathd verifier.
//!   4. On failure, shows the counterexample point and values.
//!   5. Provides an explicit reveal command ("show me your working", not "here is the answer").
//!   6. Logs every attempt to the JSONL mistake ledger:
//!      problem_id, answer_given, verdict, timestamp, time_taken_ms.

use std::collections::BTreeMap;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use mathd::ledger::{
    unescape_json, AttemptParams, CounterexampleRecord, LedgerEntry, MistakeLedger,
};
use mathd::parse;
use mathd::verifier::{
    verify_equation_solution, verify_factorisation, verify_identity, verify_integral, Equation,
    Verdict,
};

// ---------------------------------------------------------------------------
// Corpus Problem Data Structures & Parser
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub enum ProblemKind {
    Integral {
        var: String,
        integrand: String,
        claimed_antiderivative: String,
    },
    Identity {
        lhs: String,
        rhs: String,
        vars: Vec<String>,
    },
    Equation {
        var: String,
        lhs: String,
        rhs: String,
        claimed_roots: Vec<f64>,
    },
    Factorisation {
        original: String,
        claimed_factors: Vec<String>,
    },
}

#[derive(Debug, Clone)]
pub struct CorpusProblem {
    pub id: String,
    pub topic: String,
    pub kind: ProblemKind,
    pub sympy_call: Option<String>,
    pub sympy_result: Option<String>,
}

impl CorpusProblem {
    pub fn prompt_text(&self) -> String {
        match &self.kind {
            ProblemKind::Integral { var, integrand, .. } => {
                format!("Integrate with respect to {}:\n  {}", var, integrand)
            }
            ProblemKind::Identity { lhs, .. } => {
                format!("Simplify / find equivalent expression for:\n  {}", lhs)
            }
            ProblemKind::Equation { var, lhs, rhs, .. } => {
                format!("Solve for {}:\n  {} = {}", var, lhs, rhs)
            }
            ProblemKind::Factorisation { original, .. } => {
                format!("Factor into irreducible polynomials:\n  {}", original)
            }
        }
    }

    pub fn ground_truth_answer(&self) -> String {
        match &self.kind {
            ProblemKind::Integral {
                claimed_antiderivative,
                ..
            } => claimed_antiderivative.clone(),
            ProblemKind::Identity { rhs, .. } => rhs.clone(),
            ProblemKind::Equation { claimed_roots, .. } => {
                let r_strs: Vec<String> = claimed_roots.iter().map(|r| r.to_string()).collect();
                r_strs.join(", ")
            }
            ProblemKind::Factorisation {
                claimed_factors, ..
            } => claimed_factors
                .iter()
                .map(|f| format!("({})", f))
                .collect::<Vec<_>>()
                .join(" * "),
        }
    }

    pub fn claim_string_for_answer(&self, answer: &str) -> String {
        match &self.kind {
            ProblemKind::Integral { var, integrand, .. } => {
                format!("integral ({}): {} -> {}", var, integrand, answer)
            }
            ProblemKind::Identity { lhs, .. } => {
                format!("identity: {} == {}", lhs, answer)
            }
            ProblemKind::Equation { var, lhs, rhs, .. } => {
                format!("equation ({}): {} == {} [roots: {}]", var, lhs, rhs, answer)
            }
            ProblemKind::Factorisation { original, .. } => {
                format!("factorisation: {} == {}", original, answer)
            }
        }
    }

    pub fn verify_answer(&self, answer: &str) -> Result<Verdict, String> {
        match &self.kind {
            ProblemKind::Integral { var, integrand, .. } => {
                let cleaned = clean_integral_answer(answer);
                let int_ast =
                    parse(integrand).map_err(|e| format!("integrand parse error: {}", e))?;
                let ans_ast = parse(&cleaned).map_err(|e| format!("answer parse error: {}", e))?;
                Ok(verify_integral(&int_ast, &ans_ast, var))
            }
            ProblemKind::Identity { lhs, vars, .. } => {
                let lhs_ast = parse(lhs).map_err(|e| format!("LHS parse error: {}", e))?;
                let ans_ast = parse(answer).map_err(|e| format!("answer parse error: {}", e))?;
                let var_refs: Vec<&str> = vars.iter().map(|s| s.as_str()).collect();
                Ok(verify_identity(&lhs_ast, &ans_ast, &var_refs))
            }
            ProblemKind::Equation { var, lhs, rhs, .. } => {
                let lhs_ast = parse(lhs).map_err(|e| format!("equation LHS parse error: {}", e))?;
                let rhs_ast = parse(rhs).map_err(|e| format!("equation RHS parse error: {}", e))?;
                let roots = parse_roots_list(answer)?;
                let eq = Equation {
                    lhs: lhs_ast,
                    rhs: rhs_ast,
                };
                Ok(verify_equation_solution(&eq, &roots, var))
            }
            ProblemKind::Factorisation { original, .. } => {
                let orig_ast =
                    parse(original).map_err(|e| format!("original parse error: {}", e))?;
                let factors = parse_factors_list(answer)?;
                let mut factor_asts = Vec::with_capacity(factors.len());
                for f in factors {
                    let ast =
                        parse(&f).map_err(|e| format!("factor '{}' parse error: {}", f, e))?;
                    factor_asts.push(ast);
                }
                Ok(verify_factorisation(&orig_ast, &factor_asts))
            }
        }
    }
}

fn clean_integral_answer(raw: &str) -> String {
    let trimmed = raw.trim();
    for suffix in &["+ C", "+ c", "+C", "+c"] {
        if let Some(prefix) = trimmed.strip_suffix(suffix) {
            return prefix.trim().to_string();
        }
    }
    trimmed.to_string()
}

fn parse_roots_list(raw: &str) -> Result<Vec<f64>, String> {
    let s = raw.trim().trim_start_matches('[').trim_end_matches(']');
    let mut roots = Vec::new();
    for part in s.split(',') {
        let mut p = part.trim();
        if let Some((_, val)) = p.split_once('=') {
            p = val.trim();
        }
        if !p.is_empty() {
            let r: f64 = p
                .parse()
                .map_err(|e| format!("invalid root '{}': {}", p, e))?;
            roots.push(r);
        }
    }
    Ok(roots)
}

fn parse_factors_list(raw: &str) -> Result<Vec<String>, String> {
    let s = raw.trim();
    let mut factors = Vec::new();
    if s.contains('*') {
        for f in s.split('*') {
            let cleaned = f
                .trim()
                .trim_start_matches('(')
                .trim_end_matches(')')
                .trim();
            if !cleaned.is_empty() {
                factors.push(cleaned.to_string());
            }
        }
    } else if s.contains(',') {
        for f in s.split(',') {
            let cleaned = f
                .trim()
                .trim_start_matches('(')
                .trim_end_matches(')')
                .trim();
            if !cleaned.is_empty() {
                factors.push(cleaned.to_string());
            }
        }
    } else if s.starts_with('(') && s.contains(')') {
        let mut cur = String::new();
        let mut inside = false;
        for c in s.chars() {
            if c == '(' {
                inside = true;
                cur.clear();
            } else if c == ')' {
                inside = false;
                let trimmed = cur.trim();
                if !trimmed.is_empty() {
                    factors.push(trimmed.to_string());
                }
                cur.clear();
            } else if inside {
                cur.push(c);
            }
        }
    } else {
        factors.push(s.to_string());
    }
    if factors.is_empty() {
        return Err("no factors provided".to_string());
    }
    Ok(factors)
}

// ---------------------------------------------------------------------------
// Corpus JSON Lines Loader
// ---------------------------------------------------------------------------

pub fn load_corpus<P: AsRef<Path>>(path: P) -> io::Result<Vec<CorpusProblem>> {
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut problems = Vec::new();

    for (idx, line_res) in reader.lines().enumerate() {
        let line = line_res?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        match parse_corpus_line(trimmed) {
            Ok(p) => problems.push(p),
            Err(e) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("corpus line {}: {}", idx + 1, e),
                ));
            }
        }
    }

    Ok(problems)
}

fn parse_corpus_line(line: &str) -> Result<CorpusProblem, String> {
    let id = extract_json_str(line, "id").ok_or_else(|| "missing 'id'".to_string())?;
    let topic = extract_json_str(line, "topic").unwrap_or_else(|| "general".to_string());
    let kind_str = extract_json_str(line, "kind").ok_or_else(|| "missing 'kind'".to_string())?;

    let kind = match kind_str.as_str() {
        "integral" => {
            let var = extract_json_str(line, "var").unwrap_or_else(|| "x".to_string());
            let integrand = extract_json_str(line, "integrand")
                .ok_or_else(|| "missing 'integrand'".to_string())?;
            let claimed_antiderivative = extract_json_str(line, "claimed_antiderivative")
                .ok_or_else(|| "missing 'claimed_antiderivative'".to_string())?;
            ProblemKind::Integral {
                var,
                integrand,
                claimed_antiderivative,
            }
        }
        "identity" => {
            let lhs = extract_json_str(line, "lhs").ok_or_else(|| "missing 'lhs'".to_string())?;
            let rhs = extract_json_str(line, "rhs").ok_or_else(|| "missing 'rhs'".to_string())?;
            let vars =
                extract_json_str_array(line, "vars").unwrap_or_else(|| vec!["x".to_string()]);
            ProblemKind::Identity { lhs, rhs, vars }
        }
        "equation" => {
            let var = extract_json_str(line, "var").unwrap_or_else(|| "x".to_string());
            let lhs = extract_json_str(line, "lhs").ok_or_else(|| "missing 'lhs'".to_string())?;
            let rhs = extract_json_str(line, "rhs").unwrap_or_else(|| "0".to_string());
            let claimed_roots = extract_json_f64_array(line, "claimed_roots")
                .ok_or_else(|| "missing 'claimed_roots'".to_string())?;
            ProblemKind::Equation {
                var,
                lhs,
                rhs,
                claimed_roots,
            }
        }
        "factorisation" => {
            let original = extract_json_str(line, "original")
                .ok_or_else(|| "missing 'original'".to_string())?;
            let claimed_factors = extract_json_str_array(line, "claimed_factors")
                .ok_or_else(|| "missing 'claimed_factors'".to_string())?;
            ProblemKind::Factorisation {
                original,
                claimed_factors,
            }
        }
        other => return Err(format!("unknown problem kind: {}", other)),
    };

    let sympy_call = extract_sympy_field(line, "call");
    let sympy_result = extract_sympy_field(line, "result");

    Ok(CorpusProblem {
        id,
        topic,
        kind,
        sympy_call,
        sympy_result,
    })
}

fn extract_sympy_field(src: &str, field: &str) -> Option<String> {
    let idx = src.find("\"sympy_verification\":")?;
    let rem = &src[idx..];
    extract_json_str(rem, field)
}

fn extract_json_str(src: &str, key: &str) -> Option<String> {
    let target = format!("\"{}\":", key);
    let idx = src.find(&target)?;
    let rem = src[idx + target.len()..].trim_start();
    if !rem.starts_with('"') {
        return None;
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
    let end = end_idx?;
    unescape_json(&after_quote[..end]).ok()
}

fn extract_json_str_array(src: &str, key: &str) -> Option<Vec<String>> {
    let target = format!("\"{}\":", key);
    let idx = src.find(&target)?;
    let rem = src[idx + target.len()..].trim_start();
    if !rem.starts_with('[') {
        return None;
    }
    let end = rem.find(']')?;
    let inner = &rem[1..end];
    let mut out = Vec::new();
    for item in inner.split(',') {
        let trimmed = item.trim().trim_matches('"');
        if !trimmed.is_empty() {
            if let Ok(s) = unescape_json(trimmed) {
                out.push(s);
            }
        }
    }
    Some(out)
}

fn extract_json_f64_array(src: &str, key: &str) -> Option<Vec<f64>> {
    let target = format!("\"{}\":", key);
    let idx = src.find(&target)?;
    let rem = src[idx + target.len()..].trim_start();
    if !rem.starts_with('[') {
        return None;
    }
    let end = rem.find(']')?;
    let inner = &rem[1..end];
    let mut out = Vec::new();
    for item in inner.split(',') {
        let trimmed = item.trim();
        if !trimmed.is_empty() {
            out.push(trimmed.parse::<f64>().ok()?);
        }
    }
    Some(out)
}

// ---------------------------------------------------------------------------
// File-backed Mistake Ledger Helpers
// ---------------------------------------------------------------------------

pub fn load_ledger<P: AsRef<Path>>(path: P) -> io::Result<MistakeLedger> {
    let path = path.as_ref();
    if !path.exists() {
        return Ok(MistakeLedger::new());
    }
    let content = fs::read_to_string(path)?;
    content
        .parse::<MistakeLedger>()
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("ledger error: {}", e)))
}

pub fn append_ledger_entry<P: AsRef<Path>>(path: P, entry: &LedgerEntry) -> io::Result<()> {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() && !parent.exists() {
            fs::create_dir_all(parent)?;
        }
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    writeln!(file, "{}", entry.to_json_line())?;
    file.flush()
}

// ---------------------------------------------------------------------------
// Printing & Reporting
// ---------------------------------------------------------------------------

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
        Verdict::FailedDomain {
            counterexample,
            undefined,
            defined_value,
        } => {
            println!("Verdict: Failed (Domain Mismatch)");
            println!("Evidence:");
            println!("  counterexample:");
            for (k, v) in counterexample {
                println!("    {} = {:.10}", k, v);
            }
            let side = match undefined {
                mathd::verifier::Side::Lhs => "lhs",
                mathd::verifier::Side::Rhs => "rhs",
            };
            println!("  undefined: {}", side);
            println!("  defined_value: {:.10}", defined_value);
        }
        Verdict::Unverifiable { reason } => {
            println!("Verdict: Unverifiable");
            println!("Reason: {}", reason);
        }
    }
}

pub fn verdict_to_str(verdict: &Verdict) -> &'static str {
    match verdict {
        Verdict::Verified { .. } => "Verified",
        Verdict::Failed { .. } => "Failed",
        Verdict::FailedDomain { .. } => "FailedDomain",
        Verdict::Unverifiable { .. } => "Unverifiable",
    }
}

// ---------------------------------------------------------------------------
// Explicit Reveal Command (Finding B7-L2)
// ---------------------------------------------------------------------------

pub fn handle_reveal<P: AsRef<Path>>(corpus_path: P, problem_id: &str) -> Result<(), String> {
    let problems = load_corpus(corpus_path).map_err(|e| format!("cannot load corpus: {}", e))?;

    let p = problems
        .iter()
        .find(|p| p.id == problem_id)
        .ok_or_else(|| format!("problem ID '{}' not found in corpus", problem_id))?;

    println!("================================================================");
    println!("  Problem: {} (topic: {})", p.id, p.topic);
    println!("  Statement: {}", p.prompt_text());
    println!("  Ground truth answer: {}", p.ground_truth_answer());
    if let (Some(call), Some(res)) = (&p.sympy_call, &p.sympy_result) {
        println!("  SymPy oracle: {} => {}", call, res);
    }
    println!("================================================================");
    Ok(())
}

// ---------------------------------------------------------------------------
// Interactive Prove-It CLI Mode (Finding B7-L1)
// ---------------------------------------------------------------------------

pub fn run_interactive<P: AsRef<Path>, L: AsRef<Path>>(
    corpus_path: P,
    ledger_path: L,
    start_problem_id: Option<&str>,
) -> io::Result<()> {
    let corpus_path = corpus_path.as_ref();
    let ledger_path = ledger_path.as_ref();

    let problems = load_corpus(corpus_path).map_err(|e| {
        io::Error::new(
            io::ErrorKind::NotFound,
            format!(
                "failed to load corpus from '{}': {}",
                corpus_path.display(),
                e
            ),
        )
    })?;

    if problems.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "corpus contains zero problems",
        ));
    }

    let mut ledger = load_ledger(ledger_path)?;

    println!("==================================================================");
    println!("  mathd Prove-it Mode (Terminal Only)");
    println!(
        "  Loaded {} problems from corpus '{}'",
        problems.len(),
        corpus_path.display()
    );
    println!(
        "  Mistake ledger: '{}' (current entries: {})",
        ledger_path.display(),
        ledger.last_sequence()
    );
    println!("  Type your mathematical answer to prove it.");
    println!("  Commands:");
    println!("    reveal    - Reveal ground truth answer for current problem");
    println!("    skip      - Skip current problem without logging");
    println!("    ledger    - Display mistake ledger summary");
    println!("    verify    - Verify cryptographic hash chain integrity");
    println!("    quit      - Exit Prove-it");
    println!("==================================================================");

    let start_idx = if let Some(id) = start_problem_id {
        problems.iter().position(|p| p.id == id).unwrap_or(0)
    } else {
        0
    };

    let stdin = io::stdin();
    let mut stdout = io::stdout();

    let mut idx = start_idx;
    while idx < problems.len() {
        let p = &problems[idx];
        println!("\n------------------------------------------------------------------");
        println!(
            "Problem {}/{}: [{}] ({})",
            idx + 1,
            problems.len(),
            p.id,
            p.topic
        );
        println!("{}", p.prompt_text());
        println!("------------------------------------------------------------------");

        let start_time = Instant::now();
        print!("answer> ");
        stdout.flush()?;

        let mut line = String::new();
        if stdin.read_line(&mut line)? == 0 {
            println!("\nEOF received. Exiting Prove-it.");
            break;
        }

        let time_taken_ms = start_time.elapsed().as_millis() as u64;
        let input = line.trim();
        if input.is_empty() {
            continue;
        }

        match input {
            "quit" | ":quit" | "exit" | ":exit" | ":q" => {
                println!("Exiting Prove-it session.");
                break;
            }
            "reveal" | ":reveal" => {
                println!(
                    "[REVEALED] Ground truth answer: {}",
                    p.ground_truth_answer()
                );
                if let (Some(call), Some(res)) = (&p.sympy_call, &p.sympy_result) {
                    println!("           SymPy oracle: {} => {}", call, res);
                }
                // Log reveal attempt to the ledger
                let ts = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let claim_desc = p.claim_string_for_answer(&p.ground_truth_answer());
                let entry = ledger.record_attempt(AttemptParams {
                    problem_id: &p.id,
                    claim: &claim_desc,
                    answer_given: "reveal",
                    verdict: "Revealed",
                    time_taken_ms,
                    counterexample: None,
                    timestamp: ts,
                });
                append_ledger_entry(ledger_path, &entry)?;
                println!("Logged reveal action to mistake ledger.");
                idx += 1;
            }
            "skip" | ":skip" | "next" | ":next" => {
                println!("Skipped problem [{}].", p.id);
                idx += 1;
            }
            "ledger" | ":ledger" => {
                println!(
                    "Mistake ledger summary ({} entries total):",
                    ledger.entries().len()
                );
                for e in ledger.entries().iter().rev().take(10) {
                    println!(
                        "  #{} [{}] id:{} verdict:{} ({}ms)",
                        e.sequence, e.timestamp, e.problem_id, e.verdict, e.time_taken_ms
                    );
                }
            }
            "verify" | ":verify" => match ledger.verify_integrity() {
                Ok(n) => println!(
                    "OK: Cryptographic ledger verified: {} entries, chain unbroken.",
                    n
                ),
                Err(e) => println!("FAIL: Ledger verification error: {}", e),
            },
            ans => match p.verify_answer(ans) {
                Ok(verdict) => {
                    print_verdict(&verdict);
                    let v_str = verdict_to_str(&verdict);
                    let ce_record = match &verdict {
                        Verdict::Failed {
                            counterexample,
                            expected,
                            got,
                        } => {
                            let mut sorted = BTreeMap::new();
                            for (k, v) in counterexample {
                                sorted.insert(k.clone(), *v);
                            }
                            Some(CounterexampleRecord {
                                point: sorted,
                                expected: *expected,
                                got: *got,
                            })
                        }
                        Verdict::FailedDomain {
                            counterexample,
                            defined_value,
                            ..
                        } => {
                            let mut sorted = BTreeMap::new();
                            for (k, v) in counterexample {
                                sorted.insert(k.clone(), *v);
                            }
                            Some(CounterexampleRecord {
                                point: sorted,
                                expected: 0.0,
                                got: *defined_value,
                            })
                        }
                        _ => None,
                    };

                    let ts = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_secs())
                        .unwrap_or(0);
                    let claim_desc = p.claim_string_for_answer(ans);
                    let entry = ledger.record_attempt(AttemptParams {
                        problem_id: &p.id,
                        claim: &claim_desc,
                        answer_given: ans,
                        verdict: v_str,
                        time_taken_ms,
                        counterexample: ce_record.as_ref(),
                        timestamp: ts,
                    });
                    append_ledger_entry(ledger_path, &entry)?;
                    println!(
                        "Logged attempt #{} to ledger (verdict: {}).",
                        entry.sequence, v_str
                    );

                    if matches!(verdict, Verdict::Verified { .. }) {
                        println!("Correct! Advancing to next problem.");
                        idx += 1;
                    } else {
                        println!("You can try again, or type 'reveal' to see working, or 'skip' to advance.");
                    }
                }
                Err(e) => {
                    println!("Answer parse error: {}", e);
                    println!("Please check your syntax and try again.");
                }
            },
        }
    }

    println!(
        "\nProve-it session complete. Ledger written to '{}'.",
        ledger_path.display()
    );
    Ok(())
}

// ---------------------------------------------------------------------------
// 20-Problem Session Harness (from Fixture JSON)
// ---------------------------------------------------------------------------

struct FixtureProblem {
    id: String,
    claim_type: String,
    claim_str: String,
    expected_verdict: String,
    lhs: Option<String>,
    rhs: Option<String>,
    vars: Vec<String>,
    integrand: Option<String>,
    antiderivative: Option<String>,
    var: Option<String>,
    roots: Vec<f64>,
    original: Option<String>,
    factors: Vec<String>,
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
                    let id = extract_json_str(obj_str, "id").ok_or("missing id")?;
                    let claim_type = extract_json_str(obj_str, "claim_type")
                        .unwrap_or_else(|| "identity".to_string());
                    let claim_str = extract_json_str(obj_str, "claim_str").unwrap_or_default();
                    let expected_verdict = extract_json_str(obj_str, "expected_verdict")
                        .unwrap_or_else(|| "Verified".to_string());
                    let lhs = extract_json_str(obj_str, "lhs");
                    let rhs = extract_json_str(obj_str, "rhs");
                    let vars = extract_json_str_array(obj_str, "vars").unwrap_or_default();
                    let integrand = extract_json_str(obj_str, "integrand");
                    let antiderivative = extract_json_str(obj_str, "antiderivative");
                    let var = extract_json_str(obj_str, "var");
                    let roots = extract_json_f64_array(obj_str, "roots").unwrap_or_default();
                    let original = extract_json_str(obj_str, "original");
                    let factors = extract_json_str_array(obj_str, "factors").unwrap_or_default();

                    problems.push(FixtureProblem {
                        id,
                        claim_type,
                        claim_str,
                        expected_verdict,
                        lhs,
                        rhs,
                        vars,
                        integrand,
                        antiderivative,
                        var,
                        roots,
                        original,
                        factors,
                    });
                }
                obj_start = None;
            }
        }
    }

    Ok(problems)
}

pub fn run_session_harness<P: AsRef<Path>, L: AsRef<Path>>(
    fixture_path: P,
    ledger_path: L,
) -> Result<(), String> {
    let fixture_path = fixture_path.as_ref();
    let ledger_path = ledger_path.as_ref();

    if ledger_path.exists() {
        let _ = fs::remove_file(ledger_path);
    }

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
    println!("Ledger output: {}", ledger_path.display());

    let mut ledger = MistakeLedger::new();
    let mut verified_count = 0;
    let mut failed_count = 0;

    for (idx, p) in problems.iter().enumerate() {
        println!(
            "\n--- Problem {}/{}: [{}] ({}) ---",
            idx + 1,
            problems.len(),
            p.id,
            p.claim_type
        );

        let (verdict, answer_given) = match p.claim_type.as_str() {
            "identity" => {
                let lhs_str = p.lhs.as_deref().unwrap();
                let rhs_str = p.rhs.as_deref().unwrap();
                let lhs = parse(lhs_str).map_err(|e| format!("lhs parse: {}", e))?;
                let rhs = parse(rhs_str).map_err(|e| format!("rhs parse: {}", e))?;
                let var_refs: Vec<&str> = p.vars.iter().map(|s| s.as_str()).collect();
                (verify_identity(&lhs, &rhs, &var_refs), rhs_str.to_string())
            }
            "integral" => {
                let int_str = p.integrand.as_deref().unwrap();
                let anti_str = p.antiderivative.as_deref().unwrap();
                let var = p.var.as_deref().unwrap_or("x");
                let integrand = parse(int_str).map_err(|e| format!("int parse: {}", e))?;
                let antideriv = parse(anti_str).map_err(|e| format!("anti parse: {}", e))?;
                (
                    verify_integral(&integrand, &antideriv, var),
                    anti_str.to_string(),
                )
            }
            "equation" => {
                let lhs_str = p.lhs.as_deref().unwrap();
                let rhs_str = p.rhs.as_deref().unwrap();
                let var = p.var.as_deref().unwrap_or("x");
                let lhs = parse(lhs_str).map_err(|e| format!("lhs parse: {}", e))?;
                let rhs = parse(rhs_str).map_err(|e| format!("rhs parse: {}", e))?;
                let eq = Equation { lhs, rhs };
                let ans_str = p
                    .roots
                    .iter()
                    .map(|r| r.to_string())
                    .collect::<Vec<_>>()
                    .join(", ");
                (verify_equation_solution(&eq, &p.roots, var), ans_str)
            }
            "factorisation" => {
                let orig_str = p.original.as_deref().unwrap();
                let orig = parse(orig_str).map_err(|e| format!("orig parse: {}", e))?;
                let mut factor_asts = Vec::new();
                for f_str in &p.factors {
                    factor_asts.push(parse(f_str).map_err(|e| format!("factor parse: {}", e))?);
                }
                (
                    verify_factorisation(&orig, &factor_asts),
                    p.factors.join(" * "),
                )
            }
            other => return Err(format!("unknown claim_type '{}'", other)),
        };

        print_verdict(&verdict);
        let v_str = verdict_to_str(&verdict);

        if v_str != p.expected_verdict {
            return Err(format!(
                "FATAL: Problem {} [{}] expected verdict '{}', got '{}'",
                idx + 1,
                p.id,
                p.expected_verdict,
                v_str
            ));
        }

        let ce_record = match &verdict {
            Verdict::Failed {
                counterexample,
                expected,
                got,
            } => {
                let mut sorted = BTreeMap::new();
                for (k, v) in counterexample {
                    sorted.insert(k.clone(), *v);
                }
                Some(CounterexampleRecord {
                    point: sorted,
                    expected: *expected,
                    got: *got,
                })
            }
            Verdict::FailedDomain {
                counterexample,
                defined_value,
                ..
            } => {
                let mut sorted = BTreeMap::new();
                for (k, v) in counterexample {
                    sorted.insert(k.clone(), *v);
                }
                Some(CounterexampleRecord {
                    point: sorted,
                    expected: 0.0,
                    got: *defined_value,
                })
            }
            _ => None,
        };

        if v_str == "Verified" {
            verified_count += 1;
        } else if v_str == "Failed" || v_str == "FailedDomain" {
            failed_count += 1;
        }

        let ts = 1730000000 + (idx as u64) * 10;
        let time_taken_ms = 100 + (idx as u64) * 15;
        let entry = ledger.record_attempt(AttemptParams {
            problem_id: &p.id,
            claim: &p.claim_str,
            answer_given: &answer_given,
            verdict: v_str,
            time_taken_ms,
            counterexample: ce_record.as_ref(),
            timestamp: ts,
        });
        append_ledger_entry(ledger_path, &entry)
            .map_err(|e| format!("failed to append entry to ledger: {}", e))?;
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
    let disk_ledger = load_ledger(ledger_path).map_err(|e| format!("cannot load ledger: {}", e))?;
    let verified_entries = disk_ledger
        .verify_integrity()
        .map_err(|e| format!("ledger verification failed: {}", e))?;

    println!(
        "  OK: Mistake ledger verified: {} total entries, chain unbroken.",
        verified_entries
    );
    assert_eq!(verified_entries, problems.len());

    println!("\n=== Sabotage Gate: Verifying Tampered Ledger Rejection ===");
    let raw_ledger =
        fs::read_to_string(ledger_path).map_err(|e| format!("failed to read ledger: {}", e))?;
    let tampered = raw_ledger.replacen("Verified", "Tampered", 1);
    let tampered_path = ledger_path.with_extension("tampered.jsonl");
    fs::write(&tampered_path, tampered)
        .map_err(|e| format!("failed to write tampered ledger: {}", e))?;

    match load_ledger(&tampered_path) {
        Ok(_) => return Err("FATAL: Sabotage gate failed: tampered ledger was accepted!".into()),
        Err(e) => {
            println!(
                "  SABOTAGE GATE PASS: Tampered ledger correctly refused with: {}",
                e
            );
        }
    }
    let _ = fs::remove_file(&tampered_path);

    println!("\n=== B7 Prove-It 20-Problem Session Harness: ALL PASSED ===");
    Ok(())
}

// ---------------------------------------------------------------------------
// Finding Corpus / Session Paths
// ---------------------------------------------------------------------------

fn find_corpus_path(user_opt: Option<&Path>) -> Option<PathBuf> {
    if let Some(p) = user_opt {
        if p.exists() {
            return Some(p.to_path_buf());
        }
    }
    let candidates = [
        PathBuf::from("mathd/corpus/ground_truth.jsonl"),
        PathBuf::from("corpus/ground_truth.jsonl"),
        Path::new(env!("CARGO_MANIFEST_DIR")).join("corpus/ground_truth.jsonl"),
    ];
    candidates.into_iter().find(|c| c.exists())
}

fn find_session_fixture(user_opt: Option<&Path>) -> Option<PathBuf> {
    if let Some(p) = user_opt {
        if p.exists() {
            return Some(p.to_path_buf());
        }
    }
    let candidates = [
        PathBuf::from("mathd/fixtures/b7_session.json"),
        PathBuf::from("fixtures/b7_session.json"),
        Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/b7_session.json"),
    ];
    candidates.into_iter().find(|c| c.exists())
}

// ---------------------------------------------------------------------------
// Main Entrypoint
// ---------------------------------------------------------------------------

fn main() {
    let args: Vec<String> = env::args().collect();
    let mut ledger_path = PathBuf::from("mathd-mistakes.jsonl");
    let mut user_corpus_path = None;
    let mut user_session_fixture = None;
    let mut test_session_mode = false;
    let mut reveal_problem_id = None;
    let mut verify_ledger_path = None;
    let mut single_claim = None;
    let mut start_problem_id = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--ledger" => {
                if i + 1 < args.len() {
                    ledger_path = PathBuf::from(&args[i + 1]);
                    i += 1;
                }
            }
            "--corpus" => {
                if i + 1 < args.len() {
                    user_corpus_path = Some(PathBuf::from(&args[i + 1]));
                    i += 1;
                }
            }
            "--reveal" => {
                if i + 1 < args.len() {
                    reveal_problem_id = Some(args[i + 1].clone());
                    i += 1;
                }
            }
            "--problem" => {
                if i + 1 < args.len() {
                    start_problem_id = Some(args[i + 1].clone());
                    i += 1;
                }
            }
            "--verify-ledger" => {
                if i + 1 < args.len() {
                    verify_ledger_path = Some(PathBuf::from(&args[i + 1]));
                    i += 1;
                }
            }
            "--session" => {
                test_session_mode = true;
                if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                    user_session_fixture = Some(PathBuf::from(&args[i + 1]));
                    i += 1;
                }
            }
            "--test-session" => {
                test_session_mode = true;
            }
            "--test-escaping-ledger" => {
                if i + 1 < args.len() {
                    let path = PathBuf::from(&args[i + 1]);
                    let mut ledger = MistakeLedger::new();
                    let mut pt = BTreeMap::new();
                    pt.insert("x".to_string(), 2.5);
                    let ce = CounterexampleRecord {
                        point: pt,
                        expected: 7.25,
                        got: 1.0,
                    };
                    let tricky_claim = "identity: \"test\\quote\"\n == x^2 + 1";
                    let entry = ledger.record_attempt(AttemptParams {
                        problem_id: "tricky-001",
                        claim: tricky_claim,
                        answer_given: "\"test\\quote\"\n",
                        verdict: "Failed",
                        time_taken_ms: 45,
                        counterexample: Some(&ce),
                        timestamp: 1700000000,
                    });
                    append_ledger_entry(&path, &entry).expect("append entry");
                    println!("Successfully wrote tricky claim to '{}'", path.display());
                    std::process::exit(0);
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
            "--prove-it" => {
                // Explicit flag for default mode
            }
            "--help" | "-h" => {
                println!("Usage: mathd-cli [options]");
                println!();
                println!("Prove-it Mode (default):");
                println!(
                    "  Presents mathematical problems from corpus and verifies student answers."
                );
                println!();
                println!("Options:");
                println!("  --corpus <path>         Path to ground truth corpus (default: mathd/corpus/ground_truth.jsonl)");
                println!("  --reveal <id>           Explicit command to reveal the ground truth answer for problem <id>");
                println!("  --ledger <path>         Path to JSONL mistake ledger (default: mathd-mistakes.jsonl)");
                println!("  --verify-ledger <path>  Verify cryptographic integrity and hash chain of a ledger file");
                println!("  --session [fixture]     Run 20-problem session harness from fixture and exit");
                println!("  --test-session          Run 20-problem session harness and exit");
                println!("  --problem <id>          Start Prove-it at problem <id>");
                println!("  --claim <claim>         Evaluate a single claim string and exit");
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

    // 1. Reveal command (Finding B7-L2)
    if let Some(ref problem_id) = reveal_problem_id {
        let corpus_path = find_corpus_path(user_corpus_path.as_deref()).unwrap_or_else(|| {
            eprintln!("Error: corpus file not found. Use --corpus <path> to specify.");
            std::process::exit(1);
        });
        match handle_reveal(&corpus_path, problem_id) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("Error: {}", e);
                std::process::exit(1);
            }
        }
    }

    // 2. Verify ledger command
    if let Some(ref path) = verify_ledger_path {
        match load_ledger(path).and_then(|l| {
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

    // 3. Test session mode
    if test_session_mode {
        let fixture_path =
            find_session_fixture(user_session_fixture.as_deref()).unwrap_or_else(|| {
                eprintln!(
                    "Error: session fixture file not found. Use --session <fixture> to specify."
                );
                std::process::exit(1);
            });
        match run_session_harness(&fixture_path, &ledger_path) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("Session harness failed: {}", e);
                std::process::exit(1);
            }
        }
    }

    // 4. Single claim mode
    if let Some(ref claim_raw) = single_claim {
        let s = claim_raw.trim();
        let (lhs_str, rhs_str) = if let Some(rest) = s.strip_prefix("identity:") {
            rest.split_once("==")
                .or_else(|| rest.split_once('='))
                .unwrap_or((rest, ""))
        } else if s.contains("==") {
            s.split_once("==").unwrap()
        } else {
            eprintln!("Claim parsing error: format must be 'identity: <lhs> == <rhs>'");
            std::process::exit(1);
        };
        let lhs = parse(lhs_str.trim()).unwrap_or_else(|e| {
            eprintln!("LHS parse error: {}", e);
            std::process::exit(1);
        });
        let rhs = parse(rhs_str.trim()).unwrap_or_else(|e| {
            eprintln!("RHS parse error: {}", e);
            std::process::exit(1);
        });
        let verdict = verify_identity(&lhs, &rhs, &[]);
        print_verdict(&verdict);
        match verdict {
            Verdict::Verified { .. } => std::process::exit(0),
            Verdict::Failed { .. } | Verdict::FailedDomain { .. } => std::process::exit(2),
            Verdict::Unverifiable { .. } => std::process::exit(3),
        }
    }

    // 5. Default: Interactive Prove-it mode (Finding B7-L1)
    let corpus_path = find_corpus_path(user_corpus_path.as_deref()).unwrap_or_else(|| {
        eprintln!("Error: corpus file 'mathd/corpus/ground_truth.jsonl' not found. Specify with --corpus <path>.");
        std::process::exit(1);
    });

    if let Err(e) = run_interactive(&corpus_path, &ledger_path, start_problem_id.as_deref()) {
        eprintln!("Error in Prove-it CLI: {}", e);
        std::process::exit(1);
    }
}
