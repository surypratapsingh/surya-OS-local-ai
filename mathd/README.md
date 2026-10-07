# mathd — std-only math expression parser

Part of NOVA Phase B. std-only Rust, no dependencies, no I/O, no kernel code.

> **STATUS 2026-10-06: B1 (parser and AST) is real and passes its checks.**
> Everything else in this crate was retracted by `docs/audit-2026-09-24.md`
> (transcript: `docs/logs/audit-2026-09-24-mathd.log`) and has been **removed**,
> not stubbed: the old canonicalizer, evaluator, differentiator, verifier,
> corpus and card store never compiled, depended on non-std crates (`sha2`,
> `chrono`), and none of their checks had ever run. They are preserved in git
> history up to the audit commit and will be rebuilt per work order (B2–B8),
> each against its specified external oracle.

## What exists now (B1)

- **Parser** ([`src/parser.rs`](src/parser.rs)): integers (`i64`), decimals
  (`f64`), named constants (`pi`/`π`, `e`), ASCII variables, `+ - * / ^`,
  unary minus, parentheses, and the functions `sin cos tan exp ln sqrt abs`
  (one argument each). Unicode input: `·` `⋅` `×` (multiply), `÷` (divide),
  `−` (minus), `√` (square root, binds like unary minus), `π`.
- **Canonical printing** (`mathd::print`): renders an AST back to text that
  parses to the structurally identical AST.
- **Round-trip property test** ([`tests/roundtrip.rs`](tests/roundtrip.rs)):
  10,000 generated expressions (seeded, deterministic), `parse(print(ast))`
  must be structurally identical to `ast`.
- **Malformed-input corpus**: 50 hand-written bad inputs, each asserting a
  specific error message — never a panic.
- **CLI** (`src/main.rs`): `mathd parse "<expr>"` and
  `mathd roundtrip "<expr>"`.

### Documented language decisions

Each of these is deliberate; they are listed so nobody has to reverse-engineer
them from the code:

- `-x^2` parses as `-(x^2)` (standard algebraic convention); `(-x)^2` needs
  explicit parentheses. The pre-rewrite parser did the opposite while its own
  comment claimed otherwise.
- `^` is right-associative: `2^3^2` = `2^(3^2)`; `2^-3` parses.
- No rational literal syntax: `3/4` is `Div(Integer(3), Integer(4))`.
  B2 canonicalisation may give rationals a normal form.
- `pi`, `π`, `e` are reserved constants; `e` being a constant means decimal
  literals like `1e5` are **not** supported (they would be ambiguous with
  `1*e`).
- Function names are reserved: bare `sin` is a specific error, not a variable.
- Implicit multiplication (`2x`, `2(x+1)`) is not supported and produces an
  error suggesting `*`.
- Identifiers are ASCII. Only the unicode forms listed above are accepted;
  other unicode letters (e.g. `α`) are `unexpected character` errors.
- Integers are `i64`; an out-of-range integer literal is an error, never a
  silent wrap. Decimals are non-negative and finite by construction.
- Recursion depth is capped at 500; pathological nesting returns
  `expression exceeds maximum nesting depth of 500` instead of overflowing
  the stack.

## Canonical form (B2)

`canonicalize(&Expr) -> Expr` rewrites every expression into one normal
form, so two spellings of the same object become structurally identical.
The rules (R1-R10 in `src/canonicalizer.rs`, each pinned by a named unit
test):

- R1 unary plus drops; R2 integer negation folds (`-5` -> `Integer(-5)`,
  `i64::MIN` stays `Neg` because its negation does not exist); R3 double
  negation cancels; R4 distributes minus over sums only (`-(x+y)` ->
  `-x - y`; `-(x*y)` keeps the minus on the product).
- R5 sums flatten to signed term lists; integer terms fold with checked
  arithmetic (all-or-nothing on overflow); `+0` terms drop; `1-1` -> `0`.
- R6 products flatten to numerator/denominator factor lists (`/` swaps
  sides); integer factors fold and cancel by gcd (`6/4` -> `3/2`); a zero
  factor folds the product to `0`. **No symbolic cancellation**:
  `(x*y)/(y*z)` stays as it is.
- R7 trivial factors (`*1`, `/1`) drop; bare `1` stays `1`.
- R8 `a/b` and `a*b^-1` meet in one Div form; `b^-n` -> `1/b^n`.
- R9 integer powers fold (`2^10` -> `1024`); `x^0` -> `1` for every base
  including `0`; `0^-n` -> `1/0`; an `i64::MIN` exponent stays unfolded
  (it has no positive counterpart).
- R10 commutative operands sort into a hand-written total order
  (`cmp_expr`) — `f64` blocks a derived `Ord`.

Documented corner decisions (deliberate; listed so nobody has to
reverse-engineer them from the code):

- `x/0` is preserved as `x / 0`, never folded; B3's evaluator must reject
  it at evaluation time.
- `0/0` folds to `0` under the zero-factor rule. Deliberate; B3 must
  reject the domain at evaluation.
- Integer folds are all-or-nothing: if any fold overflows, every factor
  survives unfolded and sorted (e.g. `9223372036854775807 * 3 * x` keeps
  all three factors).
- A product-wide minus reattaches as `Neg` over the sorted form, or folds
  into the leading integer (`(-2)*x` -> `-(2 * x)`); a minus never wraps a
  bare sum (`-(x+y)` is always `-x - y`).

## Structural hash

`structural_hash(&Expr) -> u64` is FNV-1a 64 over the canonical form's
deterministic serialisation (variant tag, payload, children — `serialize`
in `src/canonicalizer.rs`). Two expressions hash identically exactly when
their canonical forms are equal; `hash_canonical` exists for
already-canonical input.

## Testing

```bash
cargo test
```

B2 adds `tests/canonical.rs`: agreement (10,000 equivalent-by-construction
pairs must hash identically, zero exceptions) and separation (10,000
canonically-distinct pairs, more than 1 collision means wrong), plus
determinism and idempotence checks. Equivalence pairs are constructed by
rewriting laws applied to random base expressions, never by calling the
canonicaliser, so the test is not comparing the code to itself.

On the current owner's Windows host there is **no C linker** (no MSVC, no
MinGW), so `cargo check` / `clippy` / `fmt` work but `cargo test` cannot link
locally; the full test suite runs in CI (`.github/workflows/mathd.yml`). Locally
the property gates ran as real mathd code compiled to wasm32 and executed under
node (`build/wasmrunner/`, gitignored): 20,100 B2 cases green alongside the
10,121 B1 cases. See the B1/B2 reports in `progress.md` for the real
transcripts, including the local linker failure.

## Oracles

Per `docs/work-orders.md`, B1's gates are the round-trip property test and the
malformed corpus. External oracles arrive with later work orders: SymPy + mpmath
fixtures at B3, central finite differences at B4, mutation testing at B5.
