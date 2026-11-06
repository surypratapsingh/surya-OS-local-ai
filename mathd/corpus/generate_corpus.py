#!/usr/bin/env python3
"""B6 adversarial corpus generator.

Work order B6 (`docs/work-orders.md`) and the 2026-10-24 lead review
(`docs/reviews/sprint-1024-claude.log` section 3, findings F1-F3).

Writes three JSONL fixtures next to this script:

  ground_truth.jsonl  every correct claim, confirmed by SymPy, with the exact
                      oracle call recorded.
  wrong_answers.jsonl one row per ground-truth row, holding that problem's
                      realistic wrong answers; each one is proved wrong by
                      SymPy and the call is recorded.
  domain_claims.jsonl claims that are true only on part of the real line
                      (F3), each carrying its V8 strict-rule verdict: a real
                      counterexample point SymPy reproduces (`expect: reject`)
                      or the one must-verify control row (`expect: verify`).

No value is hand-written: every antiderivative comes from
`sympy.integrate`, every correct root list from `sympy.solve`, every correct
factorisation from `sympy.expand`, and every printed string is round-tripped
through `sympy.sympify` and checked against the expression it stands for.

Topic coverage (F1). `substitution`, `integration_by_parts`, `logarithms`,
`trig_identities`, `polynomial_equations`, `rational_equations`. Two syllabus
topics are reported as NOT EXPRESSIBLE rather than silently dropped:

  * definite integrals -- `verify_integral` differentiates the claimed
    antiderivative and compares it with the integrand; there is no bounds form
    in the B5 API, so no definite-integral claim can be stated.
  * inverse trigonometry -- mathd's grammar has exactly
    {sin, cos, tan, exp, ln, sqrt, abs}; there is no asin/atan/acos, so neither
    `int 1/(1+x^2) dx = atan(x)` nor the F2 kind "wrong inverse-trig sign" can
    be written at all.

Wrong-answer kinds (F2). Every problem carries at least one wrong answer whose
`rationale` starts with one of these labels:

  sign error after integration by parts      expressible
  omitted chain rule factor                  expressible
  dropped multiplicative constant            expressible
  wrong root: sign error                     expressible
  wrong root: exponent slip                  expressible
  an extraneous root (squaring introduced it) expressible
  misremembered trig identity                expressible
  wrong factor (quadratic split)             expressible
  domain claim (part of the real line only)  V8 strict rule (see F3)
  a missing root                             NOT EXPRESSIBLE (B5 checks only
                                             that claimed roots solve the
                                             equation, never completeness)
  wrong inverse-trig sign                    NOT EXPRESSIBLE (no inverse trig
                                             functions in the grammar)

There is no "+x"/"+1" filler: each wrong answer is the named student error,
built mechanically from the integrand (or, for roots and factors, from the
correct answer), and then *disproved* by SymPy before it is written.

Domain claims (F3). `sqrt(x^2) = x` and `ln(x^2) = 2*ln(x)` are both present.
V8 (owner's strict rule, `decisions.md` 2026-10-30; `docs/team.md` 09:05):
each row records `expect` -- `reject` with a real counterexample point (one
side real and the other not, or both real with different values), or
`verify` for the control row, B6 domain-015 `ln(x^3) = 3*ln(x)`, a true claim
that looks like the trap so strict is shown not to over-reject.  The old
`expressible` flag is gone: those claims do parse and `verify_identity` does
run on them (finding N2); 16 of 17 are rejected, 1 of 17 is verified.

Run:  python mathd/corpus/generate_corpus.py
"""

import json
from pathlib import Path

import sympy

HERE = Path(__file__).resolve().parent
GT_PATH = HERE / "ground_truth.jsonl"
WRONG_PATH = HERE / "wrong_answers.jsonl"
DOMAIN_PATH = HERE / "domain_claims.jsonl"

x = sympy.Symbol("x", real=True)

KIND_PARTS_SIGN = "sign error after integration by parts"
KIND_CHAIN = "omitted chain rule factor"
KIND_CONST = "dropped multiplicative constant"
KIND_ROOT_SIGN = "wrong root: sign error"
KIND_ROOT_POWER = "wrong root: exponent slip"
KIND_EXTRANEOUS = "an extraneous root (squaring introduced it)"
KIND_TRIG = "misremembered trig identity"
KIND_FACTOR = "wrong factor (quadratic split)"
KIND_DOMAIN = "domain claim (part of the real line only)"
KIND_MISSING_ROOT = "a missing root"
KIND_INVTRIG_SIGN = "wrong inverse-trig sign"

NOT_EXPRESSIBLE = {
    KIND_MISSING_ROOT: (
        "verify_equation_solution only checks that each claimed root solves the "
        "equation; it never checks that the claimed set is complete, so an "
        "incomplete root list is accepted"
    ),
    KIND_INVTRIG_SIGN: (
        "mathd has no asin/atan/acos, so no inverse-trigonometric claim can be "
        "written in the claim language at all"
    ),
}

TOPICS_NOT_EXPRESSIBLE = {
    "inverse_trigonometry": (
        "no asin/atan/acos in the grammar: Function is "
        "{sin, cos, tan, exp, ln, sqrt, abs}"
    ),
    "definite_integrals": (
        "verify_integral takes an integrand and a claimed antiderivative only; "
        "there are no integration bounds in the B5 API"
    ),
}


# --------------------------------------------------------------- printing


def msp(expr):
    """Print a SymPy expression in mathd syntax and prove the text means it.

    SymPy's plain string form is used as the base (never a hand-rolled
    precedence game), then `**` -> `^`, `log(` -> `ln(`, `Abs(` -> `abs(`.
    The text is parsed back with SymPy itself and compared to `expr`, so a
    precedence change introduced by the rewrite cannot slip through.
    """
    text = sympy.sstr(expr)
    if "Piecewise" in text or "Integral(" in text:
        raise AssertionError(f"unprintable expression: {text!r}")
    mathd_text = text.replace("**", "^").replace("log(", "ln(").replace("Abs(", "abs(")
    back = mathd_text.replace("^", "**").replace("ln(", "log(").replace("abs(", "Abs(")
    # `locals` keeps the symbol identical to the one used to build `expr`, so
    # the comparison is not between two differently-assumed symbols.
    residual = sympy.simplify(sympy.sympify(back, locals={"x": x}) - expr)
    if residual != 0:
        raise AssertionError(
            f"printer round-trip failed: {mathd_text!r} for {expr!r} "
            f"(residual {residual})"
        )
    for forbidden in ("**", "log(", "Abs(", "E"):
        if forbidden in mathd_text:
            raise AssertionError(f"{forbidden!r} leaked into {mathd_text!r}")
    return mathd_text


def sympy_text(expr):
    """The same expression in SymPy syntax, for the recorded oracle call."""
    return msp(expr).replace("^", "**")


# --------------------------------------------------------------- row builders

gt_rows = []
wrong_rows = []
domain_rows = []


def _push(topic, kind, fields, wrongs):
    case_id = f"{topic}-{sum(1 for r in gt_rows if r['topic'] == topic) + 1:03d}"
    row = {
        "id": case_id,
        "topic": topic,
        "kind": kind,
        "source": "generated",
    }
    row.update(fields)
    gt_rows.append(row)
    wrong_rows.append({"id": case_id, "topic": topic, "wrong_answers": wrongs})


def integral_row(topic, integrand, wrong_specs):
    """`wrong_specs` is a list of (kind, rationale, wrong_expr) tuples.

    Every recorded `call` is exactly the text this generator evaluated, and
    `result` is that evaluation's own `sympy.sstr` output (B6.1, finding N1).
    """
    correct = sympy.integrate(integrand, x)
    if correct.has(sympy.Integral):
        raise AssertionError(f"SymPy returned an unevaluated integral for {integrand}")
    residual = sympy.simplify(sympy.diff(correct, x) - integrand)
    if residual != 0:
        raise AssertionError(
            f"SymPy antiderivative of {integrand} does not differentiate back "
            f"(residual {residual})"
        )
    a_text = msp(correct)
    f_text = msp(integrand)
    wrongs = []
    for kind, rationale, wrong_expr in wrong_specs:
        wrong_residual = sympy.simplify(sympy.diff(wrong_expr, x) - integrand)
        if wrong_residual == 0:
            raise AssertionError(f"{kind} mutant of {integrand} is not wrong")
        wrongs.append(
            {
                "error_kind": kind,
                "rationale": f"{kind}: {rationale}",
                "claimed_antiderivative": msp(wrong_expr),
                "sympy_verification": {
                    "verified": False,
                    "call": (
                        f"simplify(diff({sympy_text(wrong_expr)}, x) - "
                        f"({sympy_text(integrand)}))"
                    ),
                    "result": sympy.sstr(wrong_residual),
                },
            }
        )
    _push(
        topic,
        "integral",
        {
            "var": "x",
            "integrand": f_text,
            "claimed_antiderivative": a_text,
            "sympy_verification": {
                "verified": True,
                "call": (
                    f"simplify(diff({sympy_text(correct)}, x) - ({sympy_text(integrand)}))"
                ),
                "result": sympy.sstr(residual),
            },
        },
        wrongs,
    )


def identity_row(topic, lhs, rhs, wrong_specs):
    # Product-to-sum identities are not proved by plain `simplify`; the call
    # recorded below is the one actually evaluated, `expand_trig` included, so
    # re-running the text reproduces the recorded result (B6.1, finding N1).
    residual = sympy.simplify(sympy.expand_trig(lhs - rhs))
    if residual != 0:
        raise AssertionError(f"identity residual {residual} for {lhs} = {rhs}")
    wrongs = []
    for kind, rationale, wrong_rhs in wrong_specs:
        wrong_residual = sympy.simplify(sympy.expand_trig(lhs - wrong_rhs))
        if wrong_residual == 0:
            raise AssertionError(f"{kind} mutant of {lhs} = {rhs} is not wrong")
        wrongs.append(
            {
                "error_kind": kind,
                "rationale": f"{kind}: {rationale}",
                "rhs": msp(wrong_rhs),
                "sympy_verification": {
                    "verified": False,
                    "call": (
                        f"simplify(expand_trig({sympy_text(lhs)} - "
                        f"({sympy_text(wrong_rhs)})))"
                    ),
                    "result": sympy.sstr(wrong_residual),
                },
            }
        )
    _push(
        topic,
        "identity",
        {
            "lhs": msp(lhs),
            "rhs": msp(rhs),
            "vars": ["x"],
            "sympy_verification": {
                "verified": True,
                "call": (
                    f"simplify(expand_trig({sympy_text(lhs)} - ({sympy_text(rhs)})))"
                ),
                "result": sympy.sstr(residual),
            },
        },
        wrongs,
    )


def _root_list(roots):
    return "[" + ", ".join(str(int(root)) for root in roots) + "]"


def equation_row(topic, lhs, rhs, expected_roots, wrong_specs):
    solutions = sympy.solve(sympy.Eq(lhs, rhs), x)
    roots = sorted(int(value) for value in solutions)
    if roots != sorted(int(value) for value in expected_roots):
        raise AssertionError(
            f"SymPy solved {lhs} = {rhs} as {solutions}, expected {expected_roots}"
        )
    for root in roots:
        value = sympy.simplify((lhs - rhs).subs(x, root))
        if value != 0:
            raise AssertionError(f"{root} does not solve {lhs} = {rhs}")
    wrongs = []
    for kind, rationale, wrong_roots in wrong_specs:
        if sorted(wrong_roots) == roots:
            raise AssertionError(f"{kind} mutant root list equals the correct roots")
        if not any(root not in roots for root in wrong_roots):
            raise AssertionError(f"{kind} mutant adds no root that is not a real root")
        for root in wrong_roots:
            # Every claimed root must evaluate to a finite *real* number:
            # otherwise the B5 rejection would be a bare NaN (a 0/0 pole, or
            # sqrt of a negative) rather than the numeric counterexample that
            # `must_reject` in tests/corpus.rs requires.
            value = sympy.simplify((lhs - rhs).subs(x, root))
            if value.has(sympy.zoo, sympy.nan, sympy.oo) or value.is_real is not True:
                raise AssertionError(
                    f"{kind} mutant root {root} is outside the real domain "
                    f"of {lhs} = {rhs}"
                )
        # A wrong root list cannot be recorded as `solve(...)` returning those
        # roots: that call returns the true roots. Record the disproof instead,
        # evaluated at the first claimed root that is not a real root, so the
        # text and the recorded result agree when re-run (B6.1, finding N1).
        bad_root = next(int(root) for root in wrong_roots if root not in roots)
        residual = sympy.simplify((lhs - rhs).subs(x, bad_root))
        if residual == 0:
            raise AssertionError(f"{kind} mutant root {bad_root} solves the equation")
        wrongs.append(
            {
                "error_kind": kind,
                "rationale": f"{kind}: {rationale}",
                "claimed_roots": [float(root) for root in wrong_roots],
                "sympy_verification": {
                    "verified": False,
                    "call": (
                        f"simplify(({sympy_text(lhs)} - ({sympy_text(rhs)})).subs(x, "
                        f"{bad_root}))"
                    ),
                    "result": sympy.sstr(residual),
                },
            }
        )
    _push(
        topic,
        "equation",
        {
            "var": "x",
            "lhs": msp(lhs),
            "rhs": msp(rhs),
            "claimed_roots": [float(root) for root in roots],
            "sympy_verification": {
                "verified": True,
                "call": f"solve(Eq({sympy_text(lhs)}, {sympy_text(rhs)}), x)",
                "result": _root_list(roots),
            },
        },
        wrongs,
    )


def factorisation_row(topic, original, factors, wrong_specs):
    product = sympy.Mul(*factors)
    residual = sympy.simplify(sympy.expand(original - product))
    if residual != 0:
        raise AssertionError(f"factorisation residual {residual}")
    wrongs = []
    for kind, rationale, wrong_factors in wrong_specs:
        wrong_product = sympy.expand(sympy.Mul(*wrong_factors))
        wrong_residual = sympy.simplify(sympy.expand(original - wrong_product))
        if wrong_residual == 0:
            raise AssertionError(f"{kind} mutant factors are not wrong")
        wrongs.append(
            {
                "error_kind": kind,
                "rationale": f"{kind}: {rationale}",
                "claimed_factors": [msp(factor) for factor in wrong_factors],
                "sympy_verification": {
                    "verified": False,
                    "call": (
                        f"simplify(expand({sympy_text(original)} - "
                        f"({')*('.join(sympy_text(f) for f in wrong_factors)})))"
                    ),
                    "result": sympy.sstr(wrong_residual),
                },
            }
        )
    _push(
        topic,
        "factorisation",
        {
            "original": msp(original),
            "claimed_factors": [msp(factor) for factor in factors],
            "sympy_verification": {
                "verified": True,
                "call": (
                    f"simplify(expand({sympy_text(original)} - "
                    f"({')*('.join(sympy_text(f) for f in factors)})))"
                ),
                "result": sympy.sstr(residual),
            },
        },
        wrongs,
    )


# --------------------------------------------------------------- substitution

def gen_substitution():
    topic = "substitution"

    # (a*x + b)^n: the chain factor a is dropped.
    for a in (2, 3, 4, 5):
        for b in (-2, -1, 1, 2, 3):
            for n in (1, 2, 3):
                inner = a * x + b
                integral_row(
                    topic,
                    inner**n,
                    [
                        (
                            KIND_CHAIN,
                            f"integrated ({a}*x {b:+d})^n as (inner)^(n+1)/(n+1), "
                            "omitting the inner derivative",
                            inner ** (n + 1) / (n + 1),
                        )
                    ],
                )

    # x*exp(b*x^2): the chain factor 2*b*x is omitted.
    for b in (1, 2, 3, 4):
        integral_row(
            topic,
            x * sympy.exp(b * x**2),
            [
                (
                    KIND_CHAIN,
                    f"treated exp({b}*x^2) as exp({b}*x), omitting the 2*{b}*x "
                    "chain factor",
                    sympy.exp(b * x**2),
                )
            ],
        )

    # c*exp(a*x): the 1/a factor and the coefficient c are both droppable.
    for a in (2, 3, 5, 7, 25):
        for c in (2, 3, 5):
            integrand = c * sympy.exp(a * x)
            integral_row(
                topic,
                integrand,
                [
                    (
                        KIND_CHAIN,
                        f"wrote {c}*exp({a}*x) for the integral of {c}*exp({a}*x): "
                        "the 1/a from the chain was omitted",
                        integrand,
                    ),
                    (
                        KIND_CONST,
                        f"the factor {c} in front of exp({a}*x) was dropped",
                        sympy.exp(a * x) / a,
                    ),
                ],
            )

    # cos(a*x + b): chain factor a omitted.
    for a in (2, 3, 4, 5):
        for b in (-2, -1, 1, 2):
            integral_row(
                topic,
                sympy.cos(a * x + b),
                [
                    (
                        KIND_CHAIN,
                        "integrated cos(inner) as sin(inner), omitting the inner "
                        "derivative",
                        sympy.sin(a * x + b),
                    )
                ],
            )

    # 1/(a*x + b): chain factor a omitted.
    for a in (2, 3, 5):
        for b in (1, 2, 3):
            integral_row(
                topic,
                1 / (a * x + b),
                [
                    (
                        KIND_CHAIN,
                        "integrated 1/(inner) as ln(inner), omitting the inner "
                        "derivative",
                        sympy.log(a * x + b),
                    )
                ],
            )

    # cos(a*x)*sin(a*x): the 1/a from substituting u = sin(a*x) is omitted.
    for a in (2, 3, 4, 5):
        integral_row(
            topic,
            sympy.cos(a * x) * sympy.sin(a * x),
            [
                (
                    KIND_CHAIN,
                    f"substituted u = sin({a}*x) but used du = cos({a}*x) dx, "
                    f"omitting the factor {a}",
                    sympy.sin(a * x) ** 2 / 2,
                )
            ],
        )

    # c*(x + b)^n: the leading coefficient c is dropped.
    for c in (2, 3, 5):
        for b in (1, 2):
            for n in (2, 3):
                integral_row(
                    topic,
                    c * (x + b) ** n,
                    [
                        (
                            KIND_CONST,
                            f"the factor {c} in front of (x + {b})^{n} was dropped",
                            (x + b) ** (n + 1) / (n + 1),
                        )
                    ],
                )


# --------------------------------------------------------------- by parts

def _parts_sign_error(n, a, dv, v):
    """The classic sign error: uv + integral(v du) instead of uv - integral(v du).

    `correct = u*v - integral(v du)`, so the sign error is `2*u*v - correct`.
    """
    u = x**n
    correct = sympy.integrate(u * dv, x)
    return 2 * u * v - correct


def gen_integration_by_parts():
    topic = "integration_by_parts"
    families = (
        ("exp", lambda a: sympy.exp(a * x), lambda a: sympy.exp(a * x) / a),
        ("sin", lambda a: sympy.sin(a * x), lambda a: -sympy.cos(a * x) / a),
        ("cos", lambda a: sympy.cos(a * x), lambda a: sympy.sin(a * x) / a),
    )
    for name, dv_of, v_of in families:
        for n in (1, 2, 3, 4, 5):
            for a in (1, 2, 3, 4, 5, 6):
                integrand = x**n * dv_of(a)
                wrong = _parts_sign_error(n, a, dv_of(a), v_of(a))
                integral_row(
                    topic,
                    integrand,
                    [
                        (
                            KIND_PARTS_SIGN,
                            f"used uv + integral(v du) instead of uv - integral(v du) "
                            f"for u = x^{n}, dv = {name}({a}*x) dx",
                            wrong,
                        )
                    ],
                )


# --------------------------------------------------------------- logarithms

def gen_logarithms():
    topic = "logarithms"

    for c in range(2, 11):
        integral_row(
            topic,
            c * sympy.log(x),
            [
                (
                    KIND_CONST,
                    f"the factor {c} in front of ln(x) was dropped",
                    x * sympy.log(x) - x,
                )
            ],
        )

    for c in range(2, 11):
        integral_row(
            topic,
            c / x,
            [
                (
                    KIND_CONST,
                    f"the factor {c} in front of 1/x was dropped",
                    sympy.log(x),
                )
            ],
        )

    for a in range(2, 11):
        integral_row(
            topic,
            1 / (a * x),
            [
                (
                    KIND_CHAIN,
                    "integrated 1/(a*x) as ln(a*x), omitting the 1/a chain factor",
                    sympy.log(a * x),
                )
            ],
        )

    for c in (2, 3):
        for n in (1, 2, 3):
            integral_row(
                topic,
                c * x**n * sympy.log(x),
                [
                    (
                        KIND_CONST,
                        f"the factor {c} in front of x^{n}*ln(x) was dropped",
                        sympy.integrate(x**n * sympy.log(x), x),
                    )
                ],
            )

    for a in range(2, 11):
        integral_row(
            topic,
            sympy.log(a * x),
            [
                (
                    KIND_CHAIN,
                    f"integrated ln({a}*x) as x*ln(x) - x, taking the inner "
                    "derivative of ln(a*x) to be 1/x",
                    x * sympy.log(x) - x,
                )
            ],
        )

    for c in range(2, 11):
        integral_row(
            topic,
            c * sympy.log(x) / x,
            [
                (
                    KIND_CONST,
                    f"the factor {c} in front of ln(x)/x was dropped",
                    sympy.log(x) ** 2 / 2,
                )
            ],
        )

    for c in (2, 3):
        integral_row(
            topic,
            c * sympy.log(x) ** 2,
            [
                (
                    KIND_CONST,
                    f"the factor {c} in front of ln(x)^2 was dropped",
                    x * (sympy.log(x) ** 2 - 2 * sympy.log(x) + 2),
                )
            ],
        )

    for a in (2, 3, 4):
        for n in (1, 2, 3):
            integral_row(
                topic,
                x**n * sympy.log(a * x),
                [
                    (
                        KIND_CONST,
                        "the leading coefficient of the parts result was dropped",
                        sympy.integrate(x**n * sympy.log(x), x),
                    )
                ],
            )


# --------------------------------------------------------------- trig identities

def gen_trig_identities():
    topic = "trig_identities"
    freqs = [(a, b) for a in range(1, 7) for b in range(1, 7) if a != b]

    for a, b in freqs:
        lhs = sympy.sin(a * x) * sympy.cos(b * x)
        rhs = (sympy.sin((a + b) * x) + sympy.sin((a - b) * x)) / 2
        wrong_rhs = (sympy.sin((a + b) * x) - sympy.sin((a - b) * x)) / 2
        identity_row(
            topic,
            lhs,
            rhs,
            [
                (
                    KIND_TRIG,
                    "product-to-sum written with a minus where the sum belongs",
                    wrong_rhs,
                )
            ],
        )

    for a, b in freqs:
        lhs = sympy.cos(a * x) * sympy.cos(b * x)
        rhs = (sympy.cos((a - b) * x) + sympy.cos((a + b) * x)) / 2
        wrong_rhs = (sympy.cos((a - b) * x) + sympy.cos((a + b) * x)) / 1
        identity_row(
            topic,
            lhs,
            rhs,
            [
                (
                    KIND_TRIG,
                    "the factor 1/2 in the product-to-sum identity was dropped",
                    wrong_rhs,
                )
            ],
        )

    for a, b in freqs:
        lhs = sympy.sin(a * x) * sympy.sin(b * x)
        rhs = (sympy.cos((a - b) * x) - sympy.cos((a + b) * x)) / 2
        wrong_rhs = (sympy.cos((a - b) * x) + sympy.cos((a + b) * x)) / 2
        identity_row(
            topic,
            lhs,
            rhs,
            [
                (
                    KIND_TRIG,
                    "product-to-sum written with a plus where the difference "
                    "belongs",
                    wrong_rhs,
                )
            ],
        )


# --------------------------------------------------------------- equations

ROOT_POOL = (-5, -4, -3, -2, 2, 3, 4, 5)


def _triples():
    return [
        (p, q, r)
        for p in ROOT_POOL
        for q in ROOT_POOL
        for r in ROOT_POOL
        if len({p, q, r}) == 3
    ]


def _wrong_sign_roots(roots):
    """Flip the sign of the first root whose negation is not also a root."""
    for root in roots:
        if -root not in roots:
            return sorted([-root] + [r for r in roots if r != root])
    raise AssertionError("no sign-flippable root")


def _wrong_power_roots(roots):
    """Claim root^2 where root is a root: the exponent slip."""
    for root in roots:
        squared = root * root
        if squared not in roots:
            return sorted([squared] + [r for r in roots if r != root])
    raise AssertionError("no exponent-slippable root")


def _mutants_are_usable(roots, pole=None):
    """True when both root mutants are distinct, finite and genuinely new."""
    try:
        mutants = [_wrong_sign_roots(list(roots)), _wrong_power_roots(list(roots))]
    except AssertionError:
        return False
    for mutant in mutants:
        if len(set(mutant)) != len(mutant):
            return False
        if sorted(mutant) == sorted(roots):
            return False
        if not any(root not in roots for root in mutant):
            return False
        if pole is not None and pole in mutant:
            return False
    return True


def gen_polynomial_equations():
    topic = "polynomial_equations"
    usable = [triple for triple in _triples() if _mutants_are_usable(triple)]
    if len(usable) < 120:
        raise AssertionError(f"only {len(usable)} usable polynomial triples")

    for p, q, r in usable[:60]:
        root_list = [p, q, r]
        lhs = sympy.expand((x - p) * (x - q) * (x - r))
        equation_row(
            topic,
            lhs,
            0,
            root_list,
            [
                (
                    KIND_ROOT_SIGN,
                    f"one root of (x - {p})(x - {q})(x - {r}) = 0 was written with "
                    "the wrong sign",
                    _wrong_sign_roots(root_list),
                ),
                (
                    KIND_ROOT_POWER,
                    "a root was written as its square (the exponent of the factor "
                    "was applied to the root)",
                    _wrong_power_roots(root_list),
                ),
            ],
        )

    for p, q, r in usable[60:120]:
        original = sympy.expand((x - p) * (x - q) * (x - r))
        factors = [x - p, x - q, x - r]
        factorisation_row(
            topic,
            original,
            factors,
            [
                (
                    KIND_FACTOR,
                    f"(x - {q}) was written as (x + {q})",
                    [x - p, x + q, x - r],
                ),
                (
                    KIND_FACTOR,
                    f"(x - {r}) was written as (x + {r})",
                    [x - p, x - q, x + r],
                ),
            ],
        )

    # Radical equations: solving by squaring introduces a root the original
    # equation does not have.
    for a, r in ((6, 3), (12, 4), (20, 5), (30, 6), (42, 7)):
        equation_row(
            topic,
            sympy.sqrt(x + a),
            x,
            [r],
            [
                (
                    KIND_EXTRANEOUS,
                    "squaring sqrt(x + a) = x introduced the negative root "
                    f"{1 - r}, which the original equation does not satisfy",
                    [r, 1 - r],
                ),
            ],
        )

    # sqrt(x + a) = x - 1 with a >= r - 1 keeps the extraneous root inside the
    # real domain of the left-hand side; a = 1 would put sqrt(x + 1) at x = -2,
    # which is a domain error inside mathd, not a numeric counterexample.
    for a, r in ((5, 4), (11, 5), (19, 6), (29, 7)):
        equation_row(
            topic,
            sympy.sqrt(x + a),
            x - 1,
            [r],
            [
                (
                    KIND_EXTRANEOUS,
                    "squaring sqrt(x + a) = x - 1 introduced a root where the "
                    "right-hand side is negative",
                    [r, 1 - r],
                ),
            ],
        )


def gen_rational_equations():
    topic = "rational_equations"
    combos = []
    for p in ROOT_POOL:
        for q in ROOT_POOL:
            if q == p:
                continue
            for pole in ROOT_POOL:
                if pole in (p, q):
                    continue
                # A mutant root that equals the pole would be a 0/0 point in
                # mathd: rejected, but with NaN rather than a numeric
                # counterexample, so such a pair is not a usable problem.
                if _mutants_are_usable([p, q], pole=pole):
                    combos.append((p, q, pole))

    for p, q, pole in combos[:60]:
        lhs = (x - p) * (x - q) / (x - pole)
        equation_row(
            topic,
            lhs,
            0,
            [p, q],
            [
                (
                    KIND_ROOT_SIGN,
                    f"the root {q} was written with the wrong sign",
                    _wrong_sign_roots([p, q]),
                ),
                (
                    KIND_ROOT_POWER,
                    "a root was written as its square (the exponent of the "
                    "numerator factor was applied to the root)",
                    _wrong_power_roots([p, q]),
                ),
            ],
        )


# --------------------------------------------------------------- domain claims

def _parse_claim(text):
    """Parse a claim written in mathd syntax into a SymPy expression."""
    back = text.replace("^", "**").replace("ln(", "log(").replace("abs(", "Abs(")
    return sympy.sympify(back, locals={"x": x})


# ---- V8 strict domain rule (owner decision, decisions.md 2026-10-30) -----
# A claim is false if, at a point where one side is a real value, the other
# side is not; it is true only where both sides are undefined together (the
# point is skipped) or both are defined and equal.  These helpers answer over
# the reals, node by node, because plain SymPy answers over the complexes --
# `log(-3) - log(-3)` collapses to 0 there.  The domain rules applied below
# are mathd's published contract (mathd/src/evaluator.rs module doc,
# "Contract" and "Power conventions"): ln of a non-positive, division by an
# exactly zero denominator, sqrt of a negative, a negative base with a
# fractional exponent and 0^-n are domain errors; an unbound name and a
# non-finite value are errors; 0^0 = 1.  `mathd/fixtures/generate.py` carries
# the same walk for the B5 fixture; the two generators stay independent tools.
#
# Candidate points are exact dyadic rationals so `simplify` is exact (a float
# candidate would turn an equal pair into a 1e-16 "difference"), and their
# float values are exact too, so the point recorded in a call string is the
# point the classification saw.
STRICT_POINTS = [
    sympy.Rational(-3), sympy.Rational(-5, 2), sympy.Rational(-2),
    sympy.Rational(-3, 2), sympy.Rational(-1), sympy.Rational(-1, 2),
    sympy.Rational(-1, 4), sympy.Rational(1, 4), sympy.Rational(1, 2),
    sympy.Rational(3, 2), sympy.Rational(2), sympy.Rational(5, 2),
    sympy.Rational(3),
]
STRICT_NEIGHBOUR = sympy.Rational(1, 20)

# The bindings replay_calls.py provides when it re-runs a recorded call.
# `_domain_row` evaluates the exact string it records, and replay_calls.py
# (which shares no code with this generator) re-derives the result itself.
REPLAY_ENV = {
    "x": x,
    "simplify": sympy.simplify,
    "ln": sympy.log,
    "abs": sympy.Abs,
    "sqrt": sympy.sqrt,
}


def _real_value(expr, subs_map):
    """Evaluate `expr` under REAL semantics: (defined, value).

    Mirrors the walk in mathd/fixtures/generate.py (same spec citation above).
    Definedness is decided node by node, bottom-up, and whole values are only
    substituted once every node under them is real -- so an undefined subterm
    cannot be cancelled away before anyone notices it.
    """
    if isinstance(expr, sympy.Symbol):
        if expr in subs_map:
            value = sympy.sympify(subs_map[expr])
            ok = value.is_real is True and value.is_finite is True
            return ok, (value if ok else None)
        return False, None  # unbound name: mathd returns Undefined
    if isinstance(expr, sympy.Integer) or isinstance(expr, sympy.Rational):
        return True, expr
    if isinstance(expr, sympy.NumberSymbol):  # e (Euler), pi: real and finite
        ok = expr.is_real is True and expr.is_finite is True
        return ok, (expr if ok else None)
    if isinstance(expr, sympy.Number):
        ok = expr.is_real is True and expr.is_finite is True
        return ok, (expr if ok else None)
    if expr.func is sympy.log:
        ok, arg = _real_value(expr.args[0], subs_map)
        if not ok or not (arg > 0):
            return False, None  # ln of a non-positive -> Domain
        value = expr.subs(subs_map)
        good = value.is_real is True and value.is_finite is True
        return good, (value if good else None)
    if isinstance(expr, sympy.Pow):
        ok_b, base = _real_value(expr.base, subs_map)
        ok_e, expo = _real_value(expr.exp, subs_map)
        if not ok_b or not ok_e:
            return False, None
        if expo.is_Integer:
            if expo < 0 and base == 0:
                return False, None  # 0^-n -> division by zero
        else:
            if base < 0:
                return False, None  # negative base, fractional exponent
        value = expr.subs(subs_map)
        good = value.is_real is True and value.is_finite is True
        return good, (value if good else None)
    if expr.args:
        for arg in expr.args:
            ok, _ = _real_value(arg, subs_map)
            if not ok:
                return False, None
        value = expr.subs(subs_map)
        good = value.is_real is True and value.is_finite is True
        return good, (value if good else None)
    return False, None  # unknown node type: refuse rather than guess


def _strict_kind(lhs, rhs, subs_map):
    """(kind, undefined) at one point: 'one-sided' / 'values-differ' / None.

    None means both sides undefined (skipped under the rule) or both real and
    equal; `undefined` is 'lhs'/'rhs' for a one-sided point, else None.
    """
    lok, lval = _real_value(lhs, subs_map)
    rok, rval = _real_value(rhs, subs_map)
    if lok and not rok:
        return "one-sided", "rhs"
    if rok and not lok:
        return "one-sided", "lhs"
    if lok and rok and sympy.simplify(lval - rval) != 0:
        return "values-differ", None
    return None, None


def _domain_counterexample(lhs, rhs):
    """First strict-rule counterexample on the x grid that also holds at both
    neighbours (x +/- 1/20): a mismatch on an interval, which is what float
    sample points reach.    Returns (kind, undefined, exact Rational point) or None.
    """
    for point in STRICT_POINTS:
        kind, undefined = _strict_kind(lhs, rhs, {x: point})
        if kind is None:
            continue
        if all(
            _strict_kind(lhs, rhs, {x: point + delta}) == (kind, undefined)
            for delta in (-STRICT_NEIGHBOUR, STRICT_NEIGHBOUR)
        ):
            return kind, undefined, point
    return None


def _point_literal(point):
    """A Python literal that evaluates to exactly this dyadic rational."""
    if point.is_Integer:
        return str(int(point))
    return str(float(point))


def _both_undefined(lhs, rhs, point):
    lok, _ = _real_value(lhs, {x: point})
    rok, _ = _real_value(rhs, {x: point})
    return not lok and not rok


def _domain_row(lhs_text, rhs_text, note):
    """Emit one F3 claim with its V8 strict-rule verdict.

    SymPy auto-simplifies `sqrt(x^2)` to `Abs(x)`, so the claim text is kept
    literal: printing the SymPy object would silently replace the very claim
    the lead asked for (and make `note` describe a different expression).
    The recorded oracle call is over the same text the fixture states.

    A row whose strict-rule counterexample the search finds is `expect:
    reject` and records that real counterexample point -- one side real and
    the other not, or both real with different values -- in a call that
    replay_calls.py re-runs literally.  A row with no counterexample on the
    grid is `expect: verify` and records the two facts that make it true:
    both sides undefined at the same point, and symbolic equality where both
    are defined (a positive-assumed symbol).
    """
    lhs = _parse_claim(lhs_text)
    rhs = _parse_claim(rhs_text)
    lhs_sym = lhs_text.replace("^", "**")
    rhs_sym = rhs_text.replace("^", "**")
    ident = f"domain-{len(domain_rows) + 1:03d}"

    hit = _domain_counterexample(lhs, rhs)
    if hit is None:
        # Control row: no point on the grid is one-sided or unequal.
        neg_point = next(
            (p for p in STRICT_POINTS if _both_undefined(lhs, rhs, p)), None
        )
        if neg_point is None:
            raise AssertionError(
                f"control claim {lhs_text} = {rhs_text}: "
                "no point where both sides are undefined"
            )
        neg_lit = _point_literal(neg_point)
        call = (
            f"(({lhs_sym}).subs(x, {neg_lit}).is_real, "
            f"({rhs_sym}).subs(x, {neg_lit}).is_real, "
            f"simplify(({lhs_sym} - ({rhs_sym})).subs(x, "
            f"x.func('p', positive=True))))"
        )
        result = sympy.sstr(eval(call, {"__builtins__": {}}, REPLAY_ENV))  # noqa: S307
        if result != "(False, False, 0)":
            raise AssertionError(
                f"control claim {ident} {lhs_text} = {rhs_text}: expected the "
                f"recorded facts (False, False, 0), got {result}"
            )
        row = {
            "id": ident,
            "topic": "domain",
            "lhs": lhs_text,
            "rhs": rhs_text,
            "expect": "verify",
            "sympy_verification": {"verified": True, "call": call, "result": result},
            "note": note,
        }
    else:
        kind, undefined, point = hit
        point_lit = _point_literal(point)
        if kind == "one-sided":
            call = (
                f"({lhs_sym}).subs(x, {point_lit}).is_real, "
                f"({rhs_sym}).subs(x, {point_lit}).is_real"
            )
        else:
            call = (
                f"simplify(({lhs_sym} - ({rhs_sym})).subs(x, {point_lit}))"
            )
        result = sympy.sstr(eval(call, {"__builtins__": {}}, REPLAY_ENV))  # noqa: S307
        if kind == "one-sided":
            expected_pair = (
                "(True, False)" if undefined == "rhs" else "(False, True)"
            )
            if result != expected_pair:
                raise AssertionError(
                    f"{ident}: one-sided ({undefined} undefined) counterexample "
                    f"at x = {point_lit} recorded {result}, expected {expected_pair}"
                )
        elif sympy.sympify(result) == 0:
            raise AssertionError(
                f"{ident}: values-differ counterexample at x = {point_lit} "
                f"recorded a zero residual ({result})"
            )
        counterexample = {"point": {"x": float(point)}, "kind": kind}
        if kind == "one-sided":
            counterexample["undefined"] = undefined
        row = {
            "id": ident,
            "topic": "domain",
            "lhs": lhs_text,
            "rhs": rhs_text,
            "expect": "reject",
            "counterexample": counterexample,
            "sympy_verification": {
                "verified": False,
                "call": call,
                "result": result,
            },
            "note": note,
        }
    domain_rows.append(row)


def gen_domain_claims():
    claims = [
        ("sqrt(x^2)", "x", "sqrt(x^2) = |x|, not x: false for every x < 0"),
        ("abs(x)", "x", "abs(x) = x only for x >= 0"),
        ("sqrt(x^2)/x", "1", "sqrt(x^2)/x is -1 for x < 0"),
        ("abs(x)/x", "1", "abs(x)/x is -1 for x < 0"),
        ("2*sqrt(x^2)", "2*x", "2*sqrt(x^2) = 2*|x|, not 2*x"),
        ("sqrt(4*x^2)", "2*x", "sqrt(4*x^2) = 2*|x|, not 2*x"),
        ("sqrt(x^2) + 1", "x + 1", "sqrt(x^2) + 1 = |x| + 1, not x + 1"),
        ("sqrt(9*x^2)", "3*x", "sqrt(9*x^2) = 3*|x|, not 3*x"),
        ("sqrt(x^2)*x", "x^2", "sqrt(x^2)*x = |x|*x, which is -x^2 for x < 0"),
        ("abs(3*x)", "3*x", "abs(3*x) = 3*|x|, not 3*x"),
        ("sqrt(x^2) - x", "0", "sqrt(x^2) - x = |x| - x != 0 for x < 0"),
        ("abs(2*x)/x", "2", "abs(2*x)/x is -2 for x < 0"),
        ("sqrt(16*x^2)", "4*x", "sqrt(16*x^2) = 4*|x|, not 4*x"),
        ("ln(x^2)", "2*ln(x)", "ln(x^2) = 2*ln(x) requires x > 0"),
        ("ln(x^3)", "3*ln(x)", "true over the reals: both sides are undefined for x <= 0 and equal for x > 0 (the control row: the strict rule must not reject it)"),
        ("ln(sqrt(x^2))", "ln(x)", "ln(sqrt(x^2)) = ln(x) requires x > 0"),
        ("ln(x^4)", "4*ln(x)", "ln(x^4) = 4*ln(x) requires x > 0"),
    ]
    for lhs_text, rhs_text, note in claims:
        _domain_row(lhs_text, rhs_text, note)


# --------------------------------------------------------------- main

def main():
    gen_substitution()
    gen_integration_by_parts()
    gen_logarithms()
    gen_trig_identities()
    gen_polynomial_equations()
    gen_rational_equations()
    gen_domain_claims()

    if len(gt_rows) < 500:
        raise AssertionError(f"only {len(gt_rows)} problems generated, need 500")
    for row, wrong_row in zip(gt_rows, wrong_rows):
        if not wrong_row["wrong_answers"]:
            raise AssertionError(f"{row['id']} has no wrong answer")

    def dump(path, rows):
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    dump(GT_PATH, gt_rows)
    dump(WRONG_PATH, wrong_rows)
    dump(DOMAIN_PATH, domain_rows)

    topics = {}
    for row in gt_rows:
        topics[row["topic"]] = topics.get(row["topic"], 0) + 1
    kinds = {}
    for wrong_row in wrong_rows:
        for wrong in wrong_row["wrong_answers"]:
            kinds[wrong["error_kind"]] = kinds.get(wrong["error_kind"], 0) + 1

    print(f"sympy {sympy.__version__}")
    print(f"problems: {len(gt_rows)}")
    print("per topic:")
    for topic in sorted(topics):
        print(f"  {topic}: {topics[topic]}")
    for topic, reason in sorted(TOPICS_NOT_EXPRESSIBLE.items()):
        print(f"  {topic}: 0 (not expressible: {reason})")
    print("wrong answers per kind:")
    for kind in sorted(kinds):
        print(f"  {kind}: {kinds[kind]}")
    for kind, reason in sorted(NOT_EXPRESSIBLE.items()):
        print(f"  {kind}: 0 (not expressible: {reason})")
    print(f"wrong answers total: {sum(kinds.values())}")
    print(f"domain claims: {len(domain_rows)} "
          f"({sum(1 for row in domain_rows if row['expect'] == 'reject')} rejected "
          f"with a counterexample point, "
          f"{sum(1 for row in domain_rows if row['expect'] == 'verify')} control "
          f"verified)")
    for row in domain_rows:
        if row["expect"] == "verify":
            print(f"  control row: {row['id']} {row['lhs']} = {row['rhs']}")


if __name__ == "__main__":
    main()
