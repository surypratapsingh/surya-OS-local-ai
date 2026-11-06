//! Integration tests for B7 mistake ledger and Prove-it verification (work order B7).

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use mathd::ledger::{
    escape_json, unescape_json, AttemptParams, CounterexampleRecord, LedgerVerifyError,
    MistakeLedger, GENESIS_PREV_HASH,
};
use mathd::parse;
use mathd::verifier::{
    verify_equation_solution, verify_factorisation, verify_identity, verify_integral, Equation,
    Verdict,
};

fn fixture_path() -> PathBuf {
    let candidates = [
        PathBuf::from("mathd/fixtures/b7_session.json"),
        PathBuf::from("fixtures/b7_session.json"),
        Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/b7_session.json"),
    ];
    for p in candidates {
        if p.exists() {
            return p;
        }
    }
    PathBuf::from("fixtures/b7_session.json")
}

fn corpus_path() -> PathBuf {
    let candidates = [
        PathBuf::from("mathd/corpus/ground_truth.jsonl"),
        PathBuf::from("corpus/ground_truth.jsonl"),
        Path::new(env!("CARGO_MANIFEST_DIR")).join("corpus/ground_truth.jsonl"),
    ];
    for p in candidates {
        if p.exists() {
            return p;
        }
    }
    PathBuf::from("corpus/ground_truth.jsonl")
}

#[test]
fn test_ledger_json_escaping_roundtrip() {
    let tricky = "claim: \"nested \\\"quotes\\\"\" and \\backslash\\ and \nnewline and \ttab";
    let escaped = escape_json(tricky);
    assert!(escaped.contains("\\\""));
    assert!(escaped.contains("\\\\"));
    assert!(escaped.contains("\\n"));
    assert!(escaped.contains("\\t"));

    let unescaped = unescape_json(&escaped).expect("valid unescaping");
    assert_eq!(unescaped, tricky);
}

#[test]
fn test_ledger_append_and_verify_chain() {
    let temp_dir = PathBuf::from("target/mathd_test_ledger_chain");
    let ledger_path = temp_dir.join("test_mistakes.jsonl");
    if ledger_path.exists() {
        let _ = fs::remove_file(&ledger_path);
    }
    let _ = fs::remove_dir_all(&temp_dir);
    fs::create_dir_all(&temp_dir).unwrap();

    let mut ledger = MistakeLedger::new();
    assert_eq!(ledger.verify_integrity().unwrap(), 0);

    let mut pt1 = BTreeMap::new();
    pt1.insert("x".to_string(), 1.5);
    let ce1 = CounterexampleRecord {
        point: pt1,
        expected: 6.25,
        got: 3.25,
    };
    let e1 = ledger.record_attempt(AttemptParams {
        problem_id: "problem-001",
        claim: "identity: (x + 1)^2 == x^2 + 1",
        answer_given: "x^2 + 1",
        verdict: "Failed",
        time_taken_ms: 150,
        counterexample: Some(&ce1),
        timestamp: 1700000001,
    });
    assert_eq!(e1.sequence, 1);
    assert_eq!(e1.prev_hash, GENESIS_PREV_HASH);
    assert_eq!(e1.problem_id, "problem-001");
    assert_eq!(e1.verdict, "Failed");
    assert_eq!(e1.time_taken_ms, 150);

    let e2 = ledger.record_attempt(AttemptParams {
        problem_id: "problem-002",
        claim: "identity: cos(x)^2 + sin(x)^2 == 1",
        answer_given: "1",
        verdict: "Verified",
        time_taken_ms: 80,
        counterexample: None,
        timestamp: 1700000005,
    });
    assert_eq!(e2.sequence, 2);
    assert_eq!(e2.prev_hash, e1.hash);
    assert_eq!(e2.problem_id, "problem-002");
    assert_eq!(e2.verdict, "Verified");
    assert_eq!(e2.time_taken_ms, 80);

    assert_eq!(ledger.verify_integrity().unwrap(), 2);

    // Write to disk and re-parse
    fs::write(&ledger_path, ledger.to_json_lines()).unwrap();

    let disk_raw = fs::read_to_string(&ledger_path).unwrap();
    let reloaded: MistakeLedger = disk_raw.parse().unwrap();
    assert_eq!(reloaded.last_sequence(), 2);
    assert_eq!(reloaded.entries().len(), 2);
    assert_eq!(reloaded.entries()[0].problem_id, "problem-001");
    assert_eq!(reloaded.entries()[1].problem_id, "problem-002");
    assert_eq!(reloaded.verify_integrity().unwrap(), 2);

    // Sabotage gate: tamper one byte in the ledger file
    {
        let tampered = disk_raw.replacen("x^2 + 1", "x^2 + 2", 1);
        let res: Result<MistakeLedger, _> = tampered.parse();
        assert!(matches!(
            res,
            Err(LedgerVerifyError::HashMismatch { line: 1, .. })
        ));
    }

    // Sabotage gate: tamper sequence
    {
        let tampered_seq = disk_raw.replacen("\"sequence\":2", "\"sequence\":3", 1);
        let res: Result<MistakeLedger, _> = tampered_seq.parse();
        assert!(matches!(
            res,
            Err(LedgerVerifyError::InvalidSequence { line: 2, .. })
        ));
    }

    // Sabotage gate: tamper prev_hash
    {
        let target = format!("\"prev_hash\":\"{}\"", e1.hash);
        let replacement = format!("\"prev_hash\":\"{}\"", GENESIS_PREV_HASH);
        let tampered_prev = disk_raw.replacen(&target, &replacement, 1);
        let res: Result<MistakeLedger, _> = tampered_prev.parse();
        assert!(matches!(
            res,
            Err(LedgerVerifyError::ChainBroken { line: 2, .. })
        ));
    }

    let _ = fs::remove_file(&ledger_path);
    let _ = fs::remove_dir_all(&temp_dir);
}

#[test]
fn test_session_fixture_20_problems_and_mistake_ledger() {
    let fix_path = fixture_path();
    assert!(fix_path.exists(), "fixture file {:?} must exist", fix_path);

    let raw = fs::read_to_string(&fix_path).unwrap();

    let temp_dir = PathBuf::from("target/mathd_test_session_harness");
    let ledger_path = temp_dir.join("mistakes.jsonl");
    if ledger_path.exists() {
        let _ = fs::remove_file(&ledger_path);
    }
    let _ = fs::remove_dir_all(&temp_dir);
    fs::create_dir_all(&temp_dir).unwrap();

    let mut ledger = MistakeLedger::new();

    let mut verified_count = 0;
    let mut failed_count = 0;

    let inner = raw.trim().trim_start_matches('[').trim_end_matches(']');
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
                    let id = extract_json_str(obj_str, "id").unwrap();
                    let ctype = extract_json_str(obj_str, "claim_type").unwrap();
                    let claim_str = extract_json_str(obj_str, "claim_str").unwrap_or_default();
                    let expected = extract_json_str(obj_str, "expected_verdict").unwrap();

                    let (verdict, ans_given) = match ctype.as_str() {
                        "identity" => {
                            let lhs_str = extract_json_str(obj_str, "lhs").unwrap();
                            let rhs_str = extract_json_str(obj_str, "rhs").unwrap();
                            let lhs = parse(&lhs_str).unwrap();
                            let rhs = parse(&rhs_str).unwrap();
                            (verify_identity(&lhs, &rhs, &[]), rhs_str)
                        }
                        "integral" => {
                            let int_str = extract_json_str(obj_str, "integrand").unwrap();
                            let anti_str = extract_json_str(obj_str, "antiderivative").unwrap();
                            let var = extract_json_str(obj_str, "var").unwrap();
                            let integrand = parse(&int_str).unwrap();
                            let antideriv = parse(&anti_str).unwrap();
                            (verify_integral(&integrand, &antideriv, &var), anti_str)
                        }
                        "equation" => {
                            let lhs_str = extract_json_str(obj_str, "lhs").unwrap();
                            let rhs_str = extract_json_str(obj_str, "rhs").unwrap();
                            let var = extract_json_str(obj_str, "var").unwrap();
                            let roots = extract_json_f64_array(obj_str, "roots").unwrap();
                            let lhs = parse(&lhs_str).unwrap();
                            let rhs = parse(&rhs_str).unwrap();
                            let eq = Equation { lhs, rhs };
                            let r_str = roots
                                .iter()
                                .map(|r| r.to_string())
                                .collect::<Vec<_>>()
                                .join(", ");
                            (verify_equation_solution(&eq, &roots, &var), r_str)
                        }
                        "factorisation" => {
                            let orig_str = extract_json_str(obj_str, "original").unwrap();
                            let factors_strs = extract_json_str_array(obj_str, "factors").unwrap();
                            let orig = parse(&orig_str).unwrap();
                            let mut factors = Vec::new();
                            for f_str in &factors_strs {
                                factors.push(parse(f_str).unwrap());
                            }
                            (
                                verify_factorisation(&orig, &factors),
                                factors_strs.join(" * "),
                            )
                        }
                        other => panic!("unknown claim_type: {}", other),
                    };

                    let ts = 1730000000 + (ledger.last_sequence()) * 10;
                    let time_ms = 100 + (ledger.last_sequence()) * 20;

                    match (verdict, expected.as_str()) {
                        (Verdict::Verified { evidence }, "Verified") => {
                            assert!(evidence.points_tested >= 1);
                            verified_count += 1;
                            ledger.record_attempt(AttemptParams {
                                problem_id: &id,
                                claim: &claim_str,
                                answer_given: &ans_given,
                                verdict: "Verified",
                                time_taken_ms: time_ms,
                                counterexample: None,
                                timestamp: ts,
                            });
                        }
                        (
                            Verdict::Failed {
                                counterexample,
                                expected: exp_val,
                                got: got_val,
                            },
                            "Failed",
                        ) => {
                            failed_count += 1;
                            let mut pt = BTreeMap::new();
                            for (k, v) in counterexample {
                                pt.insert(k, v);
                            }
                            let ce = CounterexampleRecord {
                                point: pt,
                                expected: exp_val,
                                got: got_val,
                            };
                            ledger.record_attempt(AttemptParams {
                                problem_id: &id,
                                claim: &claim_str,
                                answer_given: &ans_given,
                                verdict: "Failed",
                                time_taken_ms: time_ms,
                                counterexample: Some(&ce),
                                timestamp: ts,
                            });
                        }
                        (v, exp) => panic!("Problem {} expected {}, got {:?}", id, exp, v),
                    }
                }
                obj_start = None;
            }
        }
    }

    assert_eq!(verified_count, 15, "must verify 15 valid claims");
    assert_eq!(failed_count, 5, "must catch 5 deliberate mistakes");

    // Mistake ledger verification (all 20 attempts recorded per work order B7)
    assert_eq!(ledger.verify_integrity().unwrap(), 20);
    assert_eq!(ledger.entries().len(), 20);
    for (i, entry) in ledger.entries().iter().enumerate() {
        assert_eq!(entry.sequence, (i + 1) as u64);
        assert!(!entry.problem_id.is_empty());
        assert!(!entry.answer_given.is_empty());
        assert!(!entry.verdict.is_empty());
        assert!(!entry.prev_hash.is_empty());
        assert!(!entry.hash.is_empty());
    }

    // Write to file and re-verify
    fs::write(&ledger_path, ledger.to_json_lines()).unwrap();
    let file_content = fs::read_to_string(&ledger_path).unwrap();
    let file_ledger: MistakeLedger = file_content.parse().unwrap();
    assert_eq!(file_ledger.verify_integrity().unwrap(), 20);

    let _ = fs::remove_file(&ledger_path);
    let _ = fs::remove_dir_all(&temp_dir);
}

#[test]
fn test_corpus_ground_truth_verification() {
    let cpath = corpus_path();
    assert!(cpath.exists(), "corpus file {:?} must exist", cpath);

    let content = fs::read_to_string(&cpath).unwrap();
    let lines: Vec<&str> = content
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();
    assert!(
        lines.len() >= 500,
        "corpus must contain at least 500 rows, found {}",
        lines.len()
    );

    // Check a sample problem of each kind
    let mut tested_kinds = std::collections::HashSet::new();
    for line in &lines {
        let kind = extract_json_str(line, "kind").unwrap();
        if tested_kinds.contains(&kind) {
            continue;
        }

        match kind.as_str() {
            "integral" => {
                let var = extract_json_str(line, "var").unwrap_or_else(|| "x".to_string());
                let integrand = extract_json_str(line, "integrand").unwrap();
                let antideriv = extract_json_str(line, "claimed_antiderivative").unwrap();
                let int_ast = parse(&integrand).unwrap();
                let anti_ast = parse(&antideriv).unwrap();
                let v = verify_integral(&int_ast, &anti_ast, &var);
                assert!(
                    matches!(v, Verdict::Verified { .. }),
                    "integral must verify: {}",
                    line
                );
                tested_kinds.insert(kind);
            }
            "identity" => {
                let lhs_str = extract_json_str(line, "lhs").unwrap();
                let rhs_str = extract_json_str(line, "rhs").unwrap();
                let vars =
                    extract_json_str_array(line, "vars").unwrap_or_else(|| vec!["x".to_string()]);
                let var_refs: Vec<&str> = vars.iter().map(|s| s.as_str()).collect();
                let lhs = parse(&lhs_str).unwrap();
                let rhs = parse(&rhs_str).unwrap();
                let v = verify_identity(&lhs, &rhs, &var_refs);
                assert!(
                    matches!(v, Verdict::Verified { .. }),
                    "identity must verify: {}",
                    line
                );
                tested_kinds.insert(kind);
            }
            "equation" => {
                let var = extract_json_str(line, "var").unwrap_or_else(|| "x".to_string());
                let lhs_str = extract_json_str(line, "lhs").unwrap();
                let rhs_str = extract_json_str(line, "rhs").unwrap();
                let roots = extract_json_f64_array(line, "claimed_roots").unwrap();
                let lhs = parse(&lhs_str).unwrap();
                let rhs = parse(&rhs_str).unwrap();
                let eq = Equation { lhs, rhs };
                let v = verify_equation_solution(&eq, &roots, &var);
                assert!(
                    matches!(v, Verdict::Verified { .. }),
                    "equation must verify: {}",
                    line
                );
                tested_kinds.insert(kind);
            }
            "factorisation" => {
                let orig_str = extract_json_str(line, "original").unwrap();
                let factors = extract_json_str_array(line, "claimed_factors").unwrap();
                let orig = parse(&orig_str).unwrap();
                let mut factor_asts = Vec::new();
                for f in &factors {
                    factor_asts.push(parse(f).unwrap());
                }
                let v = verify_factorisation(&orig, &factor_asts);
                assert!(
                    matches!(v, Verdict::Verified { .. }),
                    "factorisation must verify: {}",
                    line
                );
                tested_kinds.insert(kind);
            }
            _ => {}
        }
    }
    assert_eq!(
        tested_kinds.len(),
        4,
        "must test all 4 kinds in corpus sample"
    );
}

#[test]
fn test_unverifiable_claim_never_verified() {
    let lhs = parse("x^2").unwrap();
    let rhs = parse("0").unwrap();
    let eq = Equation { lhs, rhs };
    let verdict = verify_equation_solution(&eq, &[], "x");
    assert!(matches!(verdict, Verdict::Unverifiable { .. }));
    assert!(!matches!(verdict, Verdict::Verified { .. }));

    let orig = parse("x^2 - 1").unwrap();
    let verdict2 = verify_factorisation(&orig, &[]);
    assert!(matches!(verdict2, Verdict::Unverifiable { .. }));
    assert!(!matches!(verdict2, Verdict::Verified { .. }));
}

#[test]
#[cfg(not(target_arch = "wasm32"))]
fn test_python_independent_ledger_oracle() {
    let py_cmd = if cfg!(windows) { "python" } else { "python3" };
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let script_path = manifest_dir.join("tests/check_b7_ledger.py");
    let repo_root = manifest_dir.parent().unwrap();

    let output = std::process::Command::new(py_cmd)
        .arg(&script_path)
        .current_dir(repo_root)
        .output()
        .expect("failed to execute python oracle script");

    if !output.status.success() {
        panic!(
            "Python oracle failed (exit code {:?}):\nSTDOUT:\n{}\nSTDERR:\n{}",
            output.status.code(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

fn extract_json_str(src: &str, key: &str) -> Option<String> {
    let target = format!("\"{}\":", key);
    let idx = src.find(&target)?;
    let rem = src[idx + target.len()..].trim_start();
    if !rem.starts_with('"') {
        return None;
    }
    let after_quote = &rem[1..];
    let end = after_quote.find('"')?;
    unescape_json(&after_quote[..end]).ok()
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
