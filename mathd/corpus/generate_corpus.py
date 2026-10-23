#!/usr/bin/env python3
"""Generate B6 fixtures and check every proposed claim independently with SymPy.

All problems are marked generated because no textbook source was used.
The serialized `sympy_verification.call` fields are executable SymPy expressions;
answers are admitted only after the live SymPy result matches the recorded result.
Run from any directory with: python mathd/corpus/generate_corpus.py
"""

import json
from pathlib import Path
import re

import sympy as sp

HERE = Path(__file__).resolve().parent
GROUND_TRUTH = HERE / "ground_truth.jsonl"
WRONG_ANSWERS = HERE / "wrong_answers.jsonl"
SYMPY_VERSION = sp.__version__
x = sp.Symbol("x", real=True)


def render(expr):
    """Render SymPy expressions in mathd's explicit-multiplication grammar."""
    return sp.sstr(expr).replace("**", "^")


def sympy_call(call):
    """Evaluate one generated expression against the independent SymPy library."""
    if not re.fullmatch(r"[A-Za-z0-9_+*/^()., -]+", call):
        raise AssertionError(f"unexpected syntax in recorded SymPy call: {call!r}")
    namespace = {
        "x": x,
        "sin": sp.sin,
        "cos": sp.cos,
        "exp": sp.exp,
        "diff": sp.diff,
        "solve": sp.solve,
        "Eq": sp.Eq,
        "simplify": sp.simplify,
    }
    result = sp.sympify(call, locals=namespace)
    return sp.simplify(result) if isinstance(result, sp.Basic) else result


def record_check(call, expected_verified):
    result = sympy_call(call)
    verified = result == 0
    if verified != expected_verified:
        raise AssertionError(f"SymPy verdict mismatch: {call} -> {result}")
    return {"call": call, "result": render(result), "verified": verified}


def zero_check(call):
    return record_check(call, True)


def add_case(cases, kind, problem, claim, proof, wrongs):
    cases.append(
        {
            "id": f"{kind}_{len(cases) + 1:03d}",
            "source": "generated",
            "kind": kind,
            "problem": problem,
            **claim,
            "wrong_answers": wrongs,
            "sympy_version": SYMPY_VERSION,
            "sympy_verification": proof,
        }
    )
    if proof["verified"] is not True:
        raise AssertionError("the generated correct claim lacks a passing SymPy result")
    for wrong in wrongs:
        wrong_proof = wrong["sympy_verification"]
        if wrong_proof["verified"] is not False:
            raise AssertionError("a wrong-answer proof cannot be marked verified")
        if not wrong_proof["result"] or wrong_proof["result"] == "0":
            raise AssertionError("a wrong-answer proof must retain a nonzero residual")


def make_integrals(cases):
    entries = []
    for n in range(50):
        f = x**n
        antiderivative = x ** (n + 1) / sp.Integer(n + 1)
        entries.append((f, antiderivative, f"Integrate x^{n} with respect to x"))
    for k in range(1, 26):
        entries.append((sp.sin(k * x), -sp.cos(k * x) / k, f"Integrate sin({k}x) with respect to x"))
    for k in range(1, 26):
        entries.append((sp.cos(k * x), sp.sin(k * x) / k, f"Integrate cos({k}x) with respect to x"))
    for k in range(1, 26):
        entries.append((sp.exp(k * x), sp.exp(k * x) / k, f"Integrate exp({k}x) with respect to x"))

    for f, answer, problem in entries:
        f_text, answer_text = render(f), render(answer)
        # Execute the oracle on SymPy expressions, not on mathd's parser/evaluator.
        proof = zero_check(
            f"simplify(diff({sp.sstr(answer)}, x) - ({sp.sstr(f)}))"
        )
        wrongs = []
        for delta in (x, -x):
            wrong = answer + delta
            wrong_text = render(wrong)
            wrong_call = f"simplify(diff({sp.sstr(wrong)}, x) - ({sp.sstr(f)}))"
            wrong_result = sympy_call(wrong_call)
            if wrong_result == 0:
                raise AssertionError(f"wrong integral unexpectedly correct: {wrong_call}")
            wrongs.append({
                "claimed_antiderivative": wrong_text,
                "rationale": "coefficient error in the antiderivative",
                "sympy_verification": record_check(wrong_call, False),
            })
        add_case(
            cases,
            "integral",
            problem,
            {"integrand": f_text, "claimed_antiderivative": answer_text, "var": "x"},
            proof,
            wrongs,
        )


def make_equations(cases):
    entries = []
    for a in range(1, 33):
        entries.append(((x - a) * (x + a), [sp.Integer(-a), sp.Integer(a)], f"Solve x^2 - {a*a} = 0"))
    for root in range(-15, 16):
        entries.append((2 * x - 2 * root, [sp.Integer(root)], f"Solve 2x - {2*root} = 0"))
    for a in range(1, 32):
        entries.append(((x - a) * (x - (a + 3)), [sp.Integer(a), sp.Integer(a + 3)], f"Solve (x - {a})(x - {a+3}) = 0"))
    for a in range(1, 32):
        entries.append((x * (x - a) * (x + a), [sp.Integer(-a), sp.Integer(0), sp.Integer(a)], f"Solve x(x^2 - {a*a}) = 0"))

    for lhs, roots, problem in entries:
        lhs_text = render(lhs)
        solve_call = f"solve(Eq({sp.sstr(lhs)}, 0), x)"
        solutions = sympy_call(solve_call)
        if any(solution.is_real is not True for solution in solutions):
            raise AssertionError(f"non-real equation solution not supported by this corpus: {problem}")
        complete = len(solutions) == len(roots) and all(
            any(sp.simplify(solution - root) == 0 for root in roots) for solution in solutions
        )
        if not complete:
            raise AssertionError(f"SymPy root set mismatch for {problem}: {solutions} != {roots}")
        proof = {
            "call": solve_call,
            "result": "[" + ", ".join(render(root) for root in solutions) + "]",
            "verified": complete,
        }
        wrongs = []
        bad_roots = (max(roots) + 1, max(roots) + 2)
        for bad in bad_roots:
            wrong_call = f"simplify(({sp.sstr(lhs)}).subs(x, {int(bad)}) - 0)"
            residual = sympy_call(wrong_call)
            if residual == 0:
                raise AssertionError(f"wrong root unexpectedly solves {problem}: {bad}")
            wrongs.append({
                "claimed_roots": [int(bad)],
                "rationale": "off-by-one root",
                "sympy_verification": record_check(wrong_call, False),
            })
        add_case(cases, "equation", problem, {"lhs": lhs_text, "rhs": "0", "var": "x", "claimed_roots": [int(r) for r in roots]}, proof, wrongs)


def make_identities(cases):
    entries = []
    for a in range(1, 33):
        entries.append(((x + a) ** 2, x**2 + 2*a*x + a**2, f"Expand (x + {a})^2"))
    for a in range(1, 32):
        entries.append(((x - a) * (x + a), x**2 - a**2, f"Expand (x - {a})(x + {a})"))
    for k in range(1, 32):
        u = k * x
        entries.append((sp.sin(u) ** 2 + sp.cos(u) ** 2, sp.Integer(1), f"Simplify sin({k}x)^2 + cos({k}x)^2"))
    for k in range(1, 32):
        u = k * x
        entries.append((sp.sin(2*u), 2 * sp.sin(u) * sp.cos(u), f"Simplify sin({2*k}x)"))

    for lhs, rhs, problem in entries:
        lhs_text, rhs_text = render(lhs), render(rhs)
        proof = zero_check(f"simplify(({sp.sstr(lhs)}) - ({sp.sstr(rhs)}))")
        wrongs = []
        for offset in (1, -1):
            wrong_rhs = rhs + offset
            wrong_text = render(wrong_rhs)
            wrong_call = f"simplify(({sp.sstr(lhs)}) - ({sp.sstr(wrong_rhs)}))"
            residual = sympy_call(wrong_call)
            if residual == 0:
                raise AssertionError(f"wrong identity unexpectedly correct: {problem} -> {wrong_text}")
            wrongs.append({
                "rhs": wrong_text,
                "rationale": "constant/sign error",
                "sympy_verification": record_check(wrong_call, False),
            })
        add_case(cases, "identity", problem, {"lhs": lhs_text, "rhs": rhs_text, "vars": ["x"]}, proof, wrongs)


def make_factorisations(cases):
    entries = []
    for a in range(1, 43):
        entries.append((x**2 - a**2, [x - a, x + a], f"Factor x^2 - {a*a}"))
    for a in range(1, 43):
        entries.append((x**2 + 2*a*x + a**2, [x + a, x + a], f"Factor x^2 + {2*a}x + {a*a}"))
    for a in range(1, 42):
        b = a + 2
        entries.append(((x + a) * (x - b), [x + a, x - b], f"Factor (x + {a})(x - {b})"))

    for original, factors, problem in entries:
        original_text = render(original)
        factors_text = [render(factor) for factor in factors]
        proof = zero_check(
            f"simplify(({sp.sstr(original)}) - ({' * '.join(f'({sp.sstr(factor)})' for factor in factors)}))"
        )
        wrongs = []
        for offset in (1, -1):
            mutant = factors[:-1] + [factors[-1] + offset]
            mutant_text = [render(factor) for factor in mutant]
            wrong_call = f"simplify(({sp.sstr(original)}) - ({' * '.join(f'({sp.sstr(factor)})' for factor in mutant)}))"
            residual = sympy_call(wrong_call)
            if residual == 0:
                raise AssertionError(f"wrong factorisation unexpectedly correct: {problem}")
            wrongs.append({
                "claimed_factors": mutant_text,
                "rationale": "sign/coefficient error in a factor",
                "sympy_verification": record_check(wrong_call, False),
            })
        add_case(cases, "factorisation", problem, {"original": original_text, "claimed_factors": factors_text}, proof, wrongs)


def main():
    cases = []
    make_integrals(cases)
    make_equations(cases)
    make_identities(cases)
    make_factorisations(cases)

    if len(cases) != 500:
        raise AssertionError(f"expected 500 generated problems, got {len(cases)}")
    counts = {kind: sum(case["kind"] == kind for case in cases) for kind in ("integral", "equation", "identity", "factorisation")}
    if set(counts.values()) != {125}:
        raise AssertionError(f"unexpected category counts: {counts}")

    with GROUND_TRUTH.open("w", encoding="utf-8", newline="\n") as stream:
        for case in cases:
            stream.write(json.dumps(case, sort_keys=True) + "\n")
    with WRONG_ANSWERS.open("w", encoding="utf-8", newline="\n") as stream:
        for case in cases:
            entry = {"id": case["id"], "kind": case["kind"], "wrong_answers": case["wrong_answers"]}
            stream.write(json.dumps(entry, sort_keys=True) + "\n")

    print(f"SymPy {SYMPY_VERSION}")
    print(f"Generated {len(cases)} problems: {counts}")
    print(f"Ground-truth SymPy checks: {len(cases)}/{len(cases)} verified")
    wrong_count = sum(len(case["wrong_answers"]) for case in cases)
    print(f"Wrong-answer SymPy checks: {wrong_count}/{wrong_count} confirmed non-equivalent")
    print(f"Wrote {GROUND_TRUTH.relative_to(HERE.parents[1])}")
    print(f"Wrote {WRONG_ANSWERS.relative_to(HERE.parents[1])}")


if __name__ == "__main__":
    main()
