//! Tests and benchmark for B8 type-enforced CardStore.
//!
//! Spec (`docs/work-orders.md` B8):
//! - Keyed by B2 structural hash
//! - Hard invariant enforced by type system: Card cannot be constructed without
//!   Verdict::Verified and its evidence.
//! - Method card candidate promotion after N cards share a structural shape;
//!   proposals until human confirms, never auto-promote.
//! - Test demonstrates no code path can insert an unverified card.
//! - Benchmark shows cache-hit lookup under 1 ms over 100,000 cards.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use mathd::canonicalizer::structural_hash;
use mathd::card_store::{Card, CardError, CardStore, MethodStatus, VerifiedWitness};
use mathd::parser::{parse, BinOp, Expr};
use mathd::verifier::{Evidence, Side, Verdict};

// ---------------------------------------------------------------------------
// 1. Hard invariant: unverified card cannot be constructed
// ---------------------------------------------------------------------------

#[test]
fn test_unverified_verdict_cannot_construct_card() {
    let problem = parse("x + 1").expect("valid problem");
    let answer = parse("1 + x").expect("valid answer");

    // Negative case A: Verdict::Failed with counterexample
    let mut ce = HashMap::new();
    ce.insert("x".to_string(), 2.0);
    let failed_verdict = Verdict::Failed {
        counterexample: ce,
        expected: 3.0,
        got: 99.0,
    };

    let witness_res = VerifiedWitness::from_verdict(&failed_verdict);
    assert!(
        witness_res.is_err(),
        "VerifiedWitness::from_verdict must reject Verdict::Failed"
    );
    match witness_res.unwrap_err() {
        CardError::VerdictNotVerified { reason } => {
            assert!(
                reason.contains("Failed"),
                "error message must describe rejection: {}",
                reason
            );
        }
        err => panic!("unexpected error variant: {:?}", err),
    }

    let card_res = Card::from_verified(problem.clone(), answer.clone(), &failed_verdict);
    assert!(
        card_res.is_err(),
        "Card::from_verified must reject Verdict::Failed"
    );
    match card_res.unwrap_err() {
        CardError::VerdictNotVerified { reason } => {
            assert!(
                reason.contains("Failed"),
                "error message must describe rejection: {}",
                reason
            );
        }
        err => panic!("unexpected error variant: {:?}", err),
    }

    // Negative case B: Verdict::Unverifiable
    let unverifiable_verdict = Verdict::Unverifiable {
        reason: "domain contains pole at x = 0".to_string(),
    };

    let witness_res_u = VerifiedWitness::from_verdict(&unverifiable_verdict);
    assert!(
        witness_res_u.is_err(),
        "VerifiedWitness::from_verdict must reject Verdict::Unverifiable"
    );
    match witness_res_u.unwrap_err() {
        CardError::VerdictNotVerified { reason } => {
            assert!(
                reason.contains("Unverifiable"),
                "error message must describe rejection: {}",
                reason
            );
        }
        err => panic!("unexpected error variant: {:?}", err),
    }

    let card_res_u = Card::from_verified(problem.clone(), answer.clone(), &unverifiable_verdict);
    assert!(
        card_res_u.is_err(),
        "Card::from_verified must reject Verdict::Unverifiable"
    );
    match card_res_u.unwrap_err() {
        CardError::VerdictNotVerified { reason } => {
            assert!(
                reason.contains("Unverifiable"),
                "error message must describe rejection: {}",
                reason
            );
        }
        err => panic!("unexpected error variant: {:?}", err),
    }

    // Negative case C: Verdict::FailedDomain (Finding N1 in docs/reviews/b8-freebuff.log)
    let mut ce_fd = HashMap::new();
    ce_fd.insert("x".to_string(), -1.0);
    let failed_domain_verdict = Verdict::FailedDomain {
        counterexample: ce_fd,
        undefined: Side::Rhs,
        defined_value: 2.0,
    };

    let witness_res_fd = VerifiedWitness::from_verdict(&failed_domain_verdict);
    assert!(
        witness_res_fd.is_err(),
        "VerifiedWitness::from_verdict must reject Verdict::FailedDomain"
    );
    match witness_res_fd.unwrap_err() {
        CardError::VerdictNotVerified { reason } => {
            assert!(
                reason.contains("FailedDomain"),
                "error message must describe rejection of FailedDomain: {}",
                reason
            );
        }
        err => panic!("unexpected error variant: {:?}", err),
    }

    let card_res_fd = Card::from_verified(problem, answer, &failed_domain_verdict);
    assert!(
        card_res_fd.is_err(),
        "Card::from_verified must reject Verdict::FailedDomain"
    );
    match card_res_fd.unwrap_err() {
        CardError::VerdictNotVerified { reason } => {
            assert!(
                reason.contains("FailedDomain"),
                "error message must describe rejection of FailedDomain: {}",
                reason
            );
        }
        err => panic!("unexpected error variant: {:?}", err),
    }

    // Invariant proof: CardStore::insert only accepts an instance of Card.
    // Because no Card instance can be produced from negative verdicts,
    // the store cannot hold any unverified card.
    let store = CardStore::new();
    assert_eq!(store.len(), 0);
    assert!(store.is_empty());
}

#[test]
fn test_verified_verdict_constructs_card_with_exact_evidence() {
    let problem = parse("sin(x)^2 + cos(x)^2").expect("problem");
    let answer = parse("1").expect("answer");
    let expected_evidence = Evidence {
        points_tested: 16,
        max_relative_error: 2.22e-16,
    };
    let verdict = Verdict::Verified {
        evidence: expected_evidence.clone(),
    };

    // 1. Via VerifiedWitness
    let witness = VerifiedWitness::from_verdict(&verdict).expect("witness created from Verified");
    assert_eq!(witness.points_tested(), 16);
    assert_eq!(witness.max_relative_error(), 2.22e-16);
    assert_eq!(witness.evidence(), &expected_evidence);

    let card = Card::from_witness(problem.clone(), answer.clone(), &witness);
    assert_eq!(card.problem(), &problem);
    assert_eq!(card.answer(), &answer);
    assert_eq!(card.structural_hash(), structural_hash(&problem));
    assert_eq!(card.evidence(), &expected_evidence);
    assert_eq!(card.points_tested(), 16);
    assert_eq!(card.max_relative_error(), 2.22e-16);

    // 2. Direct from_verified constructor
    let card2 = Card::from_verified(problem.clone(), answer, &verdict).expect("card created");
    assert_eq!(card, card2);

    // 3. Insert into store
    let mut store = CardStore::new();
    store.insert(card);
    assert_eq!(store.len(), 1);
    assert!(!store.is_empty());

    let fetched = store.get_by_expr(&problem).expect("card must be found");
    assert_eq!(fetched.evidence(), &expected_evidence);
}

// ---------------------------------------------------------------------------
// 2. B2 structural hash cache keying
// ---------------------------------------------------------------------------

#[test]
fn test_structural_hash_cache_keying_canonical_equivalence() {
    let mut store = CardStore::new();

    // Two syntactically different expressions that canonicalize to the same normal form:
    // "x + 2" and "2 + x"
    let p1 = parse("x + 2").expect("p1");
    let p2 = parse("2 + x").expect("p2");

    let h1 = structural_hash(&p1);
    let h2 = structural_hash(&p2);
    assert_eq!(
        h1, h2,
        "B2 oracle: algebraically commutative sum must have identical structural hash"
    );

    let verdict = Verdict::Verified {
        evidence: Evidence {
            points_tested: 8,
            max_relative_error: 0.0,
        },
    };
    let card = Card::from_verified(p1.clone(), parse("x + 2").expect("ans"), &verdict)
        .expect("verified card");
    store.insert(card);

    // Look up by syntactic variant p2
    assert!(
        store.contains_expr(&p2),
        "store must report presence for canonical equivalent"
    );
    let hit = store.get_by_expr(&p2).expect("hit by canonical equivalent");
    assert_eq!(hit.problem(), &p1);
    assert_eq!(hit.structural_hash(), h1);
}

// ---------------------------------------------------------------------------
// 3. Method card candidate promotion & no auto-promotion
// ---------------------------------------------------------------------------

#[test]
fn test_method_card_candidate_promotion_and_never_autopromotes() {
    let mut store = CardStore::with_promotion_threshold(3);
    assert_eq!(store.promotion_threshold(), 3);

    let verdict = Verdict::Verified {
        evidence: Evidence {
            points_tested: 8,
            max_relative_error: 0.0,
        },
    };

    // Shape: ($C * $x) + $C (linear form)
    let p1 = parse("2 * x + 1").expect("p1");
    let p2 = parse("5 * y + 4").expect("p2");
    let p3 = parse("9 * z + 7").expect("p3");
    let p4 = parse("3 * w + 2").expect("p4");
    let p5 = parse("8 * u + 6").expect("p5");

    let ans = parse("0").expect("ans");

    // Card 1
    let c1 = Card::from_verified(p1, ans.clone(), &verdict).expect("c1");
    let out1 = store.insert(c1);
    assert!(out1.is_none(), "no promotion at card 1 (count < 3)");
    assert_eq!(store.proposed_methods().len(), 0);
    assert_eq!(store.confirmed_methods().len(), 0);

    // Card 2
    let c2 = Card::from_verified(p2, ans.clone(), &verdict).expect("c2");
    let out2 = store.insert(c2);
    assert!(out2.is_none(), "no promotion at card 2 (count < 3)");
    assert_eq!(store.proposed_methods().len(), 0);
    assert_eq!(store.confirmed_methods().len(), 0);

    // Card 3: Threshold reached! Must emit candidate method card
    let c3 = Card::from_verified(p3, ans.clone(), &verdict).expect("c3");
    let out3 = store.insert(c3);
    assert!(out3.is_some(), "candidate method card emitted at card 3");

    let method = out3.unwrap();
    assert_eq!(method.status(), MethodStatus::Proposed);
    assert!(method.is_proposed());
    assert!(!method.is_confirmed());
    assert!(!method.is_rejected());
    assert_eq!(method.card_count(), 3);
    assert_eq!(method.exemplar_hashes().len(), 3);

    let method_id = method.id();
    assert_eq!(store.proposed_methods().len(), 1);
    assert_eq!(store.confirmed_methods().len(), 0);

    // Card 4: Same shape. Invariant: NEVER auto-promotes to Confirmed
    let c4 = Card::from_verified(p4, ans.clone(), &verdict).expect("c4");
    let out4 = store.insert(c4);
    assert!(
        out4.is_none(),
        "no new method card emitted (already proposed)"
    );
    let current_method = store.get_method(method_id).expect("method exists");
    assert_eq!(
        current_method.status(),
        MethodStatus::Proposed,
        "Method card MUST NOT auto-promote at card 4"
    );
    assert!(current_method.is_proposed());
    assert_eq!(current_method.card_count(), 4);
    assert_eq!(store.confirmed_methods().len(), 0);

    // Card 5: Same shape. Invariant: STILL NEVER auto-promotes
    let c5 = Card::from_verified(p5, ans, &verdict).expect("c5");
    let out5 = store.insert(c5);
    assert!(out5.is_none());
    let current_method5 = store.get_method(method_id).expect("method exists");
    assert_eq!(
        current_method5.status(),
        MethodStatus::Proposed,
        "Method card MUST NOT auto-promote at card 5"
    );
    assert_eq!(current_method5.card_count(), 5);
    assert_eq!(store.confirmed_methods().len(), 0);

    // Explicit human action: Confirm
    store
        .confirm_method(method_id)
        .expect("confirm method succeeds");
    let confirmed_method = store.get_method(method_id).expect("method exists");
    assert_eq!(confirmed_method.status(), MethodStatus::Confirmed);
    assert!(confirmed_method.is_confirmed());
    assert!(!confirmed_method.is_proposed());
    assert_eq!(store.proposed_methods().len(), 0);
    assert_eq!(store.confirmed_methods().len(), 1);
}

#[test]
fn test_differing_shapes_do_not_promote() {
    let mut store = CardStore::with_promotion_threshold(3);
    let verdict = Verdict::Verified {
        evidence: Evidence {
            points_tested: 8,
            max_relative_error: 0.0,
        },
    };

    // Three distinct shapes:
    // Shape 1: sin($x)
    let p1 = parse("sin(x)").expect("p1");
    // Shape 2: $x ^ $C
    let p2 = parse("x ^ 2").expect("p2");
    // Shape 3: ln($x)
    let p3 = parse("ln(x)").expect("p3");

    let ans = parse("0").expect("ans");
    store.insert(Card::from_verified(p1, ans.clone(), &verdict).unwrap());
    store.insert(Card::from_verified(p2, ans.clone(), &verdict).unwrap());
    store.insert(Card::from_verified(p3, ans, &verdict).unwrap());

    assert_eq!(
        store.proposed_methods().len(),
        0,
        "differing shapes must each have count 1, below threshold 3"
    );
    assert_eq!(store.confirmed_methods().len(), 0);
}

// ---------------------------------------------------------------------------
// 4. Benchmark: Cache-hit lookup under 1 ms over 100,000 cards
// ---------------------------------------------------------------------------

#[test]
fn test_100k_card_cache_hit_lookup_under_1ms() {
    let mut store = CardStore::new();
    let verdict = Verdict::Verified {
        evidence: Evidence {
            points_tested: 16,
            max_relative_error: 0.0,
        },
    };

    const N: usize = 100_000;
    println!("--- B8 100k Card Benchmark ---");
    println!("Generating and inserting {} verified cards...", N);

    let start_build = Instant::now();
    for i in 0..N {
        // Distinct problem expressions: x + i
        let problem = Expr::BinOp {
            op: BinOp::Add,
            left: Box::new(Expr::Variable("x".to_string())),
            right: Box::new(Expr::Integer(i as i64)),
        };
        let answer = Expr::Integer(i as i64);
        let card = Card::from_verified(problem, answer, &verdict).expect("valid card");
        store.insert(card);
    }
    let build_duration = start_build.elapsed();
    println!("Built {} card store in {:?}", N, build_duration);
    assert_eq!(store.len(), N);

    // 1. Single-lookup latency at representative sample positions
    let probe_indices = [0, 42, 10_000, 50_000, 99_999];
    let mut max_single_lookup = Duration::ZERO;

    for &idx in &probe_indices {
        let query_expr = Expr::BinOp {
            op: BinOp::Add,
            left: Box::new(Expr::Variable("x".to_string())),
            right: Box::new(Expr::Integer(idx as i64)),
        };

        let start_query = Instant::now();
        let hit = store.get_by_expr(&query_expr);
        let query_duration = start_query.elapsed();

        assert!(hit.is_some(), "card {} must be found in cache", idx);
        let card = hit.unwrap();
        assert_eq!(card.answer(), &Expr::Integer(idx as i64));

        if query_duration > max_single_lookup {
            max_single_lookup = query_duration;
        }

        println!(
            "Single lookup for card #{:<5}: {:?} ({} ns)",
            idx,
            query_duration,
            query_duration.as_nanos()
        );

        assert!(
            query_duration < Duration::from_millis(1),
            "Single lookup for card {} took {:?}, exceeding 1 ms limit!",
            idx,
            query_duration
        );
    }

    println!(
        "Max single-lookup latency: {:?} ({} ns)",
        max_single_lookup,
        max_single_lookup.as_nanos()
    );

    // 2. High-volume batch query: 10,000 cache-hit lookups
    const BATCH_SIZE: usize = 10_000;
    println!("Running batch of {} cache-hit lookups...", BATCH_SIZE);

    let start_batch = Instant::now();
    for i in 0..BATCH_SIZE {
        // Pseudorandom stride over the 100k card indices
        let idx = (i * 7919 + 31) % N;
        let query_expr = Expr::BinOp {
            op: BinOp::Add,
            left: Box::new(Expr::Variable("x".to_string())),
            right: Box::new(Expr::Integer(idx as i64)),
        };
        let hit = store.get_by_expr(&query_expr);
        assert!(hit.is_some());
    }
    let batch_duration = start_batch.elapsed();
    let avg_per_lookup = batch_duration / (BATCH_SIZE as u32);

    println!(
        "Batch duration ({} queries): {:?}; Average per lookup: {:?} ({} ns)",
        BATCH_SIZE,
        batch_duration,
        avg_per_lookup,
        avg_per_lookup.as_nanos()
    );

    assert!(
        avg_per_lookup < Duration::from_millis(1),
        "Average lookup latency {:?} exceeded 1 ms threshold!",
        avg_per_lookup
    );
    println!("--- Benchmark complete: PASS (< 1 ms requirement satisfied) ---");
}

// ---------------------------------------------------------------------------
// 4. Exhaustive Verdict Variant & Store Invariant Tests (Findings N1, N2, N3)
// ---------------------------------------------------------------------------

#[test]
fn test_all_verdict_variants_exhaustive_rejection() {
    // Finding N1: Exhaustive mapping of all Verdict variants through VerifiedWitness
    let verified = Verdict::Verified {
        evidence: Evidence {
            points_tested: 16,
            max_relative_error: 0.0,
        },
    };
    let mut ce = HashMap::new();
    ce.insert("x".to_string(), 1.0);
    let failed = Verdict::Failed {
        counterexample: ce.clone(),
        expected: 1.0,
        got: 2.0,
    };
    let failed_domain = Verdict::FailedDomain {
        counterexample: ce,
        undefined: Side::Lhs,
        defined_value: 1.0,
    };
    let unverifiable = Verdict::Unverifiable {
        reason: "pole at x = 0".to_string(),
    };

    let all_verdicts = vec![
        ("Verified", verified, true),
        ("Failed", failed, false),
        ("FailedDomain", failed_domain, false),
        ("Unverifiable", unverifiable, false),
    ];

    let mut ok_count = 0;
    for (name, v, should_be_ok) in all_verdicts {
        let res = VerifiedWitness::from_verdict(&v);
        if should_be_ok {
            assert!(res.is_ok(), "Variant {} must produce Ok witness", name);
            ok_count += 1;
        } else {
            assert!(
                res.is_err(),
                "Variant {} must be rejected as unverified",
                name
            );
        }
    }
    assert_eq!(
        ok_count, 1,
        "Exactly one Verdict variant (Verified) can produce a VerifiedWitness"
    );
}

#[test]
fn test_card_store_get_returns_most_recent_card_in_bucket() {
    // Finding N2: Test that when multiple cards share the same structural hash,
    // get(hash) returns the most recently inserted card (last-wins),
    // and get_all(hash) returns all cards in insertion order.
    let mut store = CardStore::new();

    let problem1 = parse("x + 1").expect("problem");
    let answer1 = parse("1 + x").expect("answer 1");
    let evidence1 = Evidence {
        points_tested: 8,
        max_relative_error: 1e-12,
    };
    let v1 = Verdict::Verified {
        evidence: evidence1,
    };
    let card1 = Card::from_verified(problem1.clone(), answer1.clone(), &v1).expect("card1");

    let problem2 = parse("1 + x").expect("problem canonically identical to x + 1");
    let answer2 = parse("x + 1").expect("answer 2 (updated formulation)");
    let evidence2 = Evidence {
        points_tested: 16,
        max_relative_error: 1e-15,
    };
    let v2 = Verdict::Verified {
        evidence: evidence2,
    };
    let card2 = Card::from_verified(problem2.clone(), answer2.clone(), &v2).expect("card2");

    let hash1 = card1.structural_hash();
    let hash2 = card2.structural_hash();
    assert_eq!(
        hash1, hash2,
        "Canonically equivalent expressions must have identical structural hash"
    );

    store.insert(card1);
    // After 1st insert: get returns card1
    let got1 = store.get(hash1).expect("card1 in store");
    assert_eq!(*got1.answer(), answer1);
    assert_eq!(got1.points_tested(), 8);

    store.insert(card2);
    // After 2nd insert into the same bucket: get MUST return the most recent (card2)
    let got2 = store.get(hash1).expect("card2 in store");
    assert_eq!(
        *got2.answer(),
        answer2,
        "CardStore::get must return the most recently inserted card (Finding N2)"
    );
    assert_eq!(got2.points_tested(), 16);
    assert_eq!(got2.max_relative_error(), 1e-15);

    // get_all returns both cards in insertion order
    let all = store.get_all(hash1).expect("bucket exists");
    assert_eq!(all.len(), 2);
    assert_eq!(*all[0].answer(), answer1);
    assert_eq!(*all[1].answer(), answer2);
}

#[test]
fn test_card_store_promotion_threshold_zero_clamped_to_one() {
    // Finding N3: with_promotion_threshold(0) is clamped to 1
    let mut store0 = CardStore::with_promotion_threshold(0);
    assert_eq!(
        store0.promotion_threshold(),
        1,
        "Threshold 0 must be clamped to 1"
    );

    let problem = parse("sin(x)").expect("problem");
    let answer = parse("sin(x)").expect("answer");
    let v = Verdict::Verified {
        evidence: Evidence {
            points_tested: 16,
            max_relative_error: 0.0,
        },
    };
    let card = Card::from_verified(problem, answer, &v).expect("card");

    // Because threshold is clamped to 1, the first insert must immediately emit a proposal
    let candidate = store0.insert(card);
    assert!(
        candidate.is_some(),
        "Threshold clamped to 1 must propose on first insert"
    );
    let proposal = candidate.unwrap();
    assert_eq!(proposal.card_count(), 1);
    assert_eq!(proposal.status(), MethodStatus::Proposed);

    // Verify non-zero threshold does not clamp
    let store3 = CardStore::with_promotion_threshold(3);
    assert_eq!(store3.promotion_threshold(), 3);
}
