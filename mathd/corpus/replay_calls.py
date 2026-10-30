#!/usr/bin/env python3
"""Re-run every recorded SymPy call in the corpus, literally.

B6.1 (`docs/reviews/b6-fix-1030-claude.log` finding N1) found that 110 of the
422 recorded `simplify()` strings did not reproduce their recorded `result`:
the operand after the minus sign was not bracketed, so `a - b + c` was parsed
as `(a - b) + c`.  A recorded oracle call that answers a different question is
not evidence.

This checker shares no code with `generate_corpus.py` except the three fixture
files.  It reads each `sympy_verification` block, evaluates the recorded `call`
text exactly as written with SymPy, and compares `sympy.sstr(value)` to the
recorded `result` by string equality.  Anything else is a difference.

Run:  python mathd/corpus/replay_calls.py
Exit: 0 when every call reproduces, 1 otherwise.
"""

import json
import sys
from pathlib import Path

import sympy
from sympy import (
    Abs,
    Eq,
    cos,
    diff,
    exp,
    expand,
    expand_trig,
    log,
    pi,
    simplify,
    sin,
    solve,
    sqrt,
    tan,
)

HERE = Path(__file__).resolve().parent
FILES = ("ground_truth.jsonl", "wrong_answers.jsonl", "domain_claims.jsonl")

# `sympy_text()` in the generator prints `ln` and `abs` in mathd syntax; the
# recorded call therefore uses those two names, with `**` for powers.
ENV = {
    "x": sympy.Symbol("x", real=True),
    "simplify": simplify,
    "diff": diff,
    "solve": solve,
    "Eq": Eq,
    "expand": expand,
    "expand_trig": expand_trig,
    "ln": log,
    "abs": Abs,
    "sqrt": sqrt,
    "sin": sin,
    "cos": cos,
    "tan": tan,
    "exp": exp,
    "pi": pi,
}


def proofs():
    """Yield (file, row id, call, recorded result) for every recorded call."""
    for name in FILES:
        path = HERE / name
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                row = json.loads(line)
                if "sympy_verification" in row:
                    # ground_truth and domain_claims
                    blocks = [(row["id"], row["sympy_verification"])]
                else:
                    # wrong_answers wraps the blocks in a list
                    blocks = [
                        (row["id"], answer["sympy_verification"])
                        for answer in row["wrong_answers"]
                    ]
                for row_id, block in blocks:
                    if "call" not in block or "result" not in block:
                        raise SystemExit(
                            f"{name}:{line_number} {row_id}: incomplete proof block"
                        )
                    yield name, row_id, block["call"], block["result"]


def main():
    total = 0
    differed = 0
    for name, row_id, call, recorded in proofs():
        total += 1
        try:
            value = eval(call, {"__builtins__": {}}, ENV)  # noqa: S307 - committed fixture
            got = sympy.sstr(value)
        except Exception as error:  # noqa: BLE001 - report, do not hide
            differed += 1
            print(f"ERROR {name} {row_id}: {call}\n  {error}")
            continue
        if got != recorded:
            differed += 1
            print(f"DIFF  {name} {row_id}")
            print(f"  call     {call}")
            print(f"  recorded {recorded}")
            print(f"  re-run   {got}")
    print(f"replayed {total} recorded calls; {differed} of {total} differ")
    return 1 if differed else 0


if __name__ == "__main__":
    sys.exit(main())
