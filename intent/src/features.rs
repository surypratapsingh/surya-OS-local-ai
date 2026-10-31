//! Tokens and hashed features (intent/README.md, "Tokens" and "Features").

/// FNV-1a, 32 bits: offset basis 2166136261, prime 16777619
/// (http://www.isthe.com/chongo/tech/comp/fnv/ and draft-eastlake-fnv).
pub fn fnv1a32(bytes: &[u8]) -> u32 {
    let mut hash: u32 = 0x811c_9dc5;
    for &byte in bytes {
        hash ^= u32::from(byte);
        hash = hash.wrapping_mul(0x0100_0193);
    }
    hash
}

/// ASCII lower-case, then maximal runs of `a-z` and `0-9`. Every other
/// character, including every non-ASCII character, separates tokens.
pub fn tokens(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    for character in text.chars() {
        let lowered = character.to_ascii_lowercase();
        if lowered.is_ascii_lowercase() || lowered.is_ascii_digit() {
            current.push(lowered);
        } else if !current.is_empty() {
            out.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

/// The feature strings of one token: the whole word, then every character
/// trigram of `<token>` in order.
pub fn token_features(token: &str) -> Vec<String> {
    let mut features = vec![format!("w:{token}")];
    let marked: Vec<char> = format!("<{token}>").chars().collect();
    for window in marked.windows(3) {
        let trigram: String = window.iter().collect();
        features.push(format!("t:{trigram}"));
    }
    features
}

/// Feature string of the summary position 0.
pub const CLS_FEATURE: &str = "c:cls";

/// Bucket ids for each position: position 0 is the summary token, then one
/// position per token. Returns None when the tokens do not fit in `max_len`.
pub fn encode(text: &str, buckets: usize, max_len: usize) -> Option<Vec<Vec<u32>>> {
    let words = tokens(text);
    if words.len() + 1 > max_len {
        return None;
    }
    let bucket = |feature: &str| fnv1a32(feature.as_bytes()) % buckets as u32;
    let mut positions = vec![vec![bucket(CLS_FEATURE)]];
    for word in &words {
        positions.push(token_features(word).iter().map(|f| bucket(f)).collect());
    }
    Some(positions)
}
