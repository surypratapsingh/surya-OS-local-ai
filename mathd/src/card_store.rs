//! Type-enforced verified card store and method promotion (work order B8).
//!
//! # Spec (`docs/work-orders.md` B8)
//! - Add the cache, keyed by the B2 structural hash.
//! - **Hard invariant, enforced by the type system:** a card cannot be constructed
//!   without a `Verdict::Verified` and its evidence. Make an unverified insert
//!   impossible to write, not merely discouraged — no public constructor, no
//!   `Default`, no deserialisation path that bypasses it.
//! - Add promotion: after N cards share a structural shape, emit a candidate
//!   **method** card. Method cards are proposals until a human confirms them;
//!   they never auto-promote.
//! - **Done when** a test demonstrates no code path in the crate can insert an
//!   unverified card, and a benchmark shows cache-hit lookup under 1 ms over a
//!   corpus of 100,000 cards.

use std::collections::HashMap;
use std::fmt;
use std::hash::{Hash, Hasher};

use crate::canonicalizer::{canonicalize, structural_hash};
use crate::parser::{BinOp, Expr, Function, UnaryOp};
use crate::verifier::{Evidence, Verdict};

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// Errors arising from card construction or store operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CardError {
    /// Attempted to construct a Card from a verdict that was not `Verified`.
    VerdictNotVerified { reason: String },
    /// Card or method not found.
    NotFound { id: u64 },
    /// Invalid operation on a method card.
    InvalidMethodState { reason: String },
}

impl fmt::Display for CardError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CardError::VerdictNotVerified { reason } => {
                write!(
                    f,
                    "cannot construct Card without Verdict::Verified: {}",
                    reason
                )
            }
            CardError::NotFound { id } => write!(f, "item with id {} not found", id),
            CardError::InvalidMethodState { reason } => {
                write!(f, "invalid method state: {}", reason)
            }
        }
    }
}

impl std::error::Error for CardError {}

// ---------------------------------------------------------------------------
// VerifiedWitness: type-level witness token
// ---------------------------------------------------------------------------

/// A witness token proving that a problem-answer pair has been verified by B5.
///
/// # Invariant
/// `VerifiedWitness` cannot be constructed without a `Verdict::Verified`.
/// Its internal fields are private, it has no `Default` implementation, and its
/// only constructor verifies the verdict variant.
#[derive(Debug, Clone, PartialEq)]
pub struct VerifiedWitness {
    evidence: Evidence,
    _private: (),
}

impl VerifiedWitness {
    /// Extract a `VerifiedWitness` from a `Verdict`.
    ///
    /// Returns `Err(CardError::VerdictNotVerified)` if the verdict is not `Verdict::Verified`.
    pub fn from_verdict(verdict: &Verdict) -> Result<Self, CardError> {
        match verdict {
            Verdict::Verified { evidence } => Ok(Self {
                evidence: evidence.clone(),
                _private: (),
            }),
            Verdict::Failed {
                counterexample,
                expected,
                got,
            } => Err(CardError::VerdictNotVerified {
                reason: format!(
                    "Verdict is Failed at {:?} (expected {}, got {})",
                    counterexample, expected, got
                ),
            }),
            Verdict::Unverifiable { reason } => Err(CardError::VerdictNotVerified {
                reason: format!("Verdict is Unverifiable: {}", reason),
            }),
            // Catch any future non-verified verdict variant (e.g. FailedDomain in V8)
            #[allow(unreachable_patterns)]
            _ => Err(CardError::VerdictNotVerified {
                reason: "Verdict is not Verified".to_string(),
            }),
        }
    }

    /// Access the underlying verification evidence.
    pub fn evidence(&self) -> &Evidence {
        &self.evidence
    }

    /// Number of domain points tested during verification.
    pub fn points_tested(&self) -> usize {
        self.evidence.points_tested
    }

    /// Maximum relative numerical error observed during verification.
    pub fn max_relative_error(&self) -> f64 {
        self.evidence.max_relative_error
    }
}

// ---------------------------------------------------------------------------
// Card: type-enforced verified knowledge card
// ---------------------------------------------------------------------------

/// A verified mathematical knowledge card.
///
/// # Type-system enforcement
/// A `Card` cannot be constructed without a `Verdict::Verified` and its evidence.
/// All fields are private, no `Default` implementation exists, and no unverified
/// construction or deserialization path is exposed.
///
/// ```compile_fail
/// use mathd::card_store::Card;
/// // Attempting to construct a Card directly fails: private fields.
/// let _ = Card {
///     problem: mathd::parser::Expr::Integer(1),
///     answer: mathd::parser::Expr::Integer(1),
///     structural_hash: 0,
///     evidence: mathd::verifier::Evidence { points_tested: 8, max_relative_error: 0.0 },
/// };
/// ```
///
/// ```compile_fail
/// use mathd::card_store::Card;
/// // Attempting to call Default fails: Default is not implemented.
/// let _ = Card::default();
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct Card {
    problem: Expr,
    answer: Expr,
    structural_hash: u64,
    evidence: Evidence,
}

impl Card {
    /// Construct a verified `Card` from a `VerifiedWitness`.
    ///
    /// Because `VerifiedWitness` can only be obtained from a `Verdict::Verified`,
    /// this constructor is guaranteed to accept only verified claims.
    pub fn from_witness(problem: Expr, answer: Expr, witness: &VerifiedWitness) -> Self {
        let hash = structural_hash(&problem);
        Self {
            problem,
            answer,
            structural_hash: hash,
            evidence: witness.evidence().clone(),
        }
    }

    /// Construct a verified `Card` directly from a `Verdict`.
    ///
    /// Returns `Err(CardError::VerdictNotVerified)` if `verdict` is not `Verdict::Verified`.
    pub fn from_verified(
        problem: Expr,
        answer: Expr,
        verdict: &Verdict,
    ) -> Result<Self, CardError> {
        let witness = VerifiedWitness::from_verdict(verdict)?;
        Ok(Self::from_witness(problem, answer, &witness))
    }

    /// The problem expression.
    pub fn problem(&self) -> &Expr {
        &self.problem
    }

    /// The verified answer expression.
    pub fn answer(&self) -> &Expr {
        &self.answer
    }

    /// The B2 structural hash of the problem.
    pub fn structural_hash(&self) -> u64 {
        self.structural_hash
    }

    /// The verification evidence attached to this card.
    pub fn evidence(&self) -> &Evidence {
        &self.evidence
    }

    /// Number of domain points tested during verification.
    pub fn points_tested(&self) -> usize {
        self.evidence.points_tested
    }

    /// Maximum relative numerical error observed during verification.
    pub fn max_relative_error(&self) -> f64 {
        self.evidence.max_relative_error
    }
}

// ---------------------------------------------------------------------------
// StructuralShape & MethodCard candidate promotion
// ---------------------------------------------------------------------------

/// Abstract structural shape of an expression AST.
///
/// Concrete literals and variable names are abstracted to generic placeholders,
/// capturing the structural operator skeleton of expressions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StructuralShape {
    Literal,
    Variable,
    BinOp {
        op: BinOp,
        left: Box<StructuralShape>,
        right: Box<StructuralShape>,
    },
    UnaryOp {
        op: UnaryOp,
        operand: Box<StructuralShape>,
    },
    Call {
        func: Function,
        args: Vec<StructuralShape>,
    },
}

impl Hash for StructuralShape {
    fn hash<H: Hasher>(&self, state: &mut H) {
        match self {
            StructuralShape::Literal => {
                1u8.hash(state);
            }
            StructuralShape::Variable => {
                2u8.hash(state);
            }
            StructuralShape::BinOp { op, left, right } => {
                3u8.hash(state);
                let op_byte = match op {
                    BinOp::Add => 1u8,
                    BinOp::Sub => 2u8,
                    BinOp::Mul => 3u8,
                    BinOp::Div => 4u8,
                    BinOp::Pow => 5u8,
                };
                op_byte.hash(state);
                left.hash(state);
                right.hash(state);
            }
            StructuralShape::UnaryOp { op, operand } => {
                4u8.hash(state);
                let op_byte = match op {
                    UnaryOp::Pos => 1u8,
                    UnaryOp::Neg => 2u8,
                };
                op_byte.hash(state);
                operand.hash(state);
            }
            StructuralShape::Call { func, args } => {
                5u8.hash(state);
                let func_byte = match func {
                    Function::Sin => 1u8,
                    Function::Cos => 2u8,
                    Function::Tan => 3u8,
                    Function::Exp => 4u8,
                    Function::Ln => 5u8,
                    Function::Sqrt => 6u8,
                    Function::Abs => 7u8,
                };
                func_byte.hash(state);
                args.len().hash(state);
                for arg in args {
                    arg.hash(state);
                }
            }
        }
    }
}

impl StructuralShape {
    /// Extract the structural shape from an expression AST.
    pub fn from_expr(expr: &Expr) -> Self {
        match expr {
            Expr::Integer(_) | Expr::Decimal(_) | Expr::Constant(_) => StructuralShape::Literal,
            Expr::Variable(_) => StructuralShape::Variable,
            Expr::BinOp { op, left, right } => StructuralShape::BinOp {
                op: *op,
                left: Box::new(StructuralShape::from_expr(left)),
                right: Box::new(StructuralShape::from_expr(right)),
            },
            Expr::UnaryOp { op, operand } => StructuralShape::UnaryOp {
                op: *op,
                operand: Box::new(StructuralShape::from_expr(operand)),
            },
            Expr::Call { func, args } => StructuralShape::Call {
                func: *func,
                args: args.iter().map(StructuralShape::from_expr).collect(),
            },
        }
    }

    /// Extract the canonical structural shape from an expression.
    ///
    /// Applies B2 canonicalization first so commutative subtrees match structurally.
    pub fn from_canonical(expr: &Expr) -> Self {
        let canon = canonicalize(expr);
        Self::from_expr(&canon)
    }

    /// Compute a deterministic 64-bit hash of this structural shape.
    pub fn shape_hash(&self) -> u64 {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.hash(&mut hasher);
        hasher.finish()
    }
}

impl fmt::Display for StructuralShape {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StructuralShape::Literal => write!(f, "$C"),
            StructuralShape::Variable => write!(f, "$x"),
            StructuralShape::BinOp { op, left, right } => {
                let op_str = match op {
                    BinOp::Add => "+",
                    BinOp::Sub => "-",
                    BinOp::Mul => "*",
                    BinOp::Div => "/",
                    BinOp::Pow => "^",
                };
                write!(f, "({} {} {})", left, op_str, right)
            }
            StructuralShape::UnaryOp { op, operand } => {
                let op_str = match op {
                    UnaryOp::Pos => "+",
                    UnaryOp::Neg => "-",
                };
                write!(f, "{}{}", op_str, operand)
            }
            StructuralShape::Call { func, args } => {
                let name = match func {
                    Function::Sin => "sin",
                    Function::Cos => "cos",
                    Function::Tan => "tan",
                    Function::Exp => "exp",
                    Function::Ln => "ln",
                    Function::Sqrt => "sqrt",
                    Function::Abs => "abs",
                };
                write!(f, "{}(", name)?;
                for (i, arg) in args.iter().enumerate() {
                    if i > 0 {
                        write!(f, ", ")?;
                    }
                    write!(f, "{}", arg)?;
                }
                write!(f, ")")
            }
        }
    }
}

/// Status of a method card.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MethodStatus {
    /// Proposal awaiting human confirmation.
    Proposed,
    /// Confirmed by human review.
    Confirmed,
    /// Rejected by human review.
    Rejected,
}

/// A candidate method card proposed after N cards share a structural shape.
///
/// # Promotion invariant
/// Method cards are proposals until a human confirms them; they **never** auto-promote.
#[derive(Debug, Clone, PartialEq)]
pub struct MethodCard {
    id: u64,
    shape: StructuralShape,
    exemplar_hashes: Vec<u64>,
    status: MethodStatus,
    card_count: usize,
}

impl MethodCard {
    /// Unique 64-bit identifier of this method card (shape hash).
    pub fn id(&self) -> u64 {
        self.id
    }

    /// The structural shape represented by this method card.
    pub fn shape(&self) -> &StructuralShape {
        &self.shape
    }

    /// Hashes of the exemplar cards that contributed to this proposal.
    pub fn exemplar_hashes(&self) -> &[u64] {
        &self.exemplar_hashes
    }

    /// Number of verified cards that have matched this structural shape.
    pub fn card_count(&self) -> usize {
        self.card_count
    }

    /// Current status of this method card.
    pub fn status(&self) -> MethodStatus {
        self.status
    }

    /// True if this method card is currently an unconfirmed proposal.
    pub fn is_proposed(&self) -> bool {
        self.status == MethodStatus::Proposed
    }

    /// True if a human has confirmed this method card.
    pub fn is_confirmed(&self) -> bool {
        self.status == MethodStatus::Confirmed
    }

    /// True if a human has rejected this method card.
    pub fn is_rejected(&self) -> bool {
        self.status == MethodStatus::Rejected
    }

    /// Human confirmation. Never called automatically.
    pub fn confirm(&mut self) {
        self.status = MethodStatus::Confirmed;
    }

    /// Human rejection. Never called automatically.
    pub fn reject(&mut self) {
        self.status = MethodStatus::Rejected;
    }
}

// ---------------------------------------------------------------------------
// CardStore: cache keyed by B2 structural hash
// ---------------------------------------------------------------------------

/// Card store cache keyed by the B2 structural hash.
///
/// Enforces the project invariant that only `Card` instances (which require
/// verification evidence to exist) can be inserted.
#[derive(Debug, Clone)]
pub struct CardStore {
    cards: HashMap<u64, Vec<Card>>,
    promotion_threshold: usize,
    shape_exemplars: HashMap<StructuralShape, Vec<u64>>,
    method_cards: HashMap<u64, MethodCard>,
    method_order: Vec<u64>,
    total_cards: usize,
}

impl Default for CardStore {
    fn default() -> Self {
        Self::new()
    }
}

impl CardStore {
    /// Create an empty `CardStore` with the default promotion threshold (3).
    pub fn new() -> Self {
        Self::with_promotion_threshold(3)
    }

    /// Create an empty `CardStore` with a custom promotion threshold `n`.
    pub fn with_promotion_threshold(n: usize) -> Self {
        let threshold = if n == 0 { 1 } else { n };
        Self {
            cards: HashMap::new(),
            promotion_threshold: threshold,
            shape_exemplars: HashMap::new(),
            method_cards: HashMap::new(),
            method_order: Vec::new(),
            total_cards: 0,
        }
    }

    /// Promotion threshold N for method candidate emission.
    pub fn promotion_threshold(&self) -> usize {
        self.promotion_threshold
    }

    /// Total number of verified cards stored.
    pub fn len(&self) -> usize {
        self.total_cards
    }

    /// True if the store contains no cards.
    pub fn is_empty(&self) -> bool {
        self.total_cards == 0
    }

    /// Insert a verified card into the store.
    ///
    /// If inserting this card causes the number of cards sharing its structural
    /// shape to reach the promotion threshold `N`, a new candidate `MethodCard`
    /// is emitted with status `MethodStatus::Proposed`.
    ///
    /// Subsequent cards sharing the same shape update the proposal's exemplar
    /// list and count, but NEVER auto-promote the card to `Confirmed`.
    pub fn insert(&mut self, card: Card) -> Option<MethodCard> {
        let hash = card.structural_hash();
        let shape = StructuralShape::from_canonical(card.problem());

        self.cards.entry(hash).or_default().push(card);
        self.total_cards += 1;

        let exemplars = self.shape_exemplars.entry(shape.clone()).or_default();
        exemplars.push(hash);
        let count = exemplars.len();

        let shape_id = shape.shape_hash();

        if count == self.promotion_threshold {
            // Threshold reached: emit a candidate proposal
            let proposal = MethodCard {
                id: shape_id,
                shape,
                exemplar_hashes: exemplars.clone(),
                status: MethodStatus::Proposed,
                card_count: count,
            };
            self.method_cards.insert(shape_id, proposal.clone());
            self.method_order.push(shape_id);
            Some(proposal)
        } else if let Some(existing) = self.method_cards.get_mut(&shape_id) {
            // Already proposed: update exemplars and count, but NEVER change status (never auto-promote)
            existing.exemplar_hashes.push(hash);
            existing.card_count = count;
            None
        } else {
            None
        }
    }

    /// Look up a card by its B2 structural hash.
    ///
    /// Returns the most recently inserted card for that structural hash.
    pub fn get(&self, hash: u64) -> Option<&Card> {
        self.cards.get(&hash).and_then(|bucket| bucket.last())
    }

    /// Look up all cards matching a B2 structural hash.
    pub fn get_all(&self, hash: u64) -> Option<&[Card]> {
        self.cards.get(&hash).map(|bucket| bucket.as_slice())
    }

    /// Look up a card by problem expression.
    ///
    /// Computes the B2 structural hash of `problem` and retrieves the cached card.
    pub fn get_by_expr(&self, problem: &Expr) -> Option<&Card> {
        let hash = structural_hash(problem);
        self.get(hash)
    }

    /// Check if a card with the given B2 structural hash exists in the cache.
    pub fn contains(&self, hash: u64) -> bool {
        self.cards.contains_key(&hash)
    }

    /// Check if a card for the given problem expression exists in the cache.
    pub fn contains_expr(&self, problem: &Expr) -> bool {
        let hash = structural_hash(problem);
        self.contains(hash)
    }

    /// Return all method cards in emission order.
    pub fn method_cards(&self) -> Vec<&MethodCard> {
        self.method_order
            .iter()
            .filter_map(|id| self.method_cards.get(id))
            .collect()
    }

    /// Return all candidate method cards that are currently unconfirmed proposals.
    pub fn proposed_methods(&self) -> Vec<&MethodCard> {
        self.method_cards()
            .into_iter()
            .filter(|m| m.is_proposed())
            .collect()
    }

    /// Return all method cards that have been confirmed by human review.
    pub fn confirmed_methods(&self) -> Vec<&MethodCard> {
        self.method_cards()
            .into_iter()
            .filter(|m| m.is_confirmed())
            .collect()
    }

    /// Look up a method card by its ID.
    pub fn get_method(&self, id: u64) -> Option<&MethodCard> {
        self.method_cards.get(&id)
    }

    /// Confirm a candidate method card proposal (human review action).
    pub fn confirm_method(&mut self, id: u64) -> Result<(), CardError> {
        match self.method_cards.get_mut(&id) {
            Some(method) => {
                method.confirm();
                Ok(())
            }
            None => Err(CardError::NotFound { id }),
        }
    }

    /// Reject a candidate method card proposal (human review action).
    pub fn reject_method(&mut self, id: u64) -> Result<(), CardError> {
        match self.method_cards.get_mut(&id) {
            Some(method) => {
                method.reject();
                Ok(())
            }
            None => Err(CardError::NotFound { id }),
        }
    }
}
