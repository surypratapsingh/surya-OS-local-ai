"""Solved-problem replay (semantic cache) with tamper-evident chainlog and verified evidence gating.

E4 case memory: When a problem is solved — a maths technique, a config, a how-to —
the question, method, and answer are stored. Future similar problems can replay
the stored solution instead of re-deriving it.

Tamper-evidence:
Each record is hash-chained using chainlog.py and anchored with an anchor file.
Any modification, reordering, deletion, or truncation is detected.

Verification gating:
Project invariant: Nothing unverified becomes permanent.
A maths answer can be stored only with 'Verified' evidence from mathd. Any unverified
verdict (Failed, Unverifiable, FailedDomain, missing evidence, or invalid parameters)
is strictly refused by raising CaseVerificationError.

Hashed word vectors:
Pure stdlib-only feature hashing using SHA-256 for deterministic indexing across
restarts and environments, followed by L2 normalization and cosine similarity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Iterator, Sequence
from uuid import uuid4

from . import chainlog


class CaseVerificationError(ValueError):
    """Raised when an answer lacks required verification evidence."""


class CaseTamperError(ValueError):
    """Raised when the case log fails hash-chain or anchor integrity checks."""


_WORD_RE = re.compile(r"\b[\w']+\b", re.UNICODE)

_MATH_PATTERNS = re.compile(
    r"(?i)\b(integral|integrate|derivative|differentiate|differential|polynomial|"
    r"quadratic|factorize|factorise|factorisation|factorization|equation|eigenvalue|"
    r"matrix|trigonometric|calculus|algebra|logarithm|fourier|laplace|taylor|series|"
    r"limit|antiderivative|sin|cos|tan|cot|sec|csc)\b|"
    r"(\\int|\\frac|\\partial|\\sum|\\prod|\\sqrt|\^|\b[xyz]\s*\^|\b[xyz]\s*[*+/-])"
)


def is_math_query(question: str, method: str = "") -> bool:
    """Return True if question or method describes a mathematical problem."""
    return bool(_MATH_PATTERNS.search(question) or _MATH_PATTERNS.search(method))


def validate_math_evidence(evidence: dict | None) -> dict:
    """Validate that evidence proves mathd verification, or raise CaseVerificationError."""
    if evidence is None:
        raise CaseVerificationError("maths answer requires Verified evidence from mathd")
    if not isinstance(evidence, dict):
        raise CaseVerificationError(f"maths evidence must be a dict, got {type(evidence).__name__}")

    verdict = evidence.get("verdict")
    if verdict != "Verified":
        raise CaseVerificationError(
            f"maths answer rejected: verdict is '{verdict}', required 'Verified'"
        )

    points_tested = evidence.get("points_tested")
    if not isinstance(points_tested, int) or points_tested < 1:
        raise CaseVerificationError(
            f"maths evidence points_tested must be integer >= 1, got {points_tested}"
        )

    max_relative_error = evidence.get("max_relative_error")
    if (
        max_relative_error is None
        or not isinstance(max_relative_error, (int, float))
        or not math.isfinite(max_relative_error)
        or max_relative_error < 0.0
    ):
        raise CaseVerificationError(
            f"maths evidence max_relative_error must be finite non-negative float, got {max_relative_error}"
        )

    return evidence


def compute_word_vector(text: str, dim: int = 512) -> list[float]:
    """Compute an L2-normalized hashed word vector using SHA-256 (stdlib-only)."""
    words = [m.group(0).casefold() for m in _WORD_RE.finditer(text)]
    if not words:
        return [0.0] * dim

    vec = [0.0] * dim
    features: list[str] = list(words)
    for i in range(len(words) - 1):
        features.append(f"{words[i]}_{words[i+1]}")

    for feature in features:
        h = int(hashlib.sha256(feature.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 1.0

    norm = math.sqrt(sum(val * val for val in vec))
    if norm > 0.0:
        vec = [val / norm for val in vec]
    return vec


def cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Compute cosine similarity (dot product of L2-normalized vectors)."""
    if len(v1) != len(v2):
        raise ValueError(f"Vector dimension mismatch: {len(v1)} vs {len(v2)}")
    dot = sum(a * b for a, b in zip(v1, v2))
    return max(-1.0, min(1.0, dot))


@dataclass(frozen=True)
class Case:
    case_id: str
    created_at: str
    question: str
    method: str
    answer: str
    domain: str
    evidence: dict | None
    content_sha256: str
    chain_hash: str

    def format_replay(self) -> str:
        """A replay says it is a replay and names the case."""
        return f"Replay from case {self.case_id}: {self.answer} (method: {self.method})"


class CaseMemory:
    """Tamper-evident, verification-gated case memory store."""

    DEFAULT_THRESHOLD = 0.80

    def __init__(self, path: Path, dim: int = 512, similarity_threshold: float = DEFAULT_THRESHOLD) -> None:
        self.path = Path(path)
        self.anchor_path = chainlog.anchor_path(self.path)
        self.dim = dim
        self.similarity_threshold = similarity_threshold
        self._cache: list[Case] | None = None
        self._cache_key: tuple | None = None

    def add(
        self,
        question: str,
        method: str,
        answer: str,
        *,
        domain: str | None = None,
        evidence: dict | None = None,
        case_id: str | None = None,
    ) -> Case:
        question = question.strip()
        method = method.strip()
        answer = answer.strip()

        if not question:
            raise ValueError("case question cannot be empty")
        if not method:
            raise ValueError("case method cannot be empty")
        if not answer:
            raise ValueError("case answer cannot be empty")

        # Determine effective domain
        is_math = False
        if domain is not None:
            norm_domain = domain.strip().casefold()
            if norm_domain in ("math", "maths", "mathematics"):
                is_math = True
                domain = "maths"
            else:
                domain = domain.strip()

        if not is_math and (evidence is not None or is_math_query(question, method)):
            is_math = True
            domain = "maths"

        if domain is None:
            domain = "maths" if is_math else "general"

        if is_math:
            validate_math_evidence(evidence)

        # Integrity check: verify existing log before appending
        existing = self.all()
        previous_chain_hash = existing[-1].chain_hash if existing else chainlog.GENESIS

        if case_id is None:
            case_id = str(uuid4())
        else:
            case_id = str(case_id).strip()
            if not case_id:
                case_id = str(uuid4())
        created_at = datetime.now(UTC).isoformat()

        content_payload = {
            "question": question,
            "method": method,
            "answer": answer,
            "domain": domain,
            "evidence": evidence,
        }
        canonical_content = json.dumps(content_payload, ensure_ascii=False, sort_keys=True)
        content_sha256 = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()

        case_without_chain = {
            "case_id": case_id,
            "created_at": created_at,
            "question": question,
            "method": method,
            "answer": answer,
            "domain": domain,
            "evidence": evidence,
            "content_sha256": content_sha256,
        }
        chain_hash = chainlog.chain_hash(previous_chain_hash, case_without_chain)

        case = Case(**case_without_chain, chain_hash=chain_hash)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(case), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

        chainlog.write_anchor(self.path, len(existing) + 1, chain_hash)

        self._cache = None
        self._cache_key = None
        return case

    def _stat_key(self) -> tuple:
        def key(path: Path) -> tuple[int, int] | None:
            try:
                stat = path.stat()
            except FileNotFoundError:
                return None
            return (stat.st_mtime_ns, stat.st_size)

        return (key(self.path), key(self.anchor_path))

    def all(self) -> list[Case]:
        cache_key = self._stat_key()
        if self._cache is not None and self._cache_key == cache_key:
            return self._cache

        cases, last_line = self._read_chain() if self.path.exists() else ([], 0)
        chainlog.check_tail(
            self.path,
            len(cases),
            cases[-1].chain_hash if cases else chainlog.GENESIS,
            last_line,
        )

        self._cache = cases
        self._cache_key = cache_key
        return cases

    def _read_chain(self) -> tuple[list[Case], int]:
        cases: list[Case] = []
        previous_chain_hash = chainlog.GENESIS
        last_line = 0

        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise CaseTamperError(f"invalid case record JSON at {self.path}:{line_number}") from error

            if not isinstance(raw, dict):
                raise CaseTamperError(f"case record must be object at {self.path}:{line_number}")

            if "chain_hash" not in raw:
                raise CaseTamperError(f"missing chain_hash at {self.path}:{line_number}")

            try:
                case = Case(**raw)
            except TypeError as error:
                raise CaseTamperError(f"invalid case schema at {self.path}:{line_number}") from error

            # Check domain verification invariant
            if case.domain in ("math", "maths", "mathematics"):
                validate_math_evidence(case.evidence)

            # Check single-record content integrity
            content_payload = {
                "question": case.question,
                "method": case.method,
                "answer": case.answer,
                "domain": case.domain,
                "evidence": case.evidence,
            }
            canonical_content = json.dumps(content_payload, ensure_ascii=False, sort_keys=True)
            actual_content_hash = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()
            if actual_content_hash != case.content_sha256:
                raise CaseTamperError(
                    f"case content hash mismatch at {self.path}:{line_number}"
                )

            # Check chain integrity
            case_without_chain = {
                "case_id": case.case_id,
                "created_at": case.created_at,
                "question": case.question,
                "method": case.method,
                "answer": case.answer,
                "domain": case.domain,
                "evidence": case.evidence,
                "content_sha256": case.content_sha256,
            }
            expected_chain_hash = chainlog.chain_hash(previous_chain_hash, case_without_chain)
            if expected_chain_hash != case.chain_hash:
                raise CaseTamperError(
                    f"case chain hash mismatch at {self.path}:{line_number}; "
                    f"record may have been altered, reordered, or deleted"
                )

            previous_chain_hash = case.chain_hash
            cases.append(case)
            last_line = line_number

        return cases, last_line

    def find_similar(
        self,
        query: str,
        threshold: float | None = None,
        limit: int = 5,
    ) -> list[tuple[float, Case]]:
        if limit <= 0:
            return []

        effective_threshold = self.similarity_threshold if threshold is None else threshold
        q_vec = compute_word_vector(query, dim=self.dim)

        matches: list[tuple[float, Case]] = []
        for case in self.all():
            c_vec = compute_word_vector(case.question, dim=self.dim)
            sim = cosine_similarity(q_vec, c_vec)
            if sim >= effective_threshold:
                matches.append((sim, case))

        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[:limit]

    def replay(self, case: Case) -> str:
        """Format a replay string naming the case and stating it is a replay."""
        return case.format_replay()

    def query_or_replay(
        self,
        query: str,
        threshold: float | None = None,
    ) -> tuple[str | None, Case | None]:
        matches = self.find_similar(query, threshold=threshold, limit=1)
        if matches:
            _, best_case = matches[0]
            return self.replay(best_case), best_case
        return None, None

    def __iter__(self) -> Iterator[Case]:
        yield from self.all()
