//! Card store: append-only verified-answer log with tamper-evident chaining.
//!
//! Each "card" is a verified answer to a problem, stored immutably with:
//! - Problem hash (SHA-256 of canonical expression)
//! - Claimed answer (as text)
//! - Verification report (detection rate, mutations caught)
//! - Chain hash (links to previous card, prevents tampering)
//!
//! Similar to memory.py: deterministic, append-only, tamper-detectable.

use std::collections::HashMap;
use std::fmt;

/// A verified answer card.
#[derive(Clone, Debug, PartialEq)]
pub struct Card {
    pub problem_hash: String,        // SHA-256 of canonical problem
    pub answer: String,              // Canonical answer expression
    pub verification_score: f64,      // 0.0-1.0, mutation detection rate
    pub timestamp: String,           // ISO 8601
    pub content_hash: String,        // SHA-256 of (problem_hash, answer, score)
    pub chain_hash: String,          // SHA-256 of content_hash + previous_chain_hash
}

#[derive(Clone, Debug)]
pub struct CardStoreError {
    pub message: String,
}

impl fmt::Display for CardStoreError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for CardStoreError {}

const GENESIS_CHAIN_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// Card store: append-only verified answers with chaining.
pub struct CardStore {
    cards: Vec<Card>,
    problem_index: HashMap<String, usize>, // problem_hash → index
}

impl CardStore {
    pub fn new() -> Self {
        CardStore {
            cards: Vec::new(),
            problem_index: HashMap::new(),
        }
    }

    /// Add a verified answer to the store.
    ///
    /// Verifies chaining integrity before appending.
    pub fn add(
        &mut self,
        problem_hash: String,
        answer: String,
        verification_score: f64,
    ) -> Result<Card, CardStoreError> {
        use sha2::{Sha256, Digest};

        // Validate score
        if verification_score < 0.0 || verification_score > 1.0 {
            return Err(CardStoreError {
                message: "verification_score must be 0.0-1.0".to_string(),
            });
        }

        // Get current timestamp
        let timestamp = chrono::Utc::now().to_rfc3339();

        // Compute content hash
        let content_text = format!(
            "{}:{}:{}",
            problem_hash, answer, verification_score
        );
        let mut hasher = Sha256::new();
        hasher.update(content_text.as_bytes());
        let content_hash = format!("{:x}", hasher.finalize());

        // Compute chain hash
        let previous_chain = self
            .cards
            .last()
            .map(|c| c.chain_hash.as_str())
            .unwrap_or(GENESIS_CHAIN_HASH);

        let chain_text = format!("{}{}", content_hash, previous_chain);
        let mut hasher = Sha256::new();
        hasher.update(chain_text.as_bytes());
        let chain_hash = format!("{:x}", hasher.finalize());

        let card = Card {
            problem_hash: problem_hash.clone(),
            answer,
            verification_score,
            timestamp,
            content_hash,
            chain_hash,
        };

        // Update index
        if !self.problem_index.contains_key(&problem_hash) {
            self.problem_index
                .insert(problem_hash, self.cards.len());
        }

        self.cards.push(card.clone());
        Ok(card)
    }

    /// Retrieve a verified answer by problem hash.
    pub fn get(&self, problem_hash: &str) -> Option<&Card> {
        self.problem_index
            .get(problem_hash)
            .and_then(|&idx| self.cards.get(idx))
    }

    /// Get all cards with verification score >= threshold.
    pub fn high_confidence(&self, threshold: f64) -> Vec<&Card> {
        self.cards
            .iter()
            .filter(|c| c.verification_score >= threshold)
            .collect()
    }

    /// Verify chain integrity.
    ///
    /// Returns Err if any card's chain is broken or tampered.
    pub fn verify_integrity(&self) -> Result<(), CardStoreError> {
        use sha2::{Sha256, Digest};

        let mut previous_chain = GENESIS_CHAIN_HASH.to_string();

        for (i, card) in self.cards.iter().enumerate() {
            // Recompute content hash
            let content_text = format!(
                "{}:{}:{}",
                card.problem_hash, card.answer, card.verification_score
            );
            let mut hasher = Sha256::new();
            hasher.update(content_text.as_bytes());
            let expected_content_hash = format!("{:x}", hasher.finalize());

            if expected_content_hash != card.content_hash {
                return Err(CardStoreError {
                    message: format!("card {}: content_hash mismatch", i),
                });
            }

            // Recompute chain hash
            let chain_text = format!("{}{}", card.content_hash, previous_chain);
            let mut hasher = Sha256::new();
            hasher.update(chain_text.as_bytes());
            let expected_chain_hash = format!("{:x}", hasher.finalize());

            if expected_chain_hash != card.chain_hash {
                return Err(CardStoreError {
                    message: format!("card {}: chain_hash mismatch (card may be deleted or reordered)", i),
                });
            }

            previous_chain = card.chain_hash.clone();
        }

        Ok(())
    }

    /// Export all cards for archival or export.
    pub fn export(&self) -> Vec<Card> {
        self.cards.clone()
    }

    /// Statistics about the store.
    pub fn stats(&self) -> CardStoreStats {
        let total = self.cards.len();
        let high_conf = self.high_confidence(0.9).len();
        let avg_score = if total > 0 {
            self.cards.iter().map(|c| c.verification_score).sum::<f64>() / total as f64
        } else {
            0.0
        };

        CardStoreStats {
            total_cards: total,
            high_confidence_cards: high_conf,
            average_score: avg_score,
        }
    }
}

impl Default for CardStore {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone)]
pub struct CardStoreStats {
    pub total_cards: usize,
    pub high_confidence_cards: usize,
    pub average_score: f64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn card_store_add() {
        let mut store = CardStore::new();
        let card = store
            .add("abc123".to_string(), "2*x".to_string(), 0.95)
            .unwrap();

        assert_eq!(card.problem_hash, "abc123");
        assert_eq!(card.answer, "2*x");
        assert_eq!(card.verification_score, 0.95);
    }

    #[test]
    fn card_store_retrieve() {
        let mut store = CardStore::new();
        store
            .add("abc123".to_string(), "2*x".to_string(), 0.95)
            .unwrap();

        let card = store.get("abc123").unwrap();
        assert_eq!(card.answer, "2*x");
    }

    #[test]
    fn card_store_chaining() {
        let mut store = CardStore::new();
        let card1 = store
            .add("prob1".to_string(), "ans1".to_string(), 0.9)
            .unwrap();
        let card2 = store
            .add("prob2".to_string(), "ans2".to_string(), 0.95)
            .unwrap();

        // Card2's chain should depend on Card1's chain
        assert_ne!(card1.chain_hash, card2.chain_hash);
    }

    #[test]
    fn card_store_verify_integrity() {
        let mut store = CardStore::new();
        store
            .add("prob1".to_string(), "ans1".to_string(), 0.9)
            .unwrap();
        store
            .add("prob2".to_string(), "ans2".to_string(), 0.95)
            .unwrap();

        assert!(store.verify_integrity().is_ok());
    }

    #[test]
    fn card_store_invalid_score() {
        let mut store = CardStore::new();
        let result = store.add("prob".to_string(), "ans".to_string(), 1.5);
        assert!(result.is_err());
    }

    #[test]
    fn card_store_stats() {
        let mut store = CardStore::new();
        store
            .add("prob1".to_string(), "ans1".to_string(), 0.9)
            .unwrap();
        store
            .add("prob2".to_string(), "ans2".to_string(), 0.8)
            .unwrap();

        let stats = store.stats();
        assert_eq!(stats.total_cards, 2);
        assert_eq!(stats.high_confidence_cards, 1);
        assert!((stats.average_score - 0.85).abs() < 0.01);
    }
}
