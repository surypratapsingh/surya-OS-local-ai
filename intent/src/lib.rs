//! NOVA's command model (docs/work-orders.md, N4).
//!
//! A small transformer encoder, written and trained from scratch with no
//! dependencies, that reads the owner's words and predicts one intent (chat or
//! one of 13 actions) plus a BIO slot tag per word. The specification every
//! implementation must follow (tokens, features, layout, file format) is in
//! `intent/README.md`; the stdlib-Python runtime in
//! `novacore/src/novacore/intent.py` is the second implementation.

pub mod features;
pub mod io;
pub mod math;
pub mod model;
pub mod train;

/// Intent classes, in output order.
pub const INTENTS: [&str; 14] = [
    "chat",
    "apps.open",
    "files.find",
    "files.open",
    "media.play_pause",
    "media.next",
    "media.previous",
    "volume.up",
    "volume.down",
    "volume.mute",
    "system.lock",
    "time.now",
    "timer.set",
    "memory.save",
];

/// BIO slot tags, in output order.
pub const TAGS: [&str; 11] = [
    "O", "B-app", "I-app", "B-query", "I-query", "B-num", "I-num", "B-label", "I-label", "B-text", "I-text",
];
