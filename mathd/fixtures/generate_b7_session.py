#!/usr/bin/env python3
"""B7 20-problem Prove-it session generator (work order B7).

Generates 20 mathematical claims across 4 distinct claim types:
  1. Identity / Equivalence
  2. Integration
  3. Equation solving
  4. Factorisation

The ground-truth oracle is SymPy. Every claim is mathematically analyzed by SymPy:
- 15 valid claims are proven mathematically true (expected verdict: Verified)
- 5 deliberate mistakes are proven mathematically false (expected verdict: Failed)

Run:
    python mathd/fixtures/generate_b7_session.py
Outputs:
    mathd/fixtures/b7_session.json
"""

import json
import os
from pathlib import Path
import sympy as sp

SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = SCRIPT_DIR / "b7_session.json"

x, y = sp.symbols("x y")

# ---------------------------------------------------------------------------
# Problem definitions
# ---------------------------------------------------------------------------

PROBLEMS = [
    # -----------------------------------------------------------------------
    # 1. Identities (6 problems: 4 valid, 2 deliberate mistakes)
    # -----------------------------------------------------------------------
    {
        "id": "b7-ident-01",
        "claim_type": "identity",
        "lhs": "(x + 3)^2",
        "rhs": "x^2 + 6*x + 9",
        "vars": ["x"],
        "is_correct": True,
        "description": "Quadratic binomial expansion",
    },
    {
        "id": "b7-ident-02",
        "claim_type": "identity",
        "lhs": "(x - 2) * (x + 2)",
        "rhs": "x^2 - 4",
        "vars": ["x"],
        "is_correct": True,
        "description": "Difference of squares",
    },
    {
        "id": "b7-ident-03",
        "claim_type": "identity",
        "lhs": "sin(x)^2 + cos(x)^2",
        "rhs": "1",
        "vars": ["x"],
        "is_correct": True,
        "description": "Pythagorean trigonometric identity",
    },
    {
        "id": "b7-ident-04",
        "claim_type": "identity",
        "lhs": "(x + y)^2",
        "rhs": "x^2 + 2*x*y + y^2",
        "vars": ["x", "y"],
        "is_correct": True,
        "description": "Bivariate binomial expansion",
    },
    {
        "id": "b7-ident-05-fail",
        "claim_type": "identity",
        "lhs": "(x + 1)^2",
        "rhs": "x^2 + 1",
        "vars": ["x"],
        "is_correct": False,
        "description": "Freshman mistake: dropped linear cross-term",
    },
    {
        "id": "b7-ident-06-fail",
        "claim_type": "identity",
        "lhs": "cos(x)^2 + sin(x)^2",
        "rhs": "0",
        "vars": ["x"],
        "is_correct": False,
        "description": "Trig identity with wrong constant",
    },
    # -----------------------------------------------------------------------
    # 2. Integrals (5 problems: 4 valid, 1 deliberate mistake)
    # -----------------------------------------------------------------------
    {
        "id": "b7-integ-07",
        "claim_type": "integral",
        "integrand": "3*x^2 + 4*x",
        "antiderivative": "x^3 + 2*x^2",
        "var": "x",
        "is_correct": True,
        "description": "Polynomial antiderivative",
    },
    {
        "id": "b7-integ-08",
        "claim_type": "integral",
        "integrand": "cos(x)",
        "antiderivative": "sin(x)",
        "var": "x",
        "is_correct": True,
        "description": "Trigonometric antiderivative",
    },
    {
        "id": "b7-integ-09",
        "claim_type": "integral",
        "integrand": "exp(x) + 2*x",
        "antiderivative": "exp(x) + x^2",
        "var": "x",
        "is_correct": True,
        "description": "Exponential and polynomial sum antiderivative",
    },
    {
        "id": "b7-integ-10",
        "claim_type": "integral",
        "integrand": "4*x^3 - 6*x",
        "antiderivative": "x^4 - 3*x^2",
        "var": "x",
        "is_correct": True,
        "description": "Cubic polynomial antiderivative",
    },
    {
        "id": "b7-integ-11-fail",
        "claim_type": "integral",
        "integrand": "3*x^2 + 4*x",
        "antiderivative": "x^3 + 4*x^2",
        "var": "x",
        "is_correct": False,
        "description": "Integration power rule constant error",
    },
    # -----------------------------------------------------------------------
    # 3. Equations (5 problems: 4 valid, 1 deliberate mistake)
    # -----------------------------------------------------------------------
    {
        "id": "b7-eqn-12",
        "claim_type": "equation",
        "lhs": "x^2 - 5*x + 6",
        "rhs": "0",
        "roots": [2.0, 3.0],
        "var": "x",
        "is_correct": True,
        "description": "Quadratic equation with distinct roots",
    },
    {
        "id": "b7-eqn-13",
        "claim_type": "equation",
        "lhs": "x^2 - 4",
        "rhs": "0",
        "roots": [-2.0, 2.0],
        "var": "x",
        "is_correct": True,
        "description": "Quadratic pure equation with symmetric roots",
    },
    {
        "id": "b7-eqn-14",
        "claim_type": "equation",
        "lhs": "2*x - 10",
        "rhs": "0",
        "roots": [5.0],
        "var": "x",
        "is_correct": True,
        "description": "Linear equation",
    },
    {
        "id": "b7-eqn-15",
        "claim_type": "equation",
        "lhs": "x^2 - 2*x + 1",
        "rhs": "0",
        "roots": [1.0],
        "var": "x",
        "is_correct": True,
        "description": "Quadratic equation with double root",
    },
    {
        "id": "b7-eqn-16-fail",
        "claim_type": "equation",
        "lhs": "x^2 - 5*x + 6",
        "rhs": "0",
        "roots": [2.0, 4.0],
        "var": "x",
        "is_correct": False,
        "description": "Quadratic equation with an invalid root 4.0",
    },
    # -----------------------------------------------------------------------
    # 4. Factorisation (4 problems: 3 valid, 1 deliberate mistake)
    # -----------------------------------------------------------------------
    {
        "id": "b7-fact-17",
        "claim_type": "factorisation",
        "original": "x^2 - 9",
        "factors": ["x - 3", "x + 3"],
        "is_correct": True,
        "description": "Difference of squares factorisation",
    },
    {
        "id": "b7-fact-18",
        "claim_type": "factorisation",
        "original": "x^2 + 5*x + 6",
        "factors": ["x + 2", "x + 3"],
        "is_correct": True,
        "description": "Monic quadratic factorisation",
    },
    {
        "id": "b7-fact-19",
        "claim_type": "factorisation",
        "original": "2*x^2 + 4*x",
        "factors": ["2*x", "x + 2"],
        "is_correct": True,
        "description": "Common monomial factor extraction",
    },
    {
        "id": "b7-fact-20-fail",
        "claim_type": "factorisation",
        "original": "x^2 - 9",
        "factors": ["x - 3", "x - 3"],
        "is_correct": False,
        "description": "Factorisation with sign error in second factor",
    },
]

def to_sympy_expr(expr_str: str) -> sp.Expr:
    # mathd uses ^ for power, SymPy uses **
    py_expr = expr_str.replace("^", "**")
    return sp.parse_expr(py_expr, local_dict={"x": x, "y": y})

def verify_with_sympy(p: dict) -> str:
    ctype = p["claim_type"]
    if ctype == "identity":
        e_lhs = to_sympy_expr(p["lhs"])
        e_rhs = to_sympy_expr(p["rhs"])
        diff = sp.simplify(e_lhs - e_rhs)
        return "Verified" if diff == 0 else "Failed"
    elif ctype == "integral":
        integrand = to_sympy_expr(p["integrand"])
        antideriv = to_sympy_expr(p["antiderivative"])
        var_sym = sp.Symbol(p["var"])
        diff = sp.simplify(sp.diff(antideriv, var_sym) - integrand)
        return "Verified" if diff == 0 else "Failed"
    elif ctype == "equation":
        e_lhs = to_sympy_expr(p["lhs"])
        e_rhs = to_sympy_expr(p["rhs"])
        diff = e_lhs - e_rhs
        var_sym = sp.Symbol(p["var"])
        for r in p["roots"]:
            val = float(diff.subs(var_sym, r))
            if abs(val) > 1e-9:
                return "Failed"
        return "Verified"
    elif ctype == "factorisation":
        orig = to_sympy_expr(p["original"])
        prod = sp.Integer(1)
        for f in p["factors"]:
            prod = prod * to_sympy_expr(f)
        diff = sp.simplify(orig - prod)
        return "Verified" if diff == 0 else "Failed"
    else:
        raise ValueError(f"Unknown claim_type: {ctype}")

def main():
    print(f"Generating {len(PROBLEMS)} B7 session problems via SymPy oracle...")
    output_cases = []
    verified_count = 0
    failed_count = 0

    for p in PROBLEMS:
        oracle_verdict = verify_with_sympy(p)
        expected = "Verified" if p["is_correct"] else "Failed"
        assert oracle_verdict == expected, (
            f"SymPy oracle verdict '{oracle_verdict}' does not match expected '{expected}' for {p['id']}"
        )

        # Build claim string representation
        ctype = p["claim_type"]
        if ctype == "identity":
            claim_str = f"identity: {p['lhs']} == {p['rhs']}"
        elif ctype == "integral":
            claim_str = f"integral: {p['integrand']} -> {p['antiderivative']}"
        elif ctype == "equation":
            roots_str = ", ".join(str(r) for r in p["roots"])
            claim_str = f"equation: {p['lhs']} == {p['rhs']} [roots: {roots_str}]"
        elif ctype == "factorisation":
            factors_str = " * ".join(f"({f})" for f in p["factors"])
            claim_str = f"factorisation: {p['original']} == {factors_str}"
        else:
            claim_str = p["id"]

        case = {
            "id": p["id"],
            "claim_type": ctype,
            "claim_str": claim_str,
            "expected_verdict": oracle_verdict,
            "description": p["description"],
        }
        for k in ["lhs", "rhs", "vars", "integrand", "antiderivative", "var", "roots", "original", "factors"]:
            if k in p:
                case[k] = p[k]

        output_cases.append(case)
        if oracle_verdict == "Verified":
            verified_count += 1
        else:
            failed_count += 1

    print(f"Total problems: {len(output_cases)} (Verified: {verified_count}, Failed: {failed_count})")
    assert verified_count == 15, f"Expected 15 Verified, got {verified_count}"
    assert failed_count == 5, f"Expected 5 Failed, got {failed_count}"

    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(output_cases, f, indent=2)
    print(f"Committed fixture written to {FIXTURE_PATH}")

if __name__ == "__main__":
    main()
