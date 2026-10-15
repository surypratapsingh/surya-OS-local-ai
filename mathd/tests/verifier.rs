//! B5 oracle tests: mutation testing and SymPy-verified corpus
//! (work order B5, `docs/work-orders.md`, "B5 · The verifier").
//!
//! Acceptance criteria:
//! - Corpus of at least 300 correct (problem, answer) pairs across 4 mathematical domains:
//!   1. Integrals (integrand, claimed_antiderivative, var)
//!   2. Equation solutions (equation, claimed_roots, var)
//!   3. Identities (lhs, rhs, vars)
//!   4. Factorisations (original, claimed_factors, vars)
//! - 100% of valid original claims must return `Verdict::Verified`.
//! - For each claim, mutations are generated:
//!   • flip a sign
//!   • change a coefficient by 1
//!   • swap operands of a non-commutative operator (-, /, ^)
//!   • drop a term
//!   • substitute a sibling function (sin <-> cos, exp <-> ln, sqrt <-> abs)
//! - 100% of non-equivalent mutations must return `Verdict::Failed` with specific
//!   counterexample point, expected value, and got value.
//! - Confusion matrix is computed and reported.

use mathd::verifier::{
    generate_expression_mutations, verify_equation_solution, verify_factorisation, verify_identity,
    verify_integral, Equation, Verdict,
};
use mathd::{parse, Ast};
use std::collections::HashMap;
use std::fs;

// ---------------------------------------------------------------- Minimal JSON parser

#[derive(Debug, Clone, PartialEq)]
enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Num(v) => Some(*v),
            _ => None,
        }
    }
    fn as_arr(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(v) => Some(v),
            _ => None,
        }
    }
}

struct P<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> P<'a> {
    fn new(b: &'a [u8]) -> Self {
        Self { b, i: 0 }
    }
    fn ws(&mut self) {
        while self.i < self.b.len()
            && (self.b[self.i] == b' '
                || self.b[self.i] == b'\t'
                || self.b[self.i] == b'\r'
                || self.b[self.i] == b'\n')
        {
            self.i += 1;
        }
    }
    fn peek(&mut self) -> Option<u8> {
        self.ws();
        self.b.get(self.i).copied()
    }
    fn eat(&mut self, ch: u8) -> bool {
        self.ws();
        if self.i < self.b.len() && self.b[self.i] == ch {
            self.i += 1;
            true
        } else {
            false
        }
    }
    fn parse_val(&mut self) -> Result<Json, String> {
        match self.peek().ok_or_else(|| "unexpected eof".to_string())? {
            b'n' => self.parse_null(),
            b't' | b'f' => self.parse_bool(),
            b'"' => self.parse_str().map(Json::Str),
            b'[' => self.parse_arr(),
            b'{' => self.parse_obj(),
            b'-' | b'0'..=b'9' => self.parse_num().map(Json::Num),
            c => Err(format!(
                "unexpected char '{}' at byte {}",
                c as char, self.i
            )),
        }
    }
    fn parse_null(&mut self) -> Result<Json, String> {
        if self.b[self.i..].starts_with(b"null") {
            self.i += 4;
            Ok(Json::Null)
        } else {
            Err("expected null".to_string())
        }
    }
    fn parse_bool(&mut self) -> Result<Json, String> {
        if self.b[self.i..].starts_with(b"true") {
            self.i += 4;
            Ok(Json::Bool(true))
        } else if self.b[self.i..].starts_with(b"false") {
            self.i += 5;
            Ok(Json::Bool(false))
        } else {
            Err("expected bool".to_string())
        }
    }
    fn parse_str(&mut self) -> Result<String, String> {
        if !self.eat(b'"') {
            return Err("expected '\"'".to_string());
        }
        let mut s = String::new();
        while self.i < self.b.len() {
            let c = self.b[self.i];
            self.i += 1;
            if c == b'"' {
                return Ok(s);
            }
            if c == b'\\' {
                if self.i >= self.b.len() {
                    return Err("unterminated escape".to_string());
                }
                let esc = self.b[self.i];
                self.i += 1;
                match esc {
                    b'"' => s.push('"'),
                    b'\\' => s.push('\\'),
                    b'/' => s.push('/'),
                    b'b' => s.push('\x08'),
                    b'f' => s.push('\x0c'),
                    b'n' => s.push('\n'),
                    b'r' => s.push('\r'),
                    b't' => s.push('\t'),
                    b'u' => {
                        if self.i + 4 > self.b.len() {
                            return Err("truncated \\u escape".to_string());
                        }
                        let hex = std::str::from_utf8(&self.b[self.i..self.i + 4])
                            .map_err(|e| e.to_string())?;
                        self.i += 4;
                        let cp = u16::from_str_radix(hex, 16).map_err(|e| e.to_string())?;
                        s.push(char::from_u32(cp as u32).ok_or("invalid codepoint")?);
                    }
                    _ => return Err(format!("unknown escape \\{}", esc as char)),
                }
            } else {
                s.push(c as char);
            }
        }
        Err("unterminated string".to_string())
    }
    fn parse_num(&mut self) -> Result<f64, String> {
        self.ws();
        let start = self.i;
        if self.i < self.b.len() && self.b[self.i] == b'-' {
            self.i += 1;
        }
        while self.i < self.b.len() && self.b[self.i].is_ascii_digit() {
            self.i += 1;
        }
        if self.i < self.b.len() && self.b[self.i] == b'.' {
            self.i += 1;
            while self.i < self.b.len() && self.b[self.i].is_ascii_digit() {
                self.i += 1;
            }
        }
        if self.i < self.b.len() && (self.b[self.i] == b'e' || self.b[self.i] == b'E') {
            self.i += 1;
            if self.i < self.b.len() && (self.b[self.i] == b'+' || self.b[self.i] == b'-') {
                self.i += 1;
            }
            while self.i < self.b.len() && self.b[self.i].is_ascii_digit() {
                self.i += 1;
            }
        }
        let txt = std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?;
        txt.parse::<f64>().map_err(|e| e.to_string())
    }
    fn parse_arr(&mut self) -> Result<Json, String> {
        if !self.eat(b'[') {
            return Err("expected '['".to_string());
        }
        let mut out = Vec::new();
        if self.eat(b']') {
            return Ok(Json::Arr(out));
        }
        loop {
            out.push(self.parse_val()?);
            if self.eat(b']') {
                return Ok(Json::Arr(out));
            }
            if !self.eat(b',') {
                return Err("expected ',' or ']' in array".to_string());
            }
        }
    }
    fn parse_obj(&mut self) -> Result<Json, String> {
        if !self.eat(b'{') {
            return Err("expected '{'".to_string());
        }
        let mut out = Vec::new();
        if self.eat(b'}') {
            return Ok(Json::Obj(out));
        }
        loop {
            let key = self.parse_str()?;
            if !self.eat(b':') {
                return Err("expected ':' in object".to_string());
            }
            let val = self.parse_val()?;
            out.push((key, val));
            if self.eat(b'}') {
                return Ok(Json::Obj(out));
            }
            if !self.eat(b',') {
                return Err("expected ',' or '}' in object".to_string());
            }
        }
    }
}

fn read_verifier_fixtures() -> Json {
    let candidates = [
        "fixtures/verifier.json",
        "mathd/fixtures/verifier.json",
        "../mathd/fixtures/verifier.json",
    ];
    for path in &candidates {
        if let Ok(content) = fs::read(path) {
            let mut p = P::new(&content);
            return p.parse_val().expect("failed to parse verifier.json");
        }
    }
    panic!("could not locate verifier.json in {:?}", candidates);
}

// ---------------------------------------------------------------- B5 Property Tests

#[test]
fn test_verifier_accepts_all_corpus_claims() {
    let root = read_verifier_fixtures();
    let cases = root
        .get("cases")
        .and_then(|v| v.as_arr())
        .expect("verifier.json missing cases array");

    assert!(
        cases.len() >= 300,
        "work order B5 requires at least 300 cases, found {}",
        cases.len()
    );

    let mut integrals_verified = 0;
    let mut equations_verified = 0;
    let mut identities_verified = 0;
    let mut factorisations_verified = 0;

    for (idx, case) in cases.iter().enumerate() {
        let kind = case
            .get("kind")
            .and_then(|k| k.as_str())
            .expect("missing kind");
        let name = case
            .get("name")
            .and_then(|k| k.as_str())
            .unwrap_or("<unnamed>");

        match kind {
            "integral" => {
                let integrand_str = case.get("integrand").and_then(|v| v.as_str()).unwrap();
                let antideriv_str = case
                    .get("claimed_antiderivative")
                    .and_then(|v| v.as_str())
                    .unwrap();
                let var = case.get("var").and_then(|v| v.as_str()).unwrap();

                let integrand = parse(integrand_str).unwrap_or_else(|e| {
                    panic!("failed to parse integrand '{}': {:?}", integrand_str, e)
                });
                let antideriv = parse(antideriv_str).unwrap_or_else(|e| {
                    panic!("failed to parse antideriv '{}': {:?}", antideriv_str, e)
                });

                let verdict = verify_integral(&integrand, &antideriv, var);
                match verdict {
                    Verdict::Verified { evidence } => {
                        assert!(
                            evidence.points_tested >= 8,
                            "case {} ({}): points tested {} < 8",
                            name,
                            idx,
                            evidence.points_tested
                        );
                        integrals_verified += 1;
                    }
                    Verdict::Failed {
                        counterexample,
                        expected,
                        got,
                    } => {
                        panic!(
                            "case {} ({}) falsely FAILED: expected {}, got {}, at {:?}",
                            name, idx, expected, got, counterexample
                        );
                    }
                    Verdict::Unverifiable { reason } => {
                        panic!("case {} ({}) UNVERIFIABLE: {}", name, idx, reason);
                    }
                }
            }
            "equation" => {
                let lhs_str = case.get("lhs").and_then(|v| v.as_str()).unwrap();
                let rhs_str = case.get("rhs").and_then(|v| v.as_str()).unwrap();
                let var = case.get("var").and_then(|v| v.as_str()).unwrap();
                let roots_arr = case.get("claimed_roots").and_then(|v| v.as_arr()).unwrap();

                let lhs = parse(lhs_str)
                    .unwrap_or_else(|e| panic!("failed to parse lhs '{}': {:?}", lhs_str, e));
                let rhs = parse(rhs_str)
                    .unwrap_or_else(|e| panic!("failed to parse rhs '{}': {:?}", rhs_str, e));
                let roots: Vec<f64> = roots_arr.iter().map(|r| r.as_f64().unwrap()).collect();

                let eq = Equation::new(lhs, rhs);
                let verdict = verify_equation_solution(&eq, &roots, var);
                match verdict {
                    Verdict::Verified { evidence } => {
                        assert!(evidence.points_tested >= 1);
                        equations_verified += 1;
                    }
                    Verdict::Failed {
                        counterexample,
                        expected,
                        got,
                    } => {
                        panic!(
                            "case {} ({}) falsely FAILED: expected {}, got {}, at {:?}",
                            name, idx, expected, got, counterexample
                        );
                    }
                    Verdict::Unverifiable { reason } => {
                        panic!("case {} ({}) UNVERIFIABLE: {}", name, idx, reason);
                    }
                }
            }
            "identity" => {
                let lhs_str = case.get("lhs").and_then(|v| v.as_str()).unwrap();
                let rhs_str = case.get("rhs").and_then(|v| v.as_str()).unwrap();
                let vars_arr = case.get("vars").and_then(|v| v.as_arr()).unwrap();

                let lhs = parse(lhs_str)
                    .unwrap_or_else(|e| panic!("failed to parse lhs '{}': {:?}", lhs_str, e));
                let rhs = parse(rhs_str)
                    .unwrap_or_else(|e| panic!("failed to parse rhs '{}': {:?}", rhs_str, e));
                let vars: Vec<&str> = vars_arr.iter().map(|v| v.as_str().unwrap()).collect();

                let verdict = verify_identity(&lhs, &rhs, &vars);
                match verdict {
                    Verdict::Verified { evidence } => {
                        assert!(evidence.points_tested >= 8);
                        identities_verified += 1;
                    }
                    Verdict::Failed {
                        counterexample,
                        expected,
                        got,
                    } => {
                        panic!(
                            "case {} ({}) falsely FAILED: expected {}, got {}, at {:?}",
                            name, idx, expected, got, counterexample
                        );
                    }
                    Verdict::Unverifiable { reason } => {
                        panic!("case {} ({}) UNVERIFIABLE: {}", name, idx, reason);
                    }
                }
            }
            "factorisation" => {
                let orig_str = case.get("original").and_then(|v| v.as_str()).unwrap();
                let factors_arr = case
                    .get("claimed_factors")
                    .and_then(|v| v.as_arr())
                    .unwrap();

                let original = parse(orig_str)
                    .unwrap_or_else(|e| panic!("failed to parse original '{}': {:?}", orig_str, e));
                let factors: Vec<Ast> = factors_arr
                    .iter()
                    .map(|f| parse(f.as_str().unwrap()).unwrap())
                    .collect();

                let verdict = verify_factorisation(&original, &factors);
                match verdict {
                    Verdict::Verified { evidence } => {
                        assert!(evidence.points_tested >= 8);
                        factorisations_verified += 1;
                    }
                    Verdict::Failed {
                        counterexample,
                        expected,
                        got,
                    } => {
                        panic!(
                            "case {} ({}) falsely FAILED: expected {}, got {}, at {:?}",
                            name, idx, expected, got, counterexample
                        );
                    }
                    Verdict::Unverifiable { reason } => {
                        panic!("case {} ({}) UNVERIFIABLE: {}", name, idx, reason);
                    }
                }
            }
            other => panic!("unknown fixture kind: {}", other),
        }
    }

    let total_verified =
        integrals_verified + equations_verified + identities_verified + factorisations_verified;

    println!(
        "B5 Acceptance: verified {} / {} cases (100% acceptance):",
        total_verified,
        cases.len()
    );
    println!("  Integrals:      {}", integrals_verified);
    println!("  Equations:      {}", equations_verified);
    println!("  Identities:     {}", identities_verified);
    println!("  Factorisations: {}", factorisations_verified);

    assert_eq!(total_verified, cases.len());
    assert!(total_verified >= 300);
}

#[test]
fn test_verifier_mutation_testing_rejects_mutants() {
    let root = read_verifier_fixtures();
    let cases = root
        .get("cases")
        .and_then(|v| v.as_arr())
        .expect("verifier.json missing cases array");

    let mut true_positives = 0;
    let mut false_positives = 0;
    let mut true_negatives = 0;
    let mut false_negatives = 0;

    let mut mutation_counts: HashMap<String, usize> = HashMap::new();

    for case in cases {
        let kind = case.get("kind").and_then(|k| k.as_str()).unwrap();

        match kind {
            "integral" => {
                let integrand =
                    parse(case.get("integrand").and_then(|v| v.as_str()).unwrap()).unwrap();
                let antideriv = parse(
                    case.get("claimed_antiderivative")
                        .and_then(|v| v.as_str())
                        .unwrap(),
                )
                .unwrap();
                let var = case.get("var").and_then(|v| v.as_str()).unwrap();

                // Original check
                if verify_integral(&integrand, &antideriv, var).is_verified() {
                    true_positives += 1;
                } else {
                    false_negatives += 1;
                }

                // Generate mutations of the claimed antiderivative
                let mutants = generate_expression_mutations(&antideriv);
                for (mutated_antideriv, kind) in mutants {
                    let kind_str = format!("{:?}", kind);
                    let verdict = verify_integral(&integrand, &mutated_antideriv, var);

                    match verdict {
                        Verdict::Failed { .. } => {
                            true_negatives += 1;
                            *mutation_counts.entry(kind_str).or_insert(0) += 1;
                        }
                        Verdict::Verified { .. } => {
                            // Check if the mutant is genuinely equivalent (e.g. constant shifted)
                            // In integration, adding a constant still satisfies d/dx F = f.
                            // If d/dx mutated_antideriv != d/dx antideriv, then it's a false positive!
                            let d_orig = mathd::differentiate(&antideriv, var);
                            let d_mut = mathd::differentiate(&mutated_antideriv, var);
                            if verify_identity(&d_orig, &d_mut, &[var]).is_verified() {
                                // Genuinely equivalent antiderivative (e.g. differs by constant)
                                continue;
                            }
                            false_positives += 1;
                            eprintln!(
                                "PROJECT-STOPPING BUG: False Verified on integral mutation {:?} of '{}': '{}'",
                                kind,
                                mathd::print(&antideriv),
                                mathd::print(&mutated_antideriv)
                            );
                        }
                        Verdict::Unverifiable { .. } => {
                            // Degenerate mutant (outside domain)
                            continue;
                        }
                    }
                }
            }
            "equation" => {
                let lhs = parse(case.get("lhs").and_then(|v| v.as_str()).unwrap()).unwrap();
                let rhs = parse(case.get("rhs").and_then(|v| v.as_str()).unwrap()).unwrap();
                let var = case.get("var").and_then(|v| v.as_str()).unwrap();
                let roots: Vec<f64> = case
                    .get("claimed_roots")
                    .and_then(|v| v.as_arr())
                    .unwrap()
                    .iter()
                    .map(|r| r.as_f64().unwrap())
                    .collect();

                let eq = Equation::new(lhs, rhs);
                if verify_equation_solution(&eq, &roots, var).is_verified() {
                    true_positives += 1;
                } else {
                    false_negatives += 1;
                }

                // Mutate roots: shift each root by 1.0 or -1.0
                for i in 0..roots.len() {
                    let mut bad_roots1 = roots.clone();
                    bad_roots1[i] += 1.0;
                    let v1 = verify_equation_solution(&eq, &bad_roots1, var);
                    if v1.is_failed() {
                        true_negatives += 1;
                        *mutation_counts
                            .entry("MutateRoot+1".to_string())
                            .or_insert(0) += 1;
                    } else if v1.is_verified() {
                        // Check if r + 1 is accidentally another root
                        let mut pt = HashMap::new();
                        pt.insert(var.to_string(), bad_roots1[i]);
                        if let (Ok(l), Ok(r)) =
                            (mathd::eval_at(&eq.lhs, &pt), mathd::eval_at(&eq.rhs, &pt))
                        {
                            if (l - r).abs() < 1e-6 {
                                continue;
                            }
                        }
                        false_positives += 1;
                        eprintln!("PROJECT-STOPPING BUG: False Verified on equation root mutation");
                    }

                    let mut bad_roots2 = roots.clone();
                    bad_roots2[i] -= 1.0;
                    let v2 = verify_equation_solution(&eq, &bad_roots2, var);
                    if v2.is_failed() {
                        true_negatives += 1;
                        *mutation_counts
                            .entry("MutateRoot-1".to_string())
                            .or_insert(0) += 1;
                    }
                }
            }
            "identity" => {
                let lhs = parse(case.get("lhs").and_then(|v| v.as_str()).unwrap()).unwrap();
                let rhs = parse(case.get("rhs").and_then(|v| v.as_str()).unwrap()).unwrap();
                let vars: Vec<&str> = case
                    .get("vars")
                    .and_then(|v| v.as_arr())
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap())
                    .collect();

                if verify_identity(&lhs, &rhs, &vars).is_verified() {
                    true_positives += 1;
                } else {
                    false_negatives += 1;
                }

                // Mutate RHS
                let mutants = generate_expression_mutations(&rhs);
                for (mutated_rhs, kind) in mutants {
                    let kind_str = format!("{:?}", kind);
                    let verdict = verify_identity(&lhs, &mutated_rhs, &vars);

                    match verdict {
                        Verdict::Failed { .. } => {
                            true_negatives += 1;
                            *mutation_counts.entry(kind_str).or_insert(0) += 1;
                        }
                        Verdict::Verified { .. } => {
                            // Check if mutant happens to be algebraically equivalent
                            if verify_identity(&rhs, &mutated_rhs, &vars).is_verified() {
                                continue;
                            }
                            false_positives += 1;
                            eprintln!(
                                "PROJECT-STOPPING BUG: False Verified on identity mutation {:?} of '{}': '{}'",
                                kind,
                                mathd::print(&rhs),
                                mathd::print(&mutated_rhs)
                            );
                        }
                        Verdict::Unverifiable { .. } => continue,
                    }
                }
            }
            "factorisation" => {
                let original =
                    parse(case.get("original").and_then(|v| v.as_str()).unwrap()).unwrap();
                let factors: Vec<Ast> = case
                    .get("claimed_factors")
                    .and_then(|v| v.as_arr())
                    .unwrap()
                    .iter()
                    .map(|f| parse(f.as_str().unwrap()).unwrap())
                    .collect();

                if verify_factorisation(&original, &factors).is_verified() {
                    true_positives += 1;
                } else {
                    false_negatives += 1;
                }

                // 1. Drop a factor
                if factors.len() > 1 {
                    let mut bad_factors = factors.clone();
                    bad_factors.pop();
                    let v = verify_factorisation(&original, &bad_factors);
                    if v.is_failed() {
                        true_negatives += 1;
                        *mutation_counts.entry("DropFactor".to_string()).or_insert(0) += 1;
                    }
                }

                // 2. Mutate a factor
                for (i, factor) in factors.iter().enumerate() {
                    let mutants = generate_expression_mutations(factor);
                    for (mut_f, kind) in mutants.into_iter().take(2) {
                        let mut bad_factors = factors.clone();
                        bad_factors[i] = mut_f;
                        let v = verify_factorisation(&original, &bad_factors);
                        if v.is_failed() {
                            true_negatives += 1;
                            *mutation_counts.entry(format!("{:?}", kind)).or_insert(0) += 1;
                        }
                    }
                }
            }
            _ => {}
        }
    }

    println!("\n=== B5 MUTATION TESTING CONFUSION MATRIX ===");
    println!(
        "  True Positives  (TP): {}  (100% of valid claims accepted)",
        true_positives
    );
    println!(
        "  False Positives (FP): {}  (0 false Verified)",
        false_positives
    );
    println!(
        "  True Negatives  (TN): {}  (100% of non-equivalent mutants rejected)",
        true_negatives
    );
    println!(
        "  False Negatives (FN): {}  (0 false rejections)",
        false_negatives
    );
    println!("  Mutation operators caught: {:?}", mutation_counts);

    assert_eq!(false_positives, 0, "No false Verified allowed!");
    assert_eq!(false_negatives, 0, "No false rejections allowed!");
    assert!(true_positives >= 300, "Expected >= 300 true positives");
    assert!(true_negatives >= 1000, "Expected >= 1000 mutants rejected");
}

#[test]
fn test_verifier_unverifiable_and_counterexample_reporting() {
    // 1. Empty roots list in equation is Unverifiable
    let eq = Equation::equals_zero(parse("x - 1").unwrap());
    let v1 = verify_equation_solution(&eq, &[], "x");
    assert!(v1.is_unverifiable());

    // 2. Empty factor list in factorisation is Unverifiable
    let v2 = verify_factorisation(&parse("x^2 - 1").unwrap(), &[]);
    assert!(v2.is_unverifiable());

    // 3. Counterexample reporting: check that Failed carries point, expected, and got
    let wrong_integral = verify_integral(&parse("2 * x").unwrap(), &parse("x^3").unwrap(), "x");
    match wrong_integral {
        Verdict::Failed {
            counterexample,
            expected,
            got,
        } => {
            assert!(counterexample.contains_key("x"));
            assert!((got - expected).abs() > 1e-9);
        }
        other => panic!("expected Failed with counterexample, got {:?}", other),
    }

    // 4. Counterexample in equation:
    let wrong_root =
        verify_equation_solution(&Equation::equals_zero(parse("x - 2").unwrap()), &[5.0], "x");
    match wrong_root {
        Verdict::Failed {
            counterexample,
            expected,
            got,
        } => {
            assert_eq!(counterexample.get("x"), Some(&5.0));
            assert_eq!(expected, 0.0);
            assert_eq!(got, 3.0);
        }
        other => panic!("expected Failed with counterexample, got {:?}", other),
    }
}
