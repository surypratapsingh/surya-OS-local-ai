# mathd — CAS-verified math expression verifier

A std-only Rust library for parsing, canonicalizing, and verifying mathematical expressions. No external dependencies, no network access.

## Architecture

- **B1 (parser)**: Text → AST. Handles numbers, variables, binary/unary operators, function calls, parentheses.
- **B2 (canonicalizer)**: AST → canonical form. Normalize associativity, commutativity, like terms.
- **B3 (numeric evaluator)**: AST → number with SymPy oracle verification.
- **B4 (differentiator)**: AST → derivative with finite differences oracle.
- **B5 (verifier)**: Check answers using mutation testing as independent oracle.
- **B6 (adversarial corpus)**: Test cases covering edge cases, traps, real student errors.
- **B7 (prove-it CLI)**: Owner solves, machine verifies. Saves verified answers to card store.
- **B8 (card store)**: Append-only verified-answer log with chaining (like memory.py).

## Current Status

**B1 (parser)** is complete:
- Lexer with full tokenization
- Recursive descent parser with proper precedence
- AST representation
- 6 unit tests covering numbers, variables, operators, function calls, precedence, errors

## Testing (requires Rust toolchain)

```bash
cargo test --lib parser
```

## Oracles (future)

- **SymPy** (B3): `sympy.sympify()` to parse, `float()` to evaluate
- **Finite differences** (B4): Numerically compute derivatives
- **Mutation testing** (B5): Flip operators, change constants; correct verifier must reject
