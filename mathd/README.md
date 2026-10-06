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

## Testing

```bash
cargo test
```

On the current owner's Windows host there is **no C linker** (no MSVC, no
MinGW), so `cargo check` / `clippy` / `fmt` work but `cargo test` cannot link
locally; the full test suite runs in CI (`.github/workflows/mathd.yml`). See
the B1 report in `progress.md` for the real transcripts, including the local
linker failure.

## Oracles

Per `docs/work-orders.md`, B1's gates are the round-trip property test and the
malformed corpus. External oracles arrive with later work orders: SymPy + mpmath
fixtures at B3, central finite differences at B4, mutation testing at B5.
