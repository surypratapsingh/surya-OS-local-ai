//! Append-only, hash-chained JSONL mistake ledger (work order B7).
//!
//! Records one line per Failed claim:
//!   {"sequence": N, "timestamp": T, "claim": "...", "counterexample": {...}, "prev_hash": "...", "hash": "..."}
//!
//! Strictly std-only with zero external dependencies.
//! Uses FIPS 180-4 SHA-256 to cryptographically bind each entry to the previous.
//! Uses handwritten JSON escaping and unescaping to handle quotes, backslashes,
//! newlines, tabs, and unicode escapes.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

pub const GENESIS_PREV_HASH: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

// ---------------------------------------------------------------------------
// Pure std-only FIPS 180-4 SHA-256
// ---------------------------------------------------------------------------

const K256: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

pub struct Sha256 {
    h: [u32; 8],
    total_len: u64,
    buf: [u8; 64],
    buf_len: usize,
}

impl Default for Sha256 {
    fn default() -> Self {
        Self::new()
    }
}

impl Sha256 {
    pub fn new() -> Self {
        Self {
            h: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            total_len: 0,
            buf: [0; 64],
            buf_len: 0,
        }
    }

    fn compress(h: &mut [u32; 8], block: &[u8; 64]) {
        let mut w = [0u32; 64];
        for (i, chunk) in block.as_chunks::<4>().0.iter().enumerate() {
            w[i] = u32::from_be_bytes(*chunk);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K256[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);

            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    pub fn update(&mut self, mut data: &[u8]) {
        self.total_len += data.len() as u64;
        if self.buf_len > 0 {
            let to_copy = (64 - self.buf_len).min(data.len());
            self.buf[self.buf_len..self.buf_len + to_copy].copy_from_slice(&data[..to_copy]);
            self.buf_len += to_copy;
            data = &data[to_copy..];
            if self.buf_len == 64 {
                let block = self.buf;
                Self::compress(&mut self.h, &block);
                self.buf_len = 0;
            }
        }
        while data.len() >= 64 {
            let block: [u8; 64] = data[..64].try_into().unwrap();
            Self::compress(&mut self.h, &block);
            data = &data[64..];
        }
        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buf_len = data.len();
        }
    }

    pub fn finalize(mut self) -> [u8; 32] {
        let bit_len = self.total_len * 8;
        self.buf[self.buf_len] = 0x80;
        self.buf_len += 1;
        if self.buf_len > 56 {
            self.buf[self.buf_len..64].fill(0);
            let block = self.buf;
            Self::compress(&mut self.h, &block);
            self.buf_len = 0;
        }
        self.buf[self.buf_len..56].fill(0);
        self.buf[56..64].copy_from_slice(&bit_len.to_be_bytes());
        let block = self.buf;
        Self::compress(&mut self.h, &block);

        let mut out = [0u8; 32];
        for (i, word) in self.h.iter().enumerate() {
            out[i * 4..(i + 1) * 4].copy_from_slice(&word.to_be_bytes());
        }
        out
    }
}

pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write;
        let _ = write!(hex, "{:02x}", byte);
    }
    hex
}

// ---------------------------------------------------------------------------
// Handwritten JSON Escaping and Unescaping (zero external crates)
// ---------------------------------------------------------------------------

pub fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 16);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

pub fn unescape_json(s: &str) -> Result<String, String> {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some('/') => out.push('/'),
                Some('b') => out.push('\x08'),
                Some('f') => out.push('\x0c'),
                Some('n') => out.push('\n'),
                Some('r') => out.push('\r'),
                Some('t') => out.push('\t'),
                Some('u') => {
                    let mut hex = String::with_capacity(4);
                    for _ in 0..4 {
                        if let Some(h) = chars.next() {
                            hex.push(h);
                        } else {
                            return Err("truncated \\u escape".to_string());
                        }
                    }
                    let code = u32::from_str_radix(&hex, 16)
                        .map_err(|e| format!("invalid hex in \\u: {}", e))?;
                    let ch = char::from_u32(code)
                        .ok_or_else(|| format!("invalid unicode code point: {:x}", code))?;
                    out.push(ch);
                }
                Some(other) => return Err(format!("invalid escape character: \\{}", other)),
                None => return Err("trailing backslash in string".to_string()),
            }
        } else {
            out.push(c);
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Counterexample and Ledger Data Structures
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct CounterexampleRecord {
    pub point: BTreeMap<String, f64>,
    pub expected: f64,
    pub got: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LedgerEntry {
    pub sequence: u64,
    pub timestamp: u64,
    pub claim: String,
    pub counterexample: CounterexampleRecord,
    pub prev_hash: String,
    pub hash: String,
}

impl LedgerEntry {
    /// Compute cryptographic hash for this entry.
    pub fn compute_hash(
        sequence: u64,
        timestamp: u64,
        claim: &str,
        ce: &CounterexampleRecord,
        prev_hash: &str,
    ) -> String {
        let mut s = String::with_capacity(256);
        s.push_str(&format!(
            "seq:{}|ts:{}|claim:{}|exp:{:.10}|got:{:.10}|prev:{}|pt:[",
            sequence, timestamp, claim, ce.expected, ce.got, prev_hash
        ));
        for (k, v) in &ce.point {
            s.push_str(&format!("{}:{:.10},", k, v));
        }
        s.push(']');
        sha256_hex(s.as_bytes())
    }

    /// Serialise entry to single JSON line without external dependencies.
    pub fn to_json_line(&self) -> String {
        let mut s = String::with_capacity(256);
        s.push('{');
        s.push_str(&format!("\"sequence\":{},", self.sequence));
        s.push_str(&format!("\"timestamp\":{},", self.timestamp));
        s.push_str(&format!("\"claim\":\"{}\",", escape_json(&self.claim)));
        s.push_str("\"counterexample\":{\"point\":{");
        let mut first = true;
        for (k, v) in &self.counterexample.point {
            if !first {
                s.push(',');
            }
            first = false;
            s.push_str(&format!("\"{}\":{:.10}", escape_json(k), v));
        }
        s.push_str(&format!(
            "}},\"expected\":{:.10},\"got\":{:.10}}},",
            self.counterexample.expected, self.counterexample.got
        ));
        s.push_str(&format!("\"prev_hash\":\"{}\",", self.prev_hash));
        s.push_str(&format!("\"hash\":\"{}\"", self.hash));
        s.push('}');
        s
    }

    /// Parse single JSON line into a LedgerEntry.
    pub fn from_json_line(line: &str) -> Result<Self, String> {
        let trimmed = line.trim();
        if !trimmed.starts_with('{') || !trimmed.ends_with('}') {
            return Err("entry must start with '{' and end with '}'".to_string());
        }
        let inner = &trimmed[1..trimmed.len() - 1];

        let sequence = extract_json_u64(inner, "sequence")?;
        let timestamp = extract_json_u64(inner, "timestamp")?;
        let claim_raw = extract_json_string(inner, "claim")?;
        let claim = unescape_json(&claim_raw)?;
        let prev_hash = extract_json_string(inner, "prev_hash")?;
        let hash = extract_json_string(inner, "hash")?;

        let ce_obj = extract_json_object(inner, "counterexample")?
            .ok_or_else(|| "missing 'counterexample' object".to_string())?;

        let expected = extract_json_f64(&ce_obj, "expected")?;
        let got = extract_json_f64(&ce_obj, "got")?;
        let mut point = BTreeMap::new();

        if let Some(pt_str) = extract_json_object(&ce_obj, "point")? {
            for pair in pt_str.split(',') {
                let pair = pair.trim();
                if pair.is_empty() {
                    continue;
                }
                if let Some((k, v)) = pair.split_once(':') {
                    let k_unquoted = unquote_str(k.trim())?;
                    let key = unescape_json(&k_unquoted)?;
                    let val: f64 = v
                        .trim()
                        .parse()
                        .map_err(|e| format!("invalid float in point: {}", e))?;
                    point.insert(key, val);
                }
            }
        }

        let counterexample = CounterexampleRecord {
            point,
            expected,
            got,
        };

        Ok(Self {
            sequence,
            timestamp,
            claim,
            counterexample,
            prev_hash,
            hash,
        })
    }
}

// ---------------------------------------------------------------------------
// Minimal JSON Line Token Extractors
// ---------------------------------------------------------------------------

fn extract_json_u64(src: &str, key: &str) -> Result<u64, String> {
    let target = format!("\"{}\":", key);
    let idx = src
        .find(&target)
        .ok_or_else(|| format!("key '{}' not found", key))?;
    let rem = &src[idx + target.len()..].trim_start();
    let end = rem
        .find(|c: char| c == ',' || c == '}' || c.is_whitespace())
        .unwrap_or(rem.len());
    rem[..end]
        .parse::<u64>()
        .map_err(|e| format!("bad u64 for key '{}': {}", key, e))
}

fn extract_json_f64(src: &str, key: &str) -> Result<f64, String> {
    let target = format!("\"{}\":", key);
    let idx = src
        .find(&target)
        .ok_or_else(|| format!("key '{}' not found", key))?;
    let rem = &src[idx + target.len()..].trim_start();
    let end = rem
        .find(|c: char| c == ',' || c == '}' || c.is_whitespace())
        .unwrap_or(rem.len());
    rem[..end]
        .parse::<f64>()
        .map_err(|e| format!("bad f64 for key '{}': {}", key, e))
}

fn extract_json_string(src: &str, key: &str) -> Result<String, String> {
    let target = format!("\"{}\":", key);
    let idx = src
        .find(&target)
        .ok_or_else(|| format!("key '{}' not found", key))?;
    let rem = &src[idx + target.len()..].trim_start();
    if !rem.starts_with('"') {
        return Err(format!("key '{}' does not start with string quote", key));
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
    let end = end_idx.ok_or_else(|| format!("unterminated string for key '{}'", key))?;
    Ok(after_quote[..end].to_string())
}

fn extract_json_object(src: &str, key: &str) -> Result<Option<String>, String> {
    let target = format!("\"{}\":", key);
    let idx = match src.find(&target) {
        Some(i) => i,
        None => return Ok(None),
    };
    let rem = &src[idx + target.len()..].trim_start();
    if !rem.starts_with('{') {
        return Ok(None);
    }
    let mut depth = 0;
    let mut in_str = false;
    let mut escaped = false;
    let mut start_idx = None;
    let mut end_idx = None;

    for (i, c) in rem.char_indices() {
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
                start_idx = Some(i + 1);
            }
            depth += 1;
        } else if c == '}' {
            depth -= 1;
            if depth == 0 {
                end_idx = Some(i);
                break;
            }
        }
    }

    match (start_idx, end_idx) {
        (Some(s), Some(e)) => Ok(Some(rem[s..e].to_string())),
        _ => Err(format!("unmatched brackets in object for key '{}'", key)),
    }
}

fn unquote_str(s: &str) -> Result<String, String> {
    let trimmed = s.trim();
    if trimmed.starts_with('"') && trimmed.ends_with('"') && trimmed.len() >= 2 {
        Ok(trimmed[1..trimmed.len() - 1].to_string())
    } else {
        Err(format!("not a quoted string: {}", s))
    }
}

// ---------------------------------------------------------------------------
// Mistake Ledger Manager
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq)]
pub enum LedgerVerifyError {
    Io(String),
    MalformedJson(usize, String),
    InvalidSequence {
        line: usize,
        expected: u64,
        got: u64,
    },
    HashMismatch {
        line: usize,
        expected: String,
        got: String,
    },
    ChainBroken {
        line: usize,
        expected_prev: String,
        got_prev: String,
    },
}

impl std::fmt::Display for LedgerVerifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LedgerVerifyError::Io(e) => write!(f, "ledger I/O error: {}", e),
            LedgerVerifyError::MalformedJson(l, e) => {
                write!(f, "malformed JSON at line {}: {}", l, e)
            }
            LedgerVerifyError::InvalidSequence {
                line,
                expected,
                got,
            } => write!(
                f,
                "invalid sequence at line {}: expected {}, got {}",
                line, expected, got
            ),
            LedgerVerifyError::HashMismatch {
                line,
                expected,
                got,
            } => write!(
                f,
                "hash mismatch at line {}: expected {}, got {}",
                line, expected, got
            ),
            LedgerVerifyError::ChainBroken {
                line,
                expected_prev,
                got_prev,
            } => write!(
                f,
                "chain broken at line {}: expected prev_hash {}, got {}",
                line, expected_prev, got_prev
            ),
        }
    }
}

pub struct MistakeLedger {
    path: PathBuf,
    last_sequence: u64,
    last_hash: String,
}

impl MistakeLedger {
    /// Open existing mistake ledger or initialize new one at given path.
    pub fn open<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let path = path.as_ref().to_path_buf();
        let mut last_sequence = 0u64;
        let mut last_hash = GENESIS_PREV_HASH.to_string();

        if path.exists() {
            let file = File::open(&path)?;
            let reader = BufReader::new(file);
            for (idx, line_res) in reader.lines().enumerate() {
                let line = line_res?;
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                match LedgerEntry::from_json_line(trimmed) {
                    Ok(entry) => {
                        last_sequence = entry.sequence;
                        last_hash = entry.hash;
                    }
                    Err(e) => {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            format!("ledger corruption at line {}: {}", idx + 1, e),
                        ));
                    }
                }
            }
        }

        Ok(Self {
            path,
            last_sequence,
            last_hash,
        })
    }

    pub fn last_sequence(&self) -> u64 {
        self.last_sequence
    }

    pub fn last_hash(&self) -> &str {
        &self.last_hash
    }

    /// Record a single failed claim to the mistake ledger.
    /// Strictly writes ONE line per Failed claim.
    pub fn record_failed(
        &mut self,
        claim: &str,
        counterexample: &CounterexampleRecord,
    ) -> io::Result<LedgerEntry> {
        let seq = self.last_sequence + 1;
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);

        let prev = self.last_hash.clone();
        let hash = LedgerEntry::compute_hash(seq, ts, claim, counterexample, &prev);

        let entry = LedgerEntry {
            sequence: seq,
            timestamp: ts,
            claim: claim.to_string(),
            counterexample: counterexample.clone(),
            prev_hash: prev,
            hash: hash.clone(),
        };

        if let Some(parent) = self.path.parent() {
            if !parent.as_os_str().is_empty() && !parent.exists() {
                std::fs::create_dir_all(parent)?;
            }
        }

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;

        let line = entry.to_json_line();
        writeln!(file, "{}", line)?;
        file.flush()?;

        self.last_sequence = seq;
        self.last_hash = hash;

        Ok(entry)
    }

    /// Read all entries currently in the ledger.
    pub fn read_entries(&self) -> io::Result<Vec<LedgerEntry>> {
        if !self.path.exists() {
            return Ok(Vec::new());
        }
        let file = File::open(&self.path)?;
        let reader = BufReader::new(file);
        let mut entries = Vec::new();
        for (idx, line_res) in reader.lines().enumerate() {
            let line = line_res?;
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let entry = LedgerEntry::from_json_line(trimmed).map_err(|e| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("line {}: {}", idx + 1, e),
                )
            })?;
            entries.push(entry);
        }
        Ok(entries)
    }

    /// Cryptographically verify the integrity of the ledger.
    pub fn verify_integrity(&self) -> Result<usize, LedgerVerifyError> {
        if !self.path.exists() {
            return Ok(0);
        }
        let file = File::open(&self.path).map_err(|e| LedgerVerifyError::Io(e.to_string()))?;
        let reader = BufReader::new(file);
        let mut expected_seq = 1u64;
        let mut expected_prev = GENESIS_PREV_HASH.to_string();
        let mut count = 0;

        for (idx, line_res) in reader.lines().enumerate() {
            let line_num = idx + 1;
            let line = line_res.map_err(|e| LedgerVerifyError::Io(e.to_string()))?;
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let entry = LedgerEntry::from_json_line(trimmed)
                .map_err(|e| LedgerVerifyError::MalformedJson(line_num, e))?;

            if entry.sequence != expected_seq {
                return Err(LedgerVerifyError::InvalidSequence {
                    line: line_num,
                    expected: expected_seq,
                    got: entry.sequence,
                });
            }

            if entry.prev_hash != expected_prev {
                return Err(LedgerVerifyError::ChainBroken {
                    line: line_num,
                    expected_prev,
                    got_prev: entry.prev_hash,
                });
            }

            let expected_hash = LedgerEntry::compute_hash(
                entry.sequence,
                entry.timestamp,
                &entry.claim,
                &entry.counterexample,
                &entry.prev_hash,
            );

            if entry.hash != expected_hash {
                return Err(LedgerVerifyError::HashMismatch {
                    line: line_num,
                    expected: expected_hash,
                    got: entry.hash,
                });
            }

            expected_prev = entry.hash.clone();
            expected_seq += 1;
            count += 1;
        }

        Ok(count)
    }
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sha256_known_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"The quick brown fox jumps over the lazy dog"),
            "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592"
        );
    }

    #[test]
    fn test_escaping_roundtrip() {
        let tricky_claim = "identity: \"quotes\" and \\backslash\\ and \nnewline and \ttab == x";
        let escaped = escape_json(tricky_claim);
        assert!(escaped.contains("\\\""));
        assert!(escaped.contains("\\\\"));
        assert!(escaped.contains("\\n"));
        assert!(escaped.contains("\\t"));

        let unescaped = unescape_json(&escaped).unwrap();
        assert_eq!(unescaped, tricky_claim);
    }

    #[test]
    fn test_ledger_append_and_chain_verification() {
        let temp_dir = std::path::PathBuf::from("target/mathd_ledger_test");
        let ledger_path = temp_dir.join("mistakes.jsonl");
        if ledger_path.exists() {
            let _ = std::fs::remove_file(&ledger_path);
        }
        let _ = std::fs::remove_dir_all(&temp_dir);
        std::fs::create_dir_all(&temp_dir).unwrap();

        {
            let mut ledger = MistakeLedger::open(&ledger_path).unwrap();
            assert_eq!(ledger.verify_integrity().unwrap(), 0);

            let mut pt1 = BTreeMap::new();
            pt1.insert("x".to_string(), 0.5);
            let ce1 = CounterexampleRecord {
                point: pt1,
                expected: 2.25,
                got: 1.25,
            };

            let e1 = ledger
                .record_failed("identity: (x+1)^2 == x^2 + 1", &ce1)
                .unwrap();
            assert_eq!(e1.sequence, 1);
            assert_eq!(e1.prev_hash, GENESIS_PREV_HASH);

            let mut pt2 = BTreeMap::new();
            pt2.insert("x".to_string(), 1.0);
            let ce2 = CounterexampleRecord {
                point: pt2,
                expected: 1.0,
                got: 0.0,
            };

            let e2 = ledger
                .record_failed("identity: cos(x)^2 + sin(x)^2 == 0", &ce2)
                .unwrap();
            assert_eq!(e2.sequence, 2);
            assert_eq!(e2.prev_hash, e1.hash);

            assert_eq!(ledger.verify_integrity().unwrap(), 2);
        }

        // Reopen existing ledger
        {
            let ledger = MistakeLedger::open(&ledger_path).unwrap();
            assert_eq!(ledger.last_sequence(), 2);
            assert_eq!(ledger.verify_integrity().unwrap(), 2);

            let entries = ledger.read_entries().unwrap();
            assert_eq!(entries.len(), 2);
            assert_eq!(entries[0].claim, "identity: (x+1)^2 == x^2 + 1");
            assert_eq!(entries[1].claim, "identity: cos(x)^2 + sin(x)^2 == 0");
        }

        // Sabotage gate: tamper one byte in the ledger file
        {
            let raw = std::fs::read_to_string(&ledger_path).unwrap();
            let tampered = raw.replacen("x^2 + 1", "x^2 + 2", 1);
            std::fs::write(&ledger_path, tampered).unwrap();

            let ledger = MistakeLedger::open(&ledger_path).unwrap();
            let res = ledger.verify_integrity();
            assert!(matches!(
                res,
                Err(LedgerVerifyError::HashMismatch { line: 1, .. })
            ));
        }

        let _ = std::fs::remove_file(&ledger_path);
        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
