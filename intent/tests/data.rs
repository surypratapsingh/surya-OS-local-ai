//! The generator (Python) and this crate (Rust) tokenize independently. Every
//! committed training and development line must give one token per tag.

use intent::model::Config;
use intent::train::parse_tsv;

fn check(path: &str) {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{path}: {e}"));
    let (examples, too_long) = parse_tsv(&text, Config::NOVA.buckets, Config::NOVA.max_len).unwrap();
    assert!(examples.len() > 1000, "{path}: only {} examples", examples.len());
    println!("{path}: {} lines agree, {too_long} too long", examples.len());
}

#[test]
fn train_and_dev_lines_tokenize_to_their_tag_counts() {
    check(concat!(env!("CARGO_MANIFEST_DIR"), "/data/train.tsv"));
    check(concat!(env!("CARGO_MANIFEST_DIR"), "/data/dev.tsv"));
}
