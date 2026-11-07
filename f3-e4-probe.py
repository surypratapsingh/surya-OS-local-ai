import tempfile, sys
from pathlib import Path
sys.path.insert(0, "novacore/src")
from novacore.casememory import CaseMemory, CaseVerificationError

td = Path(tempfile.mkdtemp())
store = CaseMemory(td / "cases.jsonl")
ev = {"verdict": "Failed", "points_tested": 16, "max_relative_error": 0.0,
      "counterexample": {"x": 2.0}}
try:
    case = store.add(question="Solve x^2 - 4 = 0", method="factor", answer="x = 2",
                     domain="maths", evidence=ev)
    print("STORED a Failed verdict with complete parameters:", case.case_id)
    print("records now:", len(store.all()))
except CaseVerificationError as e:
    print("REFUSED:", e)
