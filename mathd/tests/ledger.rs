//! Integration tests for B7 mistake ledger and Prove-it verification (work order B7).

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use mathd::ledger::{
    escape_json, unescape_json, CounterexampleRecord, LedgerVerifyError, MistakeLedger,
    GENESIS_PREV_HASH,
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

    {
        let mut ledger = MistakeLedger::open(&ledger_path).unwrap();
        assert_eq!(ledger.verify_integrity().unwrap(), 0);

        let mut pt1 = BTreeMap::new();
        pt1.insert("x".to_string(), 1.5);
        let ce1 = CounterexampleRecord {
            point: pt1,
            expected: 6.25,
            got: 3.25,
        };
        let e1 = ledger
            .record_failed("identity: (x + 1)^2 == x^2 + 1", &ce1)
            .unwrap();
        assert_eq!(e1.sequence, 1);
        assert_eq!(e1.prev_hash, GENESIS_PREV_HASH);

        let mut pt2 = BTreeMap::new();
        pt2.insert("x".to_string(), 0.0);
        let ce2 = CounterexampleRecord {
            point: pt2,
            expected: 0.0,
            got: 1.0,
        };
        let e2 = ledger
            .record_failed("identity: cos(x)^2 + sin(x)^2 == 0", &ce2)
            .unwrap();
        assert_eq!(e2.sequence, 2);
        assert_eq!(e2.prev_hash, e1.hash);

        assert_eq!(ledger.verify_integrity().unwrap(), 2);
    }

    // Verify reopen and integrity
    {
        let ledger = MistakeLedger::open(&ledger_path).unwrap();
        assert_eq!(ledger.last_sequence(), 2);
        let entries = ledger.read_entries().unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].claim, "identity: (x + 1)^2 == x^2 + 1");
        assert_eq!(entries[1].claim, "identity: cos(x)^2 + sin(x)^2 == 0");
        assert_eq!(ledger.verify_integrity().unwrap(), 2);
    }

    // Sabotage gate: tamper one byte
    {
        let raw = fs::read_to_string(&ledger_path).unwrap();
        let tampered = raw.replacen("x^2 + 1", "x^2 + 2", 1);
        fs::write(&ledger_path, tampered).unwrap();

        let ledger = MistakeLedger::open(&ledger_path).unwrap();
        let res = ledger.verify_integrity();
        assert!(matches!(
            res,
            Err(LedgerVerifyError::HashMismatch { line: 1, .. })
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

    let mut ledger = MistakeLedger::open(&ledger_path).unwrap();

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
                    let expected = extract_json_str(obj_str, "expected_verdict").unwrap();

                    let verdict = match ctype.as_str() {
                        "identity" => {
                            let lhs_str = extract_json_str(obj_str, "lhs").unwrap();
                            let rhs_str = extract_json_str(obj_str, "rhs").unwrap();
                            let lhs = parse(&lhs_str).unwrap();
                            let rhs = parse(&rhs_str).unwrap();
                            verify_identity(&lhs, &rhs, &[])
                        }
                        "integral" => {
                            let int_str = extract_json_str(obj_str, "integrand").unwrap();
                            let anti_str = extract_json_str(obj_str, "antiderivative").unwrap();
                            let var = extract_json_str(obj_str, "var").unwrap();
                            let integrand = parse(&int_str).unwrap();
                            let antideriv = parse(&anti_str).unwrap();
                            verify_integral(&integrand, &antideriv, &var)
                        }
                        "equation" => {
                            let lhs_str = extract_json_str(obj_str, "lhs").unwrap();
                            let rhs_str = extract_json_str(obj_str, "rhs").unwrap();
                            let var = extract_json_str(obj_str, "var").unwrap();
                            let roots = extract_json_f64_array(obj_str, "roots").unwrap();
                            let lhs = parse(&lhs_str).unwrap();
                            let rhs = parse(&rhs_str).unwrap();
                            let eq = Equation { lhs, rhs };
                            verify_equation_solution(&eq, &roots, &var)
                        }
                        "factorisation" => {
                            let orig_str = extract_json_str(obj_str, "original").unwrap();
                            let factors_strs = extract_json_str_array(obj_str, "factors").unwrap();
                            let orig = parse(&orig_str).unwrap();
                            let mut factors = Vec::new();
                            for f_str in factors_strs {
                                factors.push(parse(&f_str).unwrap());
                            }
                            verify_factorisation(&orig, &factors)
                        }
                        other => panic!("unknown claim_type: {}", other),
                    };

                    match (verdict, expected.as_str()) {
                        (Verdict::Verified { evidence }, "Verified") => {
                            assert!(evidence.points_tested >= 1);
                            verified_count += 1;
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
                            ledger.record_failed(&id, &ce).unwrap();
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

    // Mistake ledger verification
    assert_eq!(ledger.verify_integrity().unwrap(), 5);
    let entries = ledger.read_entries().unwrap();
    assert_eq!(entries.len(), 5);
    for (i, entry) in entries.iter().enumerate() {
        assert_eq!(entry.sequence, (i + 1) as u64);
        assert!(!entry.prev_hash.is_empty());
        assert!(!entry.hash.is_empty());
    }

    let _ = fs::remove_file(&ledger_path);
    let _ = fs::remove_dir_all(&temp_dir);
}

#[test]
fn test_unverifiable_claim_never_verified() {
    // Empty roots on equation solution returns Unverifiable
    let lhs = parse("x^2").unwrap();
    let rhs = parse("0").unwrap();
    let eq = Equation { lhs, rhs };
    let verdict = verify_equation_solution(&eq, &[], "x");
    assert!(matches!(verdict, Verdict::Unverifiable { .. }));
    assert!(!matches!(verdict, Verdict::Verified { .. }));

    // Empty factors list on factorisation returns Unverifiable
    let orig = parse("x^2 - 1").unwrap();
    let verdict2 = verify_factorisation(&orig, &[]);
    assert!(matches!(verdict2, Verdict::Unverifiable { .. }));
    assert!(!matches!(verdict2, Verdict::Verified { .. }));
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
    Some(after_quote[..end].to_string())
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
            out.push(trimmed.to_string());
        }
    }
    Some(out)
}
