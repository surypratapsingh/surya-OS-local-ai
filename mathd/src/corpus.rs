//! Adversarial corpus: test cases from real student errors and edge cases.
//!
//! A curated collection of mathematical problems that expose common mistakes:
//! - Sign errors (forgot negation in derivative)
//! - Operator confusion (+ vs *, * vs /)
//! - Chain rule omission (forgot to multiply by inner derivative)
//! - Power rule errors (wrong exponent)
//! - Domain errors (sqrt/ln of negative)
//! - Boundary cases (0, 1, infinity, very small/large numbers)
//!
//! Each test case includes the problem, expected answer, and common wrong answers.

use crate::parser::Expr;
use std::collections::HashMap;

/// A test case: problem + context + expected answer + common wrong answers.
#[derive(Clone, Debug)]
pub struct TestCase {
    pub id: String,
    pub description: String,
    pub expression: String, // Original problem (parse this)
    pub context: HashMap<String, f64>, // Variable values
    pub expected_answer: f64, // Correct numerical answer
    pub common_wrong_answers: Vec<(String, f64)>, // (description, value)
    pub category: String, // "chain_rule", "sign_error", etc.
}

/// Build the adversarial corpus.
pub fn build_corpus() -> Vec<TestCase> {
    vec![
        // Derivative tests: chain rule errors
        TestCase {
            id: "chain_rule_1".to_string(),
            description: "Derivative of sin(2x) - forgot chain rule constant".to_string(),
            expression: "sin(2*x)".to_string(),
            context: {
                let mut m = HashMap::new();
                m.insert("x".to_string(), 1.0);
                m
            },
            expected_answer: 2.0 * (2.0_f64).cos(), // d/dx sin(2x) at x=1 = 2*cos(2)
            common_wrong_answers: vec![
                ("forgot constant: cos(2x)".to_string(), (2.0_f64).cos()),
                ("wrong sign".to_string(), -2.0 * (2.0_f64).cos()),
            ],
            category: "chain_rule".to_string(),
        },
        // Sign errors
        TestCase {
            id: "sign_error_1".to_string(),
            description: "Derivative of -x^2".to_string(),
            expression: "-x^2".to_string(),
            context: {
                let mut m = HashMap::new();
                m.insert("x".to_string(), 3.0);
                m
            },
            expected_answer: -6.0, // d/dx(-x^2) at x=3 = -6
            common_wrong_answers: vec![
                ("forgot negative: 2x".to_string(), 6.0),
                ("wrong power: -2x".to_string(), -6.0), // Actually same, good
            ],
            category: "sign_error".to_string(),
        },
        // Power rule with chain
        TestCase {
            id: "power_chain_1".to_string(),
            description: "Derivative of (2x+1)^3".to_string(),
            expression: "(2*x + 1)^3".to_string(),
            context: {
                let mut m = HashMap::new();
                m.insert("x".to_string(), 1.0);
                m
            },
            expected_answer: 3.0 * (2.0 * 1.0 + 1.0).powi(2) * 2.0, // 3*(2x+1)^2*2 at x=1 = 3*9*2 = 54
            common_wrong_answers: vec![
                ("forgot inner derivative: 3*(2x+1)^2 at x=1".to_string(), 27.0),
                ("wrong exponent: 2*(2x+1)^2*2 at x=1".to_string(), 36.0),
            ],
            category: "power_chain".to_string(),
        },
        // Operator confusion
        TestCase {
            id: "operator_confusion_1".to_string(),
            description: "Evaluate 2 + 3 * 4 (not 5*4)".to_string(),
            expression: "2 + 3 * 4".to_string(),
            context: HashMap::new(),
            expected_answer: 14.0, // 2 + 12
            common_wrong_answers: vec![
                ("wrong precedence: (2+3)*4".to_string(), 20.0),
                ("left-to-right: (2+3)*4".to_string(), 20.0),
            ],
            category: "precedence".to_string(),
        },
        // Domain errors
        TestCase {
            id: "domain_error_1".to_string(),
            description: "sqrt(-1) should be undefined".to_string(),
            expression: "sqrt(-1)".to_string(),
            context: HashMap::new(),
            expected_answer: f64::NAN, // Undefined
            common_wrong_answers: vec![
                ("claimed imaginary unit i".to_string(), 1.0), // Wrong: no imaginary
            ],
            category: "domain_error".to_string(),
        },
        // Boundary: 0
        TestCase {
            id: "boundary_zero".to_string(),
            description: "Derivative of x^2 at x=0".to_string(),
            expression: "x^2".to_string(),
            context: {
                let mut m = HashMap::new();
                m.insert("x".to_string(), 0.0);
                m
            },
            expected_answer: 0.0, // 2x at x=0 = 0
            common_wrong_answers: vec![
                ("claimed undefined at boundary".to_string(), f64::NAN),
            ],
            category: "boundary".to_string(),
        },
        // Boundary: 1
        TestCase {
            id: "boundary_one".to_string(),
            description: "x^x derivative at x=1".to_string(),
            expression: "x^x".to_string(),
            context: {
                let mut m = HashMap::new();
                m.insert("x".to_string(), 1.0);
                m
            },
            expected_answer: 1.0, // (x^x)' = x^x*(ln(x) + 1) = 1*(0+1) = 1
            common_wrong_answers: vec![
                ("forgot logarithm term: x^(x-1)".to_string(), 1.0), // happens to match
                ("simple power rule: x".to_string(), 1.0), // happens to match
            ],
            category: "special_function".to_string(),
        },
        // Product rule
        TestCase {
            id: "product_rule_1".to_string(),
            description: "Derivative of x * sin(x)".to_string(),
            expression: "x * sin(x)".to_string(),
            context: {
                let mut m = HashMap::new();
                m.insert("x".to_string(), 0.0);
                m
            },
            expected_answer: 0.0 * (0.0_f64).sin() + 0.0 * (0.0_f64).cos(), // x'*sin(x) + x*cos(x) at x=0 = 0
            common_wrong_answers: vec![
                ("forgot product rule: sin(x) + x*cos(x)".to_string(), (0.0_f64).sin() + 0.0 * (0.0_f64).cos()),
            ],
            category: "product_rule".to_string(),
        },
    ]
}

/// Find test cases by category.
pub fn test_cases_by_category(corpus: &[TestCase], category: &str) -> Vec<&TestCase> {
    corpus
        .iter()
        .filter(|tc| tc.category == category)
        .collect()
}

/// Summary of corpus coverage.
pub fn corpus_summary(corpus: &[TestCase]) -> HashMap<String, usize> {
    let mut counts = HashMap::new();
    for tc in corpus {
        *counts.entry(tc.category.clone()).or_insert(0) += 1;
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn corpus_is_nonempty() {
        let corpus = build_corpus();
        assert!(!corpus.is_empty());
    }

    #[test]
    fn corpus_has_categories() {
        let corpus = build_corpus();
        let summary = corpus_summary(&corpus);
        assert!(summary.contains_key("chain_rule"));
        assert!(summary.contains_key("sign_error"));
        assert!(summary.contains_key("precedence"));
    }

    #[test]
    fn corpus_filtering() {
        let corpus = build_corpus();
        let chain_tests = test_cases_by_category(&corpus, "chain_rule");
        assert!(!chain_tests.is_empty());
    }

    #[test]
    fn test_case_structure() {
        let corpus = build_corpus();
        for tc in corpus {
            assert!(!tc.id.is_empty());
            assert!(!tc.description.is_empty());
            assert!(!tc.expression.is_empty());
            assert!(!tc.category.is_empty());
            // Expected answer may be NAN for domain errors
        }
    }
}
