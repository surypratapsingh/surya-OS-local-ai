"""NOVA Study Session (work order A3.9, milestone M2).

Runs interactive or scripted quizzes from question files. Each answer is checked
by calling `mathd-cli` through a subprocess.

Project invariants:
- If `mathd-cli` is missing, appends a demand entry via `DemandStore` (`demand.py`)
  and NEVER guesses a verdict ("Nothing unverified becomes permanent").
- Tests use the real `mathd-cli` binary when it is built, and SKIP loudly when it is not.
- Answers stay strictly in RAM or in the owner's authenticated memory store (`CaseMemory`),
  following the W5 rules (no intermediate unencrypted answer files written to disk).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Sequence

from .casememory import CaseMemory
from .demand import DemandStore


@dataclass(frozen=True)
class Question:
    """A quiz question with an algebraic/mathematical claim template."""
    id: str
    prompt: str
    claim_template: str
    category: str = "maths"
    metadata: dict[str, Any] | None = None

    def format_claim(self, answer: str) -> str:
        """Format the full claim string by substituting the student's answer."""
        ans = answer.strip()
        if "{answer}" in self.claim_template:
            return self.claim_template.format(answer=ans)
        # If no placeholder, format as equivalence if appropriate
        return f"{self.claim_template} == {ans}"


@dataclass(frozen=True)
class EvaluationResult:
    """Result of evaluating a student answer against mathd-cli or logging a demand."""
    question_id: str
    prompt: str
    student_answer: str
    claim_evaluated: str
    verdict: str  # "Verified", "Failed", "Unverifiable", "DemandLogged"
    evidence: dict[str, Any] | None
    raw_output: str
    demand_entry_id: str | None = None


def find_mathd_cli(override: Path | str | None = None) -> Path | None:
    """Locate the mathd-cli executable on the host.

    Returns the Path to the executable if present and runnable, else None.
    """
    if override is not None:
        p = Path(override).resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        if sys.platform == "win32" and not p.suffix:
            p_exe = p.with_suffix(".exe")
            if p_exe.is_file():
                return p_exe
        return None

    # Check PATH
    which_bin = shutil.which("mathd-cli")
    if which_bin:
        return Path(which_bin).resolve()

    # Search common cargo build output paths
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    candidates = [
        repo_root / "mathd" / "target" / "release" / "mathd-cli.exe",
        repo_root / "mathd" / "target" / "release" / "mathd-cli",
        repo_root / "mathd" / "target" / "debug" / "mathd-cli.exe",
        repo_root / "mathd" / "target" / "debug" / "mathd-cli",
        repo_root / "target" / "release" / "mathd-cli.exe",
        repo_root / "target" / "release" / "mathd-cli",
        repo_root / "target" / "debug" / "mathd-cli.exe",
        repo_root / "target" / "debug" / "mathd-cli",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()

    return None


def load_questions(question_file: Path | str) -> list[Question]:
    """Parse quiz questions from a JSON or JSONL file."""
    path = Path(question_file).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Question file not found: {path}")

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    questions: list[Question] = []
    # Try parsing as JSON array
    if content.startswith("["):
        try:
            data = json.loads(content)
            for item in data:
                questions.append(Question(
                    id=str(item["id"]),
                    prompt=str(item["prompt"]),
                    claim_template=str(item["claim_template"]),
                    category=str(item.get("category", "maths")),
                    metadata=item.get("metadata"),
                ))
            return questions
        except (json.JSONDecodeError, KeyError, TypeError) as err:
            raise ValueError(f"Invalid JSON question array in {path}: {err}") from err

    # Otherwise parse as JSONL
    for line_no, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            questions.append(Question(
                id=str(item["id"]),
                prompt=str(item["prompt"]),
                claim_template=str(item["claim_template"]),
                category=str(item.get("category", "maths")),
                metadata=item.get("metadata"),
            ))
        except (json.JSONDecodeError, KeyError, TypeError) as err:
            raise ValueError(f"Invalid JSONL question at {path}:{line_no}: {err}") from err

    return questions


class StudySession:
    """Runs a study quiz session, checking answers with mathd-cli."""

    def __init__(
        self,
        question_source: Sequence[Question] | Path | str,
        mathd_cli: Path | str | None = None,
        demand_store: DemandStore | Path | None = None,
        case_memory: CaseMemory | None = None,
    ) -> None:
        if isinstance(question_source, (str, Path)):
            self.questions = load_questions(question_source)
        else:
            self.questions = list(question_source)

        self._questions_by_id = {q.id: q for q in self.questions}

        # mathd-cli location
        self.mathd_cli_path = find_mathd_cli(mathd_cli)

        # Demand store setup
        if demand_store is None:
            # Default to a persistent demand store
            repo_root = Path(__file__).resolve().parent.parent.parent.parent
            default_path = repo_root / "novacore" / "data" / "demands.jsonl"
            self.demand_store = DemandStore(default_path)
        elif isinstance(demand_store, Path):
            self.demand_store = DemandStore(demand_store)
        elif isinstance(demand_store, DemandStore):
            self.demand_store = demand_store
        else:
            raise TypeError(f"demand_store must be DemandStore or Path, got {type(demand_store)}")

        self.case_memory = case_memory

        # In-memory storage of answers and results (W5 rules: RAM only)
        self.results: list[EvaluationResult] = []

    def submit_answer(self, question_id: str, student_answer: str) -> EvaluationResult:
        """Submit a single answer and evaluate it against mathd-cli.

        If mathd-cli is missing, logs a demand entry and NEVER guesses a verdict.
        Answers and results stay in RAM (or case_memory if provided).
        """
        question = self._questions_by_id.get(question_id)
        if question is None:
            raise KeyError(f"Question ID '{question_id}' not found in active session")

        claim = question.format_claim(student_answer)

        # 1. Missing mathd-cli: Log demand and refuse to guess
        if self.mathd_cli_path is None:
            demand = self.demand_store.add(
                title=f"mathd-cli required for study session verification (question {question_id})",
                description=(
                    f"NOVA Study session (milestone M2) requires mathd-cli to verify answer "
                    f"for question {question_id} ('{question.prompt}'). "
                    f"Per project invariant 'Nothing unverified becomes permanent', no verdict was guessed."
                ),
                urls=[
                    "https://github.com/surypratapsingh/surya-OS-local-ai",
                ],
                instructions=[
                    "Build the mathd-cli verification binary on a host with Rust: 'cargo build --release --bin mathd-cli'",
                    "Transport the compiled mathd-cli binary via offline pendrive shuttle package",
                    "Place mathd-cli into PATH or mathd/target/release/mathd-cli",
                ],
                item_type="binary",
                target_filename="mathd-cli",
            )
            eval_res = EvaluationResult(
                question_id=question_id,
                prompt=question.prompt,
                student_answer=student_answer,
                claim_evaluated=claim,
                verdict="DemandLogged",
                evidence=None,
                raw_output="mathd-cli executable missing; demand logged, verdict refused to guess.",
                demand_entry_id=demand.entry_id,
            )
            self.results.append(eval_res)
            return eval_res

        # 2. Run mathd-cli subprocess
        try:
            proc = subprocess.run(
                [str(self.mathd_cli_path), "--claim", claim],
                capture_output=True,
                text=True,
                timeout=30.0,
            )
            output = proc.stdout + "\n" + proc.stderr
        except subprocess.TimeoutExpired:
            eval_res = EvaluationResult(
                question_id=question_id,
                prompt=question.prompt,
                student_answer=student_answer,
                claim_evaluated=claim,
                verdict="Unverifiable",
                evidence=None,
                raw_output="mathd-cli verification timed out after 30s",
            )
            self.results.append(eval_res)
            return eval_res

        verdict_str = "Unverifiable"
        evidence_dict: dict[str, Any] | None = None

        if "Verdict: Verified" in output:
            # Extract points_tested and max_relative_error
            pts_m = re.search(r"points_tested:\s*(\d+)", output)
            err_m = re.search(r"max_relative_error:\s*([0-9.eE+-]+)", output)
            if pts_m is not None and err_m is not None:
                verdict_str = "Verified"
                evidence_dict = {
                    "verdict": "Verified",
                    "points_tested": int(pts_m.group(1)),
                    "max_relative_error": float(err_m.group(1)),
                }
            else:
                # Per F3.14-F4: Never invent fallback 16 / 0.0; mark Unverifiable
                verdict_str = "Unverifiable"
                evidence_dict = {
                    "verdict": "Unverifiable",
                    "reason": "Missing points_tested or max_relative_error in verification evidence",
                    "raw_output": output.strip(),
                }
            # Optional: store in owner's case memory if configured
            if self.case_memory is not None:
                try:
                    self.case_memory.add(
                        question=question.prompt,
                        method="mathd-cli study verification",
                        answer=student_answer,
                        domain="maths",
                        evidence=evidence_dict,
                    )
                except Exception:
                    pass

        elif "Verdict: Failed" in output:
            verdict_str = "Failed"
            evidence_dict = {
                "verdict": "Failed",
                "raw_output": output.strip(),
            }
        elif "Verdict: Unverifiable" in output:
            verdict_str = "Unverifiable"
            evidence_dict = {
                "verdict": "Unverifiable",
                "raw_output": output.strip(),
            }

        eval_res = EvaluationResult(
            question_id=question_id,
            prompt=question.prompt,
            student_answer=student_answer,
            claim_evaluated=claim,
            verdict=verdict_str,
            evidence=evidence_dict,
            raw_output=output.strip(),
        )
        self.results.append(eval_res)
        return eval_res

    def run_all(self, answers: dict[str, str]) -> list[EvaluationResult]:
        """Submit and evaluate answers for all matching question IDs."""
        results: list[EvaluationResult] = []
        for q in self.questions:
            if q.id in answers:
                results.append(self.submit_answer(q.id, answers[q.id]))
        return results

    def get_summary(self) -> dict[str, Any]:
        """Compute summary statistics for evaluated answers in RAM."""
        verified = sum(1 for r in self.results if r.verdict == "Verified")
        failed = sum(1 for r in self.results if r.verdict == "Failed")
        demand_logged = sum(1 for r in self.results if r.verdict == "DemandLogged")
        unverifiable = sum(1 for r in self.results if r.verdict == "Unverifiable")

        return {
            "total_questions": len(self.questions),
            "total_evaluated": len(self.results),
            "verified": verified,
            "failed": failed,
            "demand_logged": demand_logged,
            "unverifiable": unverifiable,
        }
