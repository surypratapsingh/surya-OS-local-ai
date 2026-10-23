//! B6 corpus integration tests. Every expected claim was independently checked
//! and recorded by the committed SymPy generator at `mathd/corpus/generate_corpus.py`.
//! This harness parses those fixtures using std only, then scores claims through
//! the B5 numerical verifier; it never trusts a self-reported boolean as proof.

use mathd::verifier::{
    verify_equation_solution, verify_factorisation, verify_identity, verify_integral, Equation,
    Verdict,
};
use mathd::{parse, Ast};
use std::fs;

#[derive(Debug, Clone, PartialEq)]
enum Json {
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(pairs) => pairs.iter().find(|(name, _)| name == key).map(|(_, v)| v),
            _ => None,
        }
    }

    fn as_str(&self) -> Option<&str> {
        match self {
            Json::String(value) => Some(value),
            _ => None,
        }
    }

    fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Array(values) => Some(values),
            _ => None,
        }
    }

    fn as_number(&self) -> Option<f64> {
        match self {
            Json::Number(value) => value.parse().ok(),
            _ => None,
        }
    }
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, pos: 0 }
    }

    fn whitespace(&mut self) {
        while self
            .bytes
            .get(self.pos)
            .is_some_and(u8::is_ascii_whitespace)
        {
            self.pos += 1;
        }
    }

    fn consume(&mut self, byte: u8) -> bool {
        self.whitespace();
        if self.bytes.get(self.pos) == Some(&byte) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        self.whitespace();
        match self.bytes.get(self.pos).copied() {
            Some(b'"') => self.string().map(Json::String),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b't') => self.literal(b"true", Json::Bool(true)),
            Some(b'f') => self.literal(b"false", Json::Bool(false)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(byte) => Err(format!(
                "unexpected byte {:?} at {}",
                byte as char, self.pos
            )),
            None => Err("unexpected end of JSON".to_string()),
        }
    }

    fn literal(&mut self, literal: &[u8], value: Json) -> Result<Json, String> {
        if self.bytes[self.pos..].starts_with(literal) {
            self.pos += literal.len();
            Ok(value)
        } else {
            Err(format!("invalid literal at {}", self.pos))
        }
    }

    fn string(&mut self) -> Result<String, String> {
        if !self.consume(b'"') {
            return Err(format!("expected string at {}", self.pos));
        }
        let mut output = String::new();
        while let Some(byte) = self.bytes.get(self.pos).copied() {
            self.pos += 1;
            match byte {
                b'"' => return Ok(output),
                b'\\' => {
                    let escaped = *self
                        .bytes
                        .get(self.pos)
                        .ok_or_else(|| "trailing string escape".to_string())?;
                    self.pos += 1;
                    match escaped {
                        b'"' => output.push('"'),
                        b'\\' => output.push('\\'),
                        b'/' => output.push('/'),
                        b'b' => output.push('\u{0008}'),
                        b'f' => output.push('\u{000c}'),
                        b'n' => output.push('\n'),
                        b'r' => output.push('\r'),
                        b't' => output.push('\t'),
                        b'u' => {
                            let end = self.pos + 4;
                            let hex = std::str::from_utf8(
                                self.bytes
                                    .get(self.pos..end)
                                    .ok_or_else(|| "truncated unicode escape".to_string())?,
                            )
                            .map_err(|error| error.to_string())?;
                            self.pos = end;
                            let value =
                                u16::from_str_radix(hex, 16).map_err(|error| error.to_string())?;
                            let character = char::from_u32(u32::from(value))
                                .ok_or_else(|| "invalid unicode escape".to_string())?;
                            output.push(character);
                        }
                        other => return Err(format!("invalid escape {:?}", other as char)),
                    }
                }
                other if other < 0x20 => return Err("control byte in JSON string".to_string()),
                other if other.is_ascii() => output.push(other as char),
                _ => {
                    self.pos -= 1;
                    let tail = std::str::from_utf8(&self.bytes[self.pos..])
                        .map_err(|error| error.to_string())?;
                    let character = tail
                        .chars()
                        .next()
                        .ok_or_else(|| "invalid UTF-8 in string".to_string())?;
                    output.push(character);
                    self.pos += character.len_utf8();
                }
            }
        }
        Err("unterminated JSON string".to_string())
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.bytes.get(self.pos) == Some(&b'-') {
            self.pos += 1;
        }
        match self.bytes.get(self.pos) {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                self.pos += 1;
                while self.bytes.get(self.pos).is_some_and(u8::is_ascii_digit) {
                    self.pos += 1;
                }
            }
            _ => return Err(format!("invalid number at {start}")),
        }
        if self.bytes.get(self.pos) == Some(&b'.') {
            self.pos += 1;
            let fraction_start = self.pos;
            while self.bytes.get(self.pos).is_some_and(u8::is_ascii_digit) {
                self.pos += 1;
            }
            if self.pos == fraction_start {
                return Err(format!("empty fraction at {start}"));
            }
        }
        if matches!(self.bytes.get(self.pos), Some(b'e' | b'E')) {
            self.pos += 1;
            if matches!(self.bytes.get(self.pos), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            let exponent_start = self.pos;
            while self.bytes.get(self.pos).is_some_and(u8::is_ascii_digit) {
                self.pos += 1;
            }
            if self.pos == exponent_start {
                return Err(format!("empty exponent at {start}"));
            }
        }
        let text =
            std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|error| error.to_string())?;
        Ok(Json::Number(text.to_string()))
    }

    fn array(&mut self) -> Result<Json, String> {
        if !self.consume(b'[') {
            return Err(format!("expected array at {}", self.pos));
        }
        let mut values = Vec::new();
        if self.consume(b']') {
            return Ok(Json::Array(values));
        }
        loop {
            values.push(self.value()?);
            if self.consume(b']') {
                return Ok(Json::Array(values));
            }
            if !self.consume(b',') {
                return Err(format!("expected comma or ] at {}", self.pos));
            }
        }
    }

    fn object(&mut self) -> Result<Json, String> {
        if !self.consume(b'{') {
            return Err(format!("expected object at {}", self.pos));
        }
        let mut pairs = Vec::new();
        if self.consume(b'}') {
            return Ok(Json::Object(pairs));
        }
        loop {
            let key = self.string()?;
            if !self.consume(b':') {
                return Err(format!("expected colon after {key:?}"));
            }
            pairs.push((key, self.value()?));
            if self.consume(b'}') {
                return Ok(Json::Object(pairs));
            }
            if !self.consume(b',') {
                return Err(format!("expected comma or }} at {}", self.pos));
            }
        }
    }
}

fn parse_json_line(line: &str) -> Result<Json, String> {
    let mut parser = Parser::new(line.as_bytes());
    let value = parser.value()?;
    parser.whitespace();
    if parser.pos != parser.bytes.len() {
        return Err(format!("trailing JSON bytes at {}", parser.pos));
    }
    Ok(value)
}

fn fixture_rows(path: &str) -> Vec<Json> {
    let content = fs::read_to_string(path).unwrap_or_else(|error| panic!("{path}: {error}"));
    content
        .lines()
        .enumerate()
        .map(|(line, text)| {
            parse_json_line(text).unwrap_or_else(|error| panic!("{path}:{}: {error}", line + 1))
        })
        .collect()
}

fn field_str<'a>(row: &'a Json, key: &str) -> &'a str {
    row.get(key)
        .and_then(Json::as_str)
        .unwrap_or_else(|| panic!("missing string field {key:?}"))
}

fn parse_expr(text: &str, case_id: &str, field: &str) -> Ast {
    parse(text)
        .unwrap_or_else(|error| panic!("{case_id}: cannot parse {field} {text:?}: {error:?}"))
}

fn must_verify(verdict: Verdict, case_id: &str, claim: &str) {
    match verdict {
        Verdict::Verified { .. } => {}
        Verdict::Failed { counterexample, expected, got } => panic!(
            "{case_id}: SymPy-verified {claim} failed at {counterexample:?}: expected {expected}, got {got}"
        ),
        Verdict::Unverifiable { reason } => panic!("{case_id}: {claim} unverifiable: {reason}"),
    }
}

fn must_reject(verdict: Verdict, case_id: &str, description: &str) {
    match verdict {
        Verdict::Failed {
            counterexample,
            expected,
            got,
        } => {
            assert!(
                (expected - got).abs() > 1e-9,
                "{case_id}: rejection lacks a numeric counterexample"
            );
            assert!(
                !counterexample.is_empty(),
                "{case_id}: rejection lacks a counterexample point"
            );
        }
        Verdict::Verified { .. } => panic!("{case_id}: accepted SymPy-disproved {description}"),
        Verdict::Unverifiable { reason } => {
            panic!("{case_id}: could not reject {description}: {reason}")
        }
    }
}

#[test]
fn sympy_ground_truth_corpus_is_accepted_and_mutants_rejected() {
    let cases = fixture_rows("corpus/ground_truth.jsonl");
    assert_eq!(cases.len(), 500, "B6 requires at least 500 problems");
    let wrong_rows = fixture_rows("corpus/wrong_answers.jsonl");
    assert_eq!(
        wrong_rows.len(),
        cases.len(),
        "wrong-answer coverage differs from ground truth"
    );

    let mut integrals = 0;
    let mut equations = 0;
    let mut identities = 0;
    let mut factorisations = 0;
    let mut mutant_count = 0;

    for (case, wrong_row) in cases.iter().zip(&wrong_rows) {
        let case_id = field_str(case, "id");
        assert_eq!(
            field_str(case, "source"),
            "generated",
            "{case_id}: source is not marked generated"
        );
        assert_eq!(
            field_str(wrong_row, "id"),
            case_id,
            "wrong-answer ID mismatch"
        );
        let kind = field_str(case, "kind");
        let proof = case.get("sympy_verification").expect("missing SymPy proof");
        assert_eq!(
            proof.get("verified"),
            Some(&Json::Bool(true)),
            "{case_id}: missing positive SymPy result"
        );
        let proof_call = proof
            .get("call")
            .and_then(Json::as_str)
            .unwrap_or_else(|| panic!("{case_id}: missing recorded SymPy call"));
        let proof_result = field_str(proof, "result");

        match kind {
            "integral" => {
                let integrand = parse_expr(field_str(case, "integrand"), case_id, "integrand");
                let answer = parse_expr(
                    field_str(case, "claimed_antiderivative"),
                    case_id,
                    "claimed_antiderivative",
                );
                assert!(
                    proof_call.starts_with("simplify(diff("),
                    "{case_id}: unexpected SymPy integral check: {proof_call}"
                );
                assert!(
                    proof_call
                        .contains(&field_str(case, "claimed_antiderivative").replace('^', "**")),
                    "{case_id}: SymPy call does not contain the claimed antiderivative"
                );
                assert_eq!(
                    proof_result, "0",
                    "{case_id}: SymPy reported nonzero integral residual"
                );
                must_verify(
                    verify_integral(&integrand, &answer, field_str(case, "var")),
                    case_id,
                    "integral",
                );
                integrals += 1;
            }
            "equation" => {
                let lhs = parse_expr(field_str(case, "lhs"), case_id, "lhs");
                let rhs = parse_expr(field_str(case, "rhs"), case_id, "rhs");
                let roots = case
                    .get("claimed_roots")
                    .and_then(Json::as_array)
                    .expect("missing roots");
                let roots: Vec<f64> = roots
                    .iter()
                    .map(|root| root.as_number().expect("root is not numeric"))
                    .collect();
                must_verify(
                    verify_equation_solution(
                        &Equation::new(lhs.clone(), rhs.clone()),
                        &roots,
                        field_str(case, "var"),
                    ),
                    case_id,
                    "equation",
                );
                assert!(
                    proof_call.starts_with("solve(Eq("),
                    "{case_id}: unexpected SymPy equation check: {proof_call}"
                );
                assert!(
                    proof_call.contains(&field_str(case, "lhs").replace('^', "**")),
                    "{case_id}: SymPy call does not contain the equation"
                );
                let recorded_roots = proof_result
                    .trim_matches(['[', ']'])
                    .split(',')
                    .map(str::trim)
                    .collect::<Vec<_>>();
                let claimed_roots = roots
                    .iter()
                    .map(|root| format!("{}", *root as i64))
                    .collect::<Vec<_>>();
                assert_eq!(
                    recorded_roots, claimed_roots,
                    "{case_id}: recorded SymPy roots differ from claimed roots"
                );
                equations += 1;
            }
            "identity" => {
                assert!(
                    proof_call.starts_with("simplify("),
                    "{case_id}: unexpected SymPy identity check: {proof_call}"
                );
                assert!(
                    proof_call.contains(&field_str(case, "lhs").replace('^', "**")),
                    "{case_id}: SymPy call does not contain identity LHS"
                );
                assert_eq!(
                    proof_result, "0",
                    "{case_id}: SymPy reported nonzero identity residual"
                );
                let lhs = parse_expr(field_str(case, "lhs"), case_id, "lhs");
                let rhs = parse_expr(field_str(case, "rhs"), case_id, "rhs");
                let vars = case
                    .get("vars")
                    .and_then(Json::as_array)
                    .expect("missing variables");
                let vars: Vec<&str> = vars
                    .iter()
                    .map(|v| v.as_str().expect("variable is not a string"))
                    .collect();
                must_verify(verify_identity(&lhs, &rhs, &vars), case_id, "identity");
                identities += 1;
            }
            "factorisation" => {
                assert!(
                    proof_call.starts_with("simplify("),
                    "{case_id}: unexpected SymPy factorisation check: {proof_call}"
                );
                assert!(
                    proof_call.contains(&field_str(case, "original").replace('^', "**")),
                    "{case_id}: SymPy call does not contain original polynomial"
                );
                assert_eq!(
                    proof_result, "0",
                    "{case_id}: SymPy reported nonzero factorisation residual"
                );
                let original = parse_expr(field_str(case, "original"), case_id, "original");
                let factors = case
                    .get("claimed_factors")
                    .and_then(Json::as_array)
                    .expect("missing factors");
                let factors: Vec<Ast> = factors
                    .iter()
                    .map(|factor| {
                        parse_expr(
                            factor.as_str().expect("factor is not a string"),
                            case_id,
                            "factor",
                        )
                    })
                    .collect();
                must_verify(
                    verify_factorisation(&original, &factors),
                    case_id,
                    "factorisation",
                );
                factorisations += 1;
            }
            other => panic!("{case_id}: unsupported case kind {other:?}"),
        }

        let wrong_answers = wrong_row
            .get("wrong_answers")
            .and_then(Json::as_array)
            .expect("missing wrong answers");
        for wrong in wrong_answers {
            let wrong_proof = wrong
                .get("sympy_verification")
                .expect("missing wrong-answer SymPy proof");
            assert_eq!(
                wrong_proof.get("verified"),
                Some(&Json::Bool(false)),
                "{case_id}: wrong answer lacks negative SymPy result"
            );
            let wrong_call = wrong_proof
                .get("call")
                .and_then(Json::as_str)
                .unwrap_or_else(|| panic!("{case_id}: wrong answer missing recorded SymPy call"));
            let wrong_result = field_str(wrong_proof, "result");
            assert_ne!(
                wrong_result, "0",
                "{case_id}: wrong claim has zero SymPy residual"
            );
            let wrong_value = match kind {
                "integral" => field_str(wrong, "claimed_antiderivative"),
                "identity" => field_str(wrong, "rhs"),
                _ => "",
            };
            if !wrong_value.is_empty() {
                assert!(
                    wrong_call.contains(&wrong_value.replace('^', "**")),
                    "{case_id}: SymPy wrong-answer call omits the mutant"
                );
            }
            assert!(!wrong_call.is_empty());
            assert!(wrong_proof.get("verified") == Some(&Json::Bool(false)));
            match kind {
                "integral" => {
                    let integrand = parse_expr(field_str(case, "integrand"), case_id, "integrand");
                    let mutant = parse_expr(
                        field_str(wrong, "claimed_antiderivative"),
                        case_id,
                        "wrong antiderivative",
                    );
                    must_reject(
                        verify_integral(&integrand, &mutant, field_str(case, "var")),
                        case_id,
                        "integral mutant",
                    );
                }
                "equation" => {
                    let lhs = parse_expr(field_str(case, "lhs"), case_id, "lhs");
                    let rhs = parse_expr(field_str(case, "rhs"), case_id, "rhs");
                    let roots = wrong
                        .get("claimed_roots")
                        .and_then(Json::as_array)
                        .expect("missing mutant roots");
                    let roots: Vec<f64> = roots
                        .iter()
                        .map(|root| root.as_number().expect("root is not numeric"))
                        .collect();
                    must_reject(
                        verify_equation_solution(
                            &Equation::new(lhs, rhs),
                            &roots,
                            field_str(case, "var"),
                        ),
                        case_id,
                        "equation mutant",
                    );
                }
                "identity" => {
                    let lhs = parse_expr(field_str(case, "lhs"), case_id, "lhs");
                    let rhs = parse_expr(field_str(wrong, "rhs"), case_id, "wrong rhs");
                    let vars = case
                        .get("vars")
                        .and_then(Json::as_array)
                        .expect("missing variables");
                    let vars: Vec<&str> = vars
                        .iter()
                        .map(|v| v.as_str().expect("variable is not a string"))
                        .collect();
                    must_reject(
                        verify_identity(&lhs, &rhs, &vars),
                        case_id,
                        "identity mutant",
                    );
                }
                "factorisation" => {
                    let original = parse_expr(field_str(case, "original"), case_id, "original");
                    let factors = wrong
                        .get("claimed_factors")
                        .and_then(Json::as_array)
                        .expect("missing mutant factors");
                    let factors: Vec<Ast> = factors
                        .iter()
                        .map(|factor| {
                            parse_expr(
                                factor.as_str().expect("factor is not a string"),
                                case_id,
                                "mutant factor",
                            )
                        })
                        .collect();
                    must_reject(
                        verify_factorisation(&original, &factors),
                        case_id,
                        "factorisation mutant",
                    );
                }
                _ => unreachable!(),
            }
            let _recorded_independent_result = wrong_result;
            mutant_count += 1;
        }
    }

    assert_eq!(
        (integrals, equations, identities, factorisations),
        (125, 125, 125, 125)
    );
    assert_eq!(mutant_count, 1000);
    println!("B6 corpus: 500/500 SymPy-confirmed claims accepted ({integrals} integrals, {equations} equations, {identities} identities, {factorisations} factorizations)");
    println!("B6 adversarial arm: {mutant_count}/1000 SymPy-disproved claims rejected with numeric counterexamples");
}
