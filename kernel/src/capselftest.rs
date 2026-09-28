//! K4 capability gate (cap gate).
//!
//! The done-when (docs/work-orders.md, Phase A ladder, K4 row): "A process
//! cannot touch a resource absent from its capability set — proven by a test
//! that attempts it and is denied." The named scenario below does exactly
//! that: a subject whose manifest declares only `fs:read` attempts the
//! camera through the mediation point and is denied.
//!
//! Oracle layering (AGENTS.md rules 1-2). No external tool exists for an
//! in-kernel permission system, so every expectation here is hand-written
//! from the specification, never derived from `cap.rs`:
//!
//! - HAND_NAMES is written from the wire grammar C2 fixed
//!   (`docs/manifest-format.md`: `namespace:permission`, lowercase, no
//!   wildcards; `compute:expression` / `compute:verify` are the C2 fixture
//!   examples). If cap.rs renames a wire name, this table disagrees.
//! - The matrix expectation is one spec sentence: "a process gets what its
//!   manifest declares and nothing else" — an exact name grants exactly the
//!   resource it names, so a one-row manifest for resource R must grant R
//!   and every other probe must be denied.
//! - The near-miss probe (fs:read does not open fs:write) is the case a
//!   prefix- or substring-matching bug would wrongly open.
//! - The reserved probe is the deny-by-default anchor: even the fullest
//!   legal manifest cannot grant kernel-internal authority.
//! - The independent host oracle is `kernel/scripts/test-cap.sh` (check.sh
//!   stage 11): it parses cap.rs's enum arms OUTSIDE the compiled binary and
//!   checks them against the manifest grammar and examples. Mutation proofs
//!   for both layers: `docs/logs/k4a-cap-mutations.log`.

use crate::cap::{CapError, CapErrorKind, CapSet, Resource, RESERVED_NAME, RESOURCE_COUNT};

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;

fn check(ok: bool, name: &str, detail: &str) {
    unsafe {
        if ok {
            PASS += 1;
            sprintln!("  ok    {}", name);
        } else {
            FAIL += 1;
            sprintln!("  FAIL  {} - {}", name, detail);
        }
    }
}

// ---------------------------------------------------------------------------
// Hand data, written from the specification, not from cap.rs
// ---------------------------------------------------------------------------

/// The wire names, hand-copied from `docs/manifest-format.md` (grammar and
/// C2 examples). Order matches the Resource discriminants; asserted against
/// `to_name` below so a rename cannot silently change the signed wire form.
const HAND_NAMES: [&str; 8] = [
    "camera",
    "microphone",
    "audio",
    "fs:read",
    "fs:write",
    "compute:expression",
    "compute:verify",
    RESERVED_NAME,
];

/// The seven grantable resources, in discriminant order (index 7 is the
/// reserved one and is deliberately absent from every manifest below).
const GRANTABLE: usize = 7;

fn probe_resource(i: usize) -> Resource {
    match Resource::from_index(i) {
        Some(r) => r,
        None => panic!("cap gate: probe index out of range"),
    }
}

// ---------------------------------------------------------------------------
// The done-when scenario and its neighbourhood
// ---------------------------------------------------------------------------

fn scenario_checks() {
    // Subject: a process whose manifest declares exactly ["fs:read"].
    let manifest: [(&str, bool); 1] = [("fs:read", true)];
    let subject = match CapSet::from_manifest(&manifest) {
        Ok(s) => s,
        Err(e) => panic!("cap gate: the fs:read manifest must build (got {:?})", e),
    };

    // 1. THE done-when check: attempt a resource absent from the set.
    let attempted = subject.grants(Resource::Camera);
    check(
        !attempted,
        "done-when: camera attempt by an fs:read-only process is DENIED",
        "a resource absent from the capability set was touchable",
    );

    // 2. The same subject touching what it DOES hold must succeed: the
    //    denial above must come from policy, not from a broken access path.
    check(
        subject.grants(Resource::FsRead),
        "same subject touches fs:read (its own grant works)",
        "own grant denied - denial is a broken path, not policy",
    );

    // 3. Near-miss: the family sibling stays closed. This is what a
    //    prefix/substring bug would open.
    check(
        !subject.grants(Resource::FsWrite),
        "near-miss denied: fs:write stays closed to an fs:read process",
        "a sibling resource in the same family leaked",
    );

    // 4. The fullest legal manifest: all seven grantable rows. Hand rule:
    //    it grants exactly those seven and can still not touch the
    //    kernel-reserved resource.
    let fullest: [(&str, bool); GRANTABLE] = [
        ("camera", true),
        ("microphone", true),
        ("audio", true),
        ("fs:read", true),
        ("fs:write", true),
        ("compute:expression", true),
        ("compute:verify", true),
    ];
    let full_set = match CapSet::from_manifest(&fullest) {
        Ok(s) => s,
        Err(e) => panic!("cap gate: the fullest manifest must build (got {:?})", e),
    };
    let mut fullest_ok = true;
    for i in 0..GRANTABLE {
        if !full_set.grants(probe_resource(i)) {
            fullest_ok = false;
        }
    }
    if full_set.grants(Resource::Kernel) {
        fullest_ok = false;
    }
    check(
        fullest_ok,
        "fullest manifest grants its seven rows and never the reserved resource",
        "reserved authority leaked, or a declared grant is missing",
    );

    // 5. The empty set denies every probe, including on the reserved
    //    resource (deny-by-default made literal).
    let empty = CapSet::new();
    let mut empty_ok = true;
    for i in 0..RESOURCE_COUNT {
        if empty.grants(probe_resource(i)) {
            empty_ok = false;
        }
    }
    check(
        empty_ok,
        "empty capability set denies all resources",
        "the default was not deny",
    );
}

// ---------------------------------------------------------------------------
// The full matrix: one-row manifests vs every probe
// ---------------------------------------------------------------------------

/// For each grantable resource R: build a one-row manifest for R's exact
/// wire name, then attempt all eight resources. Hand expectation (one spec
/// sentence, "what its manifest declares and nothing else"): granted iff the
/// probe IS R. The reserved column must be denied in every row.
fn matrix_checks() {
    let mut all_ok = true;
    for row in 0..GRANTABLE {
        let r = probe_resource(row);
        let manifest: [(&str, bool); 1] = [(r.to_name(), true)];
        let set = match CapSet::from_manifest(&manifest) {
            Ok(s) => s,
            Err(_) => {
                all_ok = false;
                sprintln!("        row {}: manifest refused outright", r.to_name());
                continue;
            }
        };
        for col in 0..RESOURCE_COUNT {
            let p = probe_resource(col);
            let want = p == r; // the spec sentence, written by hand
            if set.grants(p) != want {
                all_ok = false;
                sprintln!(
                    "        row {}: probe {} got {} want {}",
                    r.to_name(),
                    p.to_name(),
                    set.grants(p),
                    want
                );
            }
        }
    }
    check(
        all_ok,
        "matrix: each single-row manifest grants exactly its own resource (7x8 probes)",
        "a one-row manifest opened something it did not name",
    );
}

// ---------------------------------------------------------------------------
// Fail-closed constructor: the error taxonomy
// ---------------------------------------------------------------------------

fn refused_as(rows: &[(&str, bool)]) -> Option<CapError> {
    CapSet::from_manifest(rows).err()
}

fn error_checks() {
    // Wildcards are ambient authority. They must be REFUSED, and (being
    // unrepresentable in the set) never reach a grant.
    check(
        refused_as(&[("*", true)]).map(|e| e.kind) == Some(CapErrorKind::Wildcard),
        "refused: bare \"*\" row (wildcard)",
        "a wildcard row escaped the constructor",
    );
    check(
        refused_as(&[("camera:*", true)]).map(|e| e.kind) == Some(CapErrorKind::Wildcard),
        "refused: \"camera:*\" row (namespace wildcard)",
        "a wildcard row escaped the constructor",
    );
    check(
        refused_as(&[("fs:*", true)]).map(|e| e.kind) == Some(CapErrorKind::Wildcard),
        "refused: \"fs:*\" row (permission wildcard)",
        "a wildcard row escaped the constructor",
    );

    // Names outside the closed vocabulary. `Boot:read` / `fs:READ` fail the
    // lowercase grammar; `boot:cpu` is well-formed but names no process
    // authority here (it is a bootchain hash name, not a resource).
    check(
        refused_as(&[("Boot:read", true)]).map(|e| e.kind) == Some(CapErrorKind::Unknown),
        "refused: \"Boot:read\" (uppercase, out of grammar)",
        "an out-of-vocabulary name was accepted",
    );
    check(
        refused_as(&[("boot:cpu", true)]).map(|e| e.kind) == Some(CapErrorKind::Unknown),
        "refused: \"boot:cpu\" (not a process resource)",
        "an out-of-vocabulary name was accepted",
    );
    check(
        refused_as(&[("fs:READ", true)]).map(|e| e.kind) == Some(CapErrorKind::Unknown),
        "refused: \"fs:READ\" (uppercase permission)",
        "an out-of-vocabulary name was accepted",
    );
    check(
        refused_as(&[("compute:expression ", true)]).map(|e| e.kind) == Some(CapErrorKind::Unknown),
        "refused: trailing-space name",
        "an out-of-vocabulary name was accepted",
    );

    // The kernel-reserved resource is never grantable.
    check(
        refused_as(&[(RESERVED_NAME, true)]).map(|e| e.kind) == Some(CapErrorKind::Reserved),
        "refused: reserved-resource row",
        "kernel-internal authority was grantable",
    );

    // A present-but-unconfirmed row is an error, never a silent absence:
    // the package verifier must be able to tell "not granted" from
    // "malformed grant".
    check(
        refused_as(&[("camera", false)]).map(|e| e.kind) == Some(CapErrorKind::Unconfirmed),
        "refused: unconfirmed row (error, not silent absence)",
        "an unconfirmed row slipped through as a non-grant",
    );

    // All-or-nothing: a bad SECOND row rejects the whole set, and the error
    // names the offending row index. No partial set escapes.
    let two_rows: [(&str, bool); 2] = [("camera", true), ("fs:*", true)];
    check(
        refused_as(&two_rows).map(|e| (e.index, e.kind)) == Some((1, CapErrorKind::Wildcard)),
        "all-or-nothing: bad second row rejects the whole set at index 1",
        "a partial set escaped the constructor, or the index is wrong",
    );

    // Row-count bound: MAX_CAPS rows build; MAX_CAPS+1 is refused. A fixed
    // stack array of legal names, no allocation.
    let over: [(&str, bool); 17] = [("camera", true); 17];
    let at_limit = CapSet::from_manifest(&over[..16]).is_ok();
    let over_limit = match CapSet::from_manifest(&over) {
        Err(e) => e.index == 17,
        Ok(_) => false,
    };
    check(
        at_limit && over_limit,
        "row-count bound: 16 rows build, 17 refused with index 17",
        "the TOO_MANY bound is off",
    );
}

// ---------------------------------------------------------------------------
// Vocabulary and bookkeeping anchors
// ---------------------------------------------------------------------------

fn vocabulary_checks() {
    // Wire names vs the hand table (provenance: docs/manifest-format.md).
    let mut names_ok = true;
    for (i, &hand) in HAND_NAMES.iter().enumerate() {
        let r = match Resource::from_index(i) {
            Some(r) => r,
            None => {
                names_ok = false;
                continue;
            }
        };
        if r.to_name() != hand {
            names_ok = false;
            sprintln!(
                "        index {}: kernel says {:?}, hand table says {:?}",
                i,
                r.to_name(),
                HAND_NAMES[i]
            );
        }
    }
    check(
        names_ok,
        "wire names match the hand table from docs/manifest-format.md",
        "a wire name drifted from the signed grammar",
    );

    // Duplicate rows are idempotent: two camera rows still grant exactly
    // one resource (the host verifier rejects duplicates; a repeat must not
    // double-count or widen anything).
    let dup: [(&str, bool); 2] = [("camera", true), ("camera", true)];
    match CapSet::from_manifest(&dup) {
        Ok(s) => check(
            s.len() == 1 && s.grants(Resource::Camera) && !s.grants(Resource::Microphone),
            "duplicate rows are idempotent (len stays 1)",
            "a duplicate grant widened or double-counted",
        ),
        Err(_) => check(
            false,
            "duplicate rows are idempotent (len stays 1)",
            "duplicate legal rows were refused",
        ),
    }

    // Debug rendering names exactly what the subject holds, so a denial log
    // is auditable. Rendered through a hand-rolled fmt::Write sink.
    use core::fmt::Write as _;
    struct Sink {
        buf: [u8; 64],
        n: usize,
    }
    impl core::fmt::Write for Sink {
        fn write_str(&mut self, t: &str) -> core::fmt::Result {
            for &b in t.as_bytes() {
                if self.n < self.buf.len() {
                    self.buf[self.n] = b;
                    self.n += 1;
                }
            }
            Ok(())
        }
    }
    let one: [(&str, bool); 1] = [("fs:read", true)];
    match CapSet::from_manifest(&one) {
        Ok(s) => {
            let mut sink = Sink {
                buf: [0u8; 64],
                n: 0,
            };
            let wrote = core::write!(sink, "{:?}", s).is_ok();
            check(
                wrote && &sink.buf[..sink.n] == b"[fs:read]",
                "Debug rendering lists exactly the granted wire names",
                "evidence rendering is wrong",
            );
        }
        Err(_) => check(
            false,
            "Debug rendering lists exactly the granted wire names",
            "the fs:read manifest unexpectedly failed",
        ),
    }
}

// ---------------------------------------------------------------------------
// Gate entry points (same contract as the other gates)
// ---------------------------------------------------------------------------

pub fn run() {
    sprintln!("cap gate:   capability table (deny-by-default, no wildcards)");
    scenario_checks();
    matrix_checks();
    error_checks();
    vocabulary_checks();
    let (p, f) = unsafe { (PASS, FAIL) };
    sprintln!("cap gate:   {} checks passed, {} failed", p, f);
}

pub fn summary() -> (u32, u32) {
    unsafe { (PASS, FAIL) }
}
