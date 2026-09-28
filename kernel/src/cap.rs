//! Kernel capability table (K4).
//!
//! AGENTS.md, project invariants: "Capabilities are explicit. A process or
//! module gets what its manifest declares and nothing else. Ambient authority
//! is a bug." This module is the enforcement point K4's done-when sentence
//! refers to (docs/work-orders.md, Phase A ladder): a resource absent from a
//! subject's capability set must be untouchable by that subject.
//!
//! Design, and why the types look like this:
//!
//! - The wire grammar was already fixed by C2 (`docs/manifest-format.md`,
//!   `packages[i].capabilities`): `namespace:permission`, each side lowercase
//!   `[a-z][a-z0-9_]*`, **no wildcards** — "a wildcard is ambient authority,
//!   which AGENTS.md forbids". The host verifier `tools/nova_trust.py`
//!   (the `_CAPABILITY` regex, no `*` in either character class) already
//!   rejects wildcards in a signed manifest.
//! - Here the same rule is enforced by the type system instead of a regex:
//!   a resource is a fieldless enum arm. There is no stringly-typed
//!   capability name in a `CapSet` and no wildcard variant, so ambient
//!   authority is *unrepresentable*, not merely rejected at runtime.
//! - There is deliberately no second `Cap` enum mirroring `Resource`. A
//!   grant's wire form is the resource's exact signed name; two parallel
//!   encodings of one vocabulary would be a second table to keep in sync —
//!   a hole factory. One vocabulary, one mediation point.
//! - Deny by default (Saltzer & Schroeder, "The Protection of Information in
//!   Computer Systems", 1975: least privilege; complete mediation — every
//!   access is checked through the table, and the default answer is no):
//!   `CapSet::new()` grants nothing, and a `CapSet` can only come from
//!   `from_manifest`, which adds a row only when the manifest grants it with
//!   a confirmed flag. An unconfirmed or unknown row is a hard error, never
//!   a silent absence: the package verifier (also this work order) must be
//!   able to tell "not granted" from "malformed grant", and a partially
//!   parsed set must never escape the constructor (all-or-nothing).
//! - `Resource::Kernel` ("reserved") stands for kernel-internal authority no
//!   process ever holds. `from_manifest` refuses it, so even the fullest
//!   legal set denies it. It exists so deny-by-default itself stays
//!   testable: if a future refactor made every resource grantable, the gate
//!   would still catch the reserved resource being granted.
//!
//! Oracle layering (AGENTS.md rules 1-2): there is no external tool for an
//! in-kernel permission system, so the vocabulary is checked two ways.
//! In-kernel, `capselftest.rs` derives its expectations from hand-written
//! tables in that file, never from this one. Out-of-kernel,
//! `kernel/scripts/test-cap.sh` (check.sh stage 11) parses THIS file's enum
//! arms and asserts they agree with the manifest grammar and examples that
//! C2 fixed — an oracle outside the compiled binary. Mutation proofs for
//! both layers live in `docs/logs/k4a-cap-mutations.log`.

use core::fmt;

/// The kernel-reserved resource. Never grantable, never held by a process.
pub(crate) const RESERVED_NAME: &str = "reserved";

/// Everything a process could ask the kernel to do. Closed vocabulary:
/// adding a resource means adding the enum arm, its wire name here, and its
/// enforcement point — in that order, in one review.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Resource {
    /// The camera sensor. AGENTS.md invariant: camera bytes live in RAM
    /// only; this capability gates the request, the RAM-only path is
    /// enforced by the storage rules, not here.
    Camera,
    /// The microphone.
    Microphone,
    /// Audio output.
    Audio,
    /// Read from the filesystem (the K3 read-only FAT32 path).
    FsRead,
    /// Write to the filesystem. No kernel implementation exists yet, so
    /// this currently denies for everyone — fail-closed by absence of the
    /// operation, not by a stub that lies.
    FsWrite,
    /// Evaluate an expression (the `mathd` payload's manifest declares this,
    /// per `docs/manifest-format.md` and the C2 fixtures).
    ComputeExpression,
    /// Verify a claimed result (mathd's verifier).
    ComputeVerify,
    /// Kernel-reserved authority. No process ever holds it (module doc).
    Kernel,
}

/// Number of distinct resources; `Resource::Kernel` is the last arm, so this
/// counts every variant. The gate asserts its hand probe table matches.
pub(crate) const RESOURCE_COUNT: usize = Resource::Kernel as usize + 1;

impl Resource {
    /// Wire name of a resource: exactly the `namespace:permission` form C2
    /// signs (single-word namespaces for devices, per the C2 fixture's
    /// `compute:expression` shape).
    pub(crate) fn to_name(self) -> &'static str {
        match self {
            Resource::Camera => "camera",
            Resource::Microphone => "microphone",
            Resource::Audio => "audio",
            Resource::FsRead => "fs:read",
            Resource::FsWrite => "fs:write",
            Resource::ComputeExpression => "compute:expression",
            Resource::ComputeVerify => "compute:verify",
            Resource::Kernel => RESERVED_NAME,
        }
    }

    /// The only path from a manifest row to a resource. A name outside the
    /// closed vocabulary — including every wildcard form, which cannot name
    /// anything — returns `None`. Fail closed.
    pub(crate) fn from_name(name: &str) -> Option<Resource> {
        match name {
            "camera" => Some(Resource::Camera),
            "microphone" => Some(Resource::Microphone),
            "audio" => Some(Resource::Audio),
            "fs:read" => Some(Resource::FsRead),
            "fs:write" => Some(Resource::FsWrite),
            "compute:expression" => Some(Resource::ComputeExpression),
            "compute:verify" => Some(Resource::ComputeVerify),
            RESERVED_NAME => Some(Resource::Kernel),
            _ => None,
        }
    }

    /// Inverse of the discriminant order, for full-range scans and evidence
    /// printing (the `Debug` impl below).
    pub(crate) fn from_index(i: usize) -> Option<Resource> {
        match i {
            0 => Some(Resource::Camera),
            1 => Some(Resource::Microphone),
            2 => Some(Resource::Audio),
            3 => Some(Resource::FsRead),
            4 => Some(Resource::FsWrite),
            5 => Some(Resource::ComputeExpression),
            6 => Some(Resource::ComputeVerify),
            7 => Some(Resource::Kernel),
            _ => None,
        }
    }
}

/// Why `from_manifest` refused a row. The verifier reports the row index
/// with it, so a bad package names its own defect.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum CapErrorKind {
    /// A wildcard form (`camera:*`, `fs:*`, `*`) — ambient authority.
    Wildcard,
    /// Not in the closed vocabulary.
    Unknown,
    /// Names the reserved kernel resource.
    Reserved,
    /// Present but not confirmed granted. The constructor rule is: absence
    /// of a row means no grant; a row that says "not granted" is malformed
    /// input to this path and is refused, not silently skipped.
    Unconfirmed,
    /// More rows than a manifest may carry.
    TooMany,
}

impl CapErrorKind {
    pub(crate) fn kind_str(self) -> &'static str {
        match self {
            CapErrorKind::Wildcard => "wildcard",
            CapErrorKind::Unknown => "unknown",
            CapErrorKind::Reserved => "reserved",
            CapErrorKind::Unconfirmed => "unconfirmed",
            CapErrorKind::TooMany => "too-many",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) struct CapError {
    pub(crate) index: usize,
    pub(crate) kind: CapErrorKind,
}

/// Upper bound on manifest rows one subject may carry. Distinct grants are
/// bounded by the vocabulary (7 grantable resources of 8); this bounds the
/// raw row count before dedup so a malformed package cannot push unbounded
/// input at the constructor.
pub(crate) const MAX_CAPS: usize = 16;

/// The capability set of one subject. Only constructible through
/// `from_manifest`: no `add`, no `Default`, no public field — a set cannot
/// grow by anything other than a confirmed manifest row (the same
/// fail-closed-constructor rule work order B8 applies to `Verified` cards).
#[derive(Clone, Copy)]
pub(crate) struct CapSet {
    /// One flag per `Resource` discriminant. All `false` = denies everything.
    owned: [bool; RESOURCE_COUNT],
}

impl CapSet {
    /// The empty set: denies everything. Deny-by-default made literal.
    pub(crate) fn new() -> CapSet {
        CapSet {
            owned: [false; RESOURCE_COUNT],
        }
    }

    /// Build a set from one package's manifest rows: `(wire name, granted)`.
    /// All-or-nothing: the first refused row aborts the whole set, so no
    /// partial set can escape. A row is added only when `granted` is `true`.
    /// Duplicate rows are accepted and idempotent here (the host package
    /// verifier rejects duplicates per `docs/manifest-format.md`; a repeated
    /// grant adds nothing, so tolerating one is not a hole).
    pub(crate) fn from_manifest(entries: &[(&str, bool)]) -> Result<CapSet, CapError> {
        if entries.len() > MAX_CAPS {
            return Err(CapError {
                index: entries.len(),
                kind: CapErrorKind::TooMany,
            });
        }
        let mut set = CapSet::new();
        for (i, &(name, granted)) in entries.iter().enumerate() {
            if name.contains('*') {
                return Err(CapError {
                    index: i,
                    kind: CapErrorKind::Wildcard,
                });
            }
            if !granted {
                return Err(CapError {
                    index: i,
                    kind: CapErrorKind::Unconfirmed,
                });
            }
            match Resource::from_name(name) {
                Some(Resource::Kernel) => {
                    return Err(CapError {
                        index: i,
                        kind: CapErrorKind::Reserved,
                    })
                }
                Some(r) => set.owned[r as usize] = true,
                None => {
                    return Err(CapError {
                        index: i,
                        kind: CapErrorKind::Unknown,
                    })
                }
            }
        }
        Ok(set)
    }

    /// The mediation point of the done-when sentence: attempt to touch a
    /// resource; the answer is no unless a confirmed manifest row granted
    /// it. Every future syscall path must ask here (complete mediation).
    pub(crate) fn grants(&self, r: Resource) -> bool {
        self.owned[r as usize]
    }

    pub(crate) fn len(&self) -> usize {
        let mut n = 0;
        for flag in self.owned.iter() {
            if *flag {
                n += 1;
            }
        }
        n
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Debug renders the set as its granted wire names, so a denial in a log
/// names exactly what the subject holds and nothing else.
impl fmt::Debug for CapSet {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("[")?;
        let mut first = true;
        for i in 0..RESOURCE_COUNT {
            if self.owned[i] {
                if let Some(r) = Resource::from_index(i) {
                    if !first {
                        f.write_str(", ")?;
                    }
                    f.write_str(r.to_name())?;
                    first = false;
                }
            }
        }
        f.write_str("]")
    }
}
