//! Oracles: the FNV-1a 32-bit test vectors published with the algorithm
//! (http://www.isthe.com/chongo/tech/comp/fnv/ "FNV test vectors"), and the
//! platform's libm for the deterministic exp and tanh.

use intent::features::{encode, fnv1a32, token_features, tokens};
use intent::math;

#[test]
fn fnv1a_matches_the_published_vectors() {
    assert_eq!(fnv1a32(b""), 0x811c_9dc5);
    assert_eq!(fnv1a32(b"a"), 0xe40c_292c);
    assert_eq!(fnv1a32(b"foobar"), 0xbf9c_f968);
}

#[test]
fn tokens_follow_the_shared_rule() {
    assert_eq!(tokens("Open Spotify!"), ["open", "spotify"]);
    assert_eq!(
        tokens("Mom's birthday: June 5"),
        ["mom", "s", "birthday", "june", "5"]
    );
    assert_eq!(tokens("café ☕ open"), ["caf", "open"]);
    assert_eq!(tokens("INVOICE-42.PDF"), ["invoice", "42", "pdf"]);
    assert!(tokens("  ?! ").is_empty());
}

#[test]
fn features_are_the_word_then_its_trigrams() {
    assert_eq!(token_features("ab"), ["w:ab", "t:<ab", "t:ab>"]);
    assert_eq!(token_features("a"), ["w:a", "t:<a>"]);
    let input = encode("open it", 8192, 64).unwrap();
    assert_eq!(input.len(), 3); // the summary position and two words
    assert_eq!(input[1].len(), 1 + 4); // "open": word + "<op" "ope" "pen" "en>"
                                       // 64 positions: the summary token plus at most 63 words.
    assert!(encode(&"word ".repeat(64), 8192, 64).is_none());
    assert!(encode(&"word ".repeat(63), 8192, 64).is_some());
}

#[test]
fn deterministic_exp_agrees_with_libm() {
    let mut worst: f64 = 0.0;
    let mut x = -700.0;
    while x <= 700.0 {
        let ours = math::exp(x);
        let libm = x.exp();
        worst = worst.max(((ours - libm) / libm).abs());
        x += 0.371_9;
    }
    for &x in &[
        0.0,
        1e-300,
        -1e-300,
        1e-10,
        -1e-10,
        0.5,
        -0.5,
        1.0,
        0.346_573_590_279_972_6,
    ] {
        worst = worst.max(((math::exp(x) - x.exp()) / x.exp()).abs());
    }
    assert!(worst < 1e-14, "worst relative error {worst:e}");
    assert_eq!(math::exp(800.0), f64::INFINITY);
    assert_eq!(math::exp(-800.0), 0.0);
}

#[test]
fn deterministic_tanh_agrees_with_libm() {
    let mut x = -25.0;
    while x <= 25.0 {
        let error = (math::tanh(x) - x.tanh()).abs();
        assert!(error < 1e-15, "tanh({x}) off by {error:e}");
        x += 0.013_7;
    }
}
