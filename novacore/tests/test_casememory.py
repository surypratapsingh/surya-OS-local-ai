"""Unit tests for E4 Case Memory (novacore.casememory)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from novacore.casememory import (
    Case,
    CaseMemory,
    CaseTamperError,
    CaseVerificationError,
    compute_word_vector,
    cosine_similarity,
    is_math_query,
)


class CaseMemoryTests(unittest.TestCase):
    def test_add_math_case_with_verified_evidence_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            evidence = {
                "verdict": "Verified",
                "points_tested": 16,
                "max_relative_error": 0.0,
                "tool": "mathd",
            }
            case = store.add(
                question="What is the derivative of x^3 with respect to x?",
                method="power_rule",
                answer="3*x^2",
                domain="maths",
                evidence=evidence,
            )
            
            self.assertEqual(case.question, "What is the derivative of x^3 with respect to x?")
            self.assertEqual(case.method, "power_rule")
            self.assertEqual(case.answer, "3*x^2")
            self.assertEqual(case.domain, "maths")
            self.assertEqual(case.evidence, evidence)
            self.assertTrue(log_path.exists())
            
            loaded = store.all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].case_id, case.case_id)
            self.assertEqual(loaded[0].chain_hash, case.chain_hash)

    def test_add_math_case_without_evidence_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            with self.assertRaises(CaseVerificationError):
                store.add(
                    question="Compute the integral of sin(x) dx",
                    method="antiderivative",
                    answer="-cos(x) + C",
                    domain="maths",
                    evidence=None,
                )
            
            # Nothing should be stored
            self.assertEqual(len(store.all()), 0)

    def test_add_math_case_with_unverified_verdicts_refused(self) -> None:
        """Each unverified verdict is supplied with complete parameters so only the verdict check fires."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            unverified_verdicts = [
                {
                    "verdict": "Failed",
                    "points_tested": 16,
                    "max_relative_error": 0.05,
                    "counterexample": {"x": 2.0},
                    "expected": 4.0,
                    "got": 2.0,
                },
                {
                    "verdict": "Unverifiable",
                    "points_tested": 16,
                    "max_relative_error": 0.0,
                    "reason": "timeout",
                },
                {
                    "verdict": "FailedDomain",
                    "points_tested": 16,
                    "max_relative_error": 0.0,
                    "counterexample": {"x": -1.0},
                    "undefined": "Rhs",
                    "defined_value": 1.0,
                },
                {
                    "verdict": "Unknown",
                    "points_tested": 16,
                    "max_relative_error": 0.0,
                },
            ]
            
            for ev in unverified_verdicts:
                verdict_name = ev.get("verdict")
                with self.subTest(verdict=verdict_name):
                    with self.assertRaises(CaseVerificationError) as ctx:
                        store.add(
                            question="Solve x^2 - 4 = 0",
                            method="factor",
                            answer="x = 2",
                            domain="maths",
                            evidence=ev,
                        )
                    self.assertIn(
                        f"verdict is '{verdict_name}', required 'Verified'",
                        str(ctx.exception),
                    )
            
            self.assertEqual(len(store.all()), 0)

    def test_add_math_case_with_invalid_evidence_parameters_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            # points_tested < 1
            with self.assertRaises(CaseVerificationError):
                store.add(
                    question="Solve x + 1 = 2",
                    method="subtract",
                    answer="x = 1",
                    domain="maths",
                    evidence={"verdict": "Verified", "points_tested": 0, "max_relative_error": 0.0},
                )
            
            # max_relative_error is infinite
            with self.assertRaises(CaseVerificationError):
                store.add(
                    question="Solve x + 1 = 2",
                    method="subtract",
                    answer="x = 1",
                    domain="maths",
                    evidence={"verdict": "Verified", "points_tested": 16, "max_relative_error": float("inf")},
                )

    def test_implicit_math_query_requires_verified_evidence(self) -> None:
        """A maths answer without domain='maths' is still detected and requires Verified evidence."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            with self.assertRaises(CaseVerificationError):
                store.add(
                    question="Evaluate the definite integral of x^2 from 0 to 1",
                    method="fundamental_theorem",
                    answer="1/3",
                    evidence=None,
                )

    def test_all_math_pattern_keywords_implicitly_detected_and_gated(self) -> None:
        """Every keyword in _MATH_PATTERNS is exercised by an independent question without evidence.
        
        Dropping any keyword (such as 'differentiate' in E4-P4) causes implicit detection to miss,
        storing the unverified question as domain='general' and failing this test.
        """
        keyword_probes = [
            ("integral", "Calculate the improper integral of f(t) dt"),
            ("integrate", "Please integrate this flow over time"),
            ("derivative", "Find the second derivative of position"),
            ("differentiate", "Differentiate 4t cubed with respect to t"),
            ("differential", "Solve the linear differential model"),
            ("polynomial", "Determine the degree of this polynomial"),
            ("quadratic", "Find the vertex of this quadratic curve"),
            ("factorize", "Factorize the grouped terms"),
            ("factorise", "Factorise the grouped terms"),
            ("factorisation", "Determine the prime factorisation"),
            ("factorization", "Perform LU factorization"),
            ("equation", "Balance the recurrence equation"),
            ("eigenvalue", "Compute the dominant eigenvalue"),
            ("matrix", "Invert the covariance matrix"),
            ("trigonometric", "Simplify the trigonometric ratio"),
            ("calculus", "State the fundamental theorem of calculus"),
            ("algebra", "Verify the Lie algebra commutator"),
            ("logarithm", "Compute the discrete logarithm"),
            ("fourier", "Compute the discrete fourier series"),
            ("laplace", "Compute the unilateral laplace transform"),
            ("taylor", "Derive the taylor expansion about the origin"),
            ("series", "Determine whether the alternating series converges"),
            ("limit", "Evaluate the limit as delta approaches zero"),
            ("antiderivative", "Find the most general antiderivative"),
            ("sin", "Compute the value of sin of angle theta"),
            ("cos", "Compute the value of cos of angle theta"),
            ("tan", "Compute the value of tan of angle theta"),
            ("cot", "Compute the value of cot of angle theta"),
            ("sec", "Compute the value of sec of angle theta"),
            ("csc", "Compute the value of csc of angle theta"),
            (r"\int", r"Evaluate \int f dt numerically"),
            (r"\frac", r"Simplify \frac numerator denominator"),
            (r"\partial", r"Compute \partial u / \partial t"),
            (r"\sum", r"Compute \sum a_k over indices"),
            (r"\prod", r"Compute \prod b_k over indices"),
            (r"\sqrt", r"Evaluate \sqrt of 49"),
            ("^", "Evaluate 2 ^ 10"),
            ("x ^", "Simplify x ^ 4"),
            ("y *", "Compute y * 8"),
            ("z +", "Compute z + 5"),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)

            for kw, query in keyword_probes:
                with self.subTest(keyword=kw):
                    with self.assertRaises(CaseVerificationError, msg=f"Keyword '{kw}' must trigger math detection and require evidence"):
                        store.add(
                            question=query,
                            method="symbolic_eval",
                            answer="result",
                            evidence=None,
                        )
            
            # Ensure nothing unverified was stored
            self.assertEqual(len(store.all()), 0)

    def test_add_non_math_case_succeeds_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            case = store.add(
                question="How do I configure static IP address in systemd-networkd?",
                method="networkd_config",
                answer="Write /etc/systemd/network/10-static.network",
                domain="config",
                evidence=None,
            )
            
            self.assertEqual(case.domain, "config")
            self.assertIsNone(case.evidence)
            self.assertEqual(len(store.all()), 1)

    def test_tampered_content_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            store.add(
                question="How do I create a compressed tarball archive?",
                method="tar_czvf",
                answer="tar -czvf archive.tar.gz folder",
                domain="how-to",
            )
            
            # Tamper the file text directly
            content = log_path.read_text(encoding="utf-8")
            tampered = content.replace("archive.tar.gz", "malicious.tar.gz")
            log_path.write_text(tampered, encoding="utf-8")
            
            new_store = CaseMemory(log_path)
            with self.assertRaises(CaseTamperError) as ctx:
                new_store.all()
            self.assertIn("content hash mismatch", str(ctx.exception))

    def test_tampered_content_hash_field_detected(self) -> None:
        """Tampering content_sha256 alone is specifically caught by the content_sha256 check."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            store.add(
                question="How do I list files?",
                method="ls_la",
                answer="ls -la",
                domain="how-to",
            )
            
            lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            lines[0]["content_sha256"] = "0" * 64
            log_path.write_text(json.dumps(lines[0], ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            
            new_store = CaseMemory(log_path)
            with self.assertRaises(CaseTamperError) as ctx:
                new_store.all()
            self.assertIn("content hash mismatch", str(ctx.exception))

    def test_tampered_chain_hash_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            store.add(
                question="Question 1",
                method="Method 1",
                answer="Answer 1",
                domain="general",
            )
            store.add(
                question="Question 2",
                method="Method 2",
                answer="Answer 2",
                domain="general",
            )
            
            # Modify record 1 content and recompute content_sha256, but leave chain_hash
            lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            lines[0]["answer"] = "Tampered Answer"
            import hashlib
            payload = {
                "question": lines[0]["question"],
                "method": lines[0]["method"],
                "answer": lines[0]["answer"],
                "domain": lines[0]["domain"],
                "evidence": lines[0]["evidence"],
            }
            lines[0]["content_sha256"] = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            
            log_path.write_text("\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in lines) + "\n")
            
            new_store = CaseMemory(log_path)
            with self.assertRaises(CaseTamperError):
                new_store.all()

    def test_anchor_tail_mismatch_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            store.add("Question 1", "Method 1", "Answer 1", domain="general")
            store.add("Question 2", "Method 2", "Answer 2", domain="general")
            
            # Drop the second record from log, leaving anchor recording 2 records
            lines = log_path.read_text(encoding="utf-8").splitlines()
            log_path.write_text(lines[0] + "\n", encoding="utf-8")
            
            new_store = CaseMemory(log_path)
            with self.assertRaises(ValueError):
                new_store.all()

    def test_replay_formatting_states_replay_and_names_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path)
            
            case = store.add(
                question="How do I configure static IP?",
                method="networkd",
                answer="Write /etc/systemd/network/10-static.network",
                domain="config",
            )
            
            replay_text = store.replay(case)
            self.assertIn("replay", replay_text.casefold())
            self.assertIn(case.case_id, replay_text)
            self.assertEqual(replay_text, case.format_replay())

    def test_find_similar_cosine_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            store = CaseMemory(log_path, similarity_threshold=0.5)
            
            ev = {"verdict": "Verified", "points_tested": 16, "max_relative_error": 0.0}
            c1 = store.add(
                question="Compute the indefinite integral of cos(x) dx",
                method="antiderivative_lookup",
                answer="sin(x) + C",
                domain="maths",
                evidence=ev,
            )
            c2 = store.add(
                question="How do I configure static IP address in systemd-networkd?",
                method="networkd",
                answer="Write /etc/systemd/network/10-static.network",
                domain="config",
            )
            
            similar = store.find_similar("Calculate the indefinite integral of cos(x) dx")
            self.assertTrue(len(similar) >= 1)
            best_sim, best_case = similar[0]
            self.assertEqual(best_case.case_id, c1.case_id)
            self.assertGreater(best_sim, 0.75)
            
            replay_str, matched_case = store.query_or_replay("Calculate the indefinite integral of cos(x) dx")
            self.assertIsNotNone(replay_str)
            self.assertEqual(matched_case.case_id, c1.case_id)

    def test_paraphrase_fixture_evaluation_and_false_replay_rate(self) -> None:
        """Evaluate paraphrase fixture, assert provenance, and measure false-replay rate."""
        fixture_path = Path(__file__).parent / "fixtures" / "case_paraphrases.json"
        self.assertTrue(fixture_path.exists(), f"Missing fixture at {fixture_path}")
        
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertIn("_provenance", data)
        self.assertIn("Hand-written", data["_provenance"])
        
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cases.jsonl"
            threshold = 0.80
            store = CaseMemory(log_path, similarity_threshold=threshold)
            
            for item in data["cases"]:
                store.add(
                    question=item["question"],
                    method=item["method"],
                    answer=item["answer"],
                    domain=item.get("domain"),
                    evidence=item.get("evidence"),
                    case_id=item.get("case_id"),
                )

            true_positives = 0
            false_positives = 0
            true_negatives = 0
            false_negatives = 0

            results = []
            for q in data["queries"]:
                query_text = q["query"]
                is_para = q["is_paraphrase"]
                expected_case_id = q.get("expected_case_id")

                # Find all similarities for reporting
                all_matches = store.find_similar(query_text, threshold=0.0, limit=len(data["cases"]))
                best_sim = all_matches[0][0] if all_matches else 0.0
                best_case = all_matches[0][1] if all_matches else None

                # Replay decision at the fixed evaluation threshold
                matches = store.find_similar(query_text, threshold=threshold, limit=1)
                replayed = len(matches) > 0
                matched_case = matches[0][1] if replayed else None

                if is_para:
                    if replayed and matched_case.case_id == expected_case_id:
                        true_positives += 1
                        status = "TP"
                    else:
                        false_negatives += 1
                        status = "FN"
                else:
                    if replayed:
                        false_positives += 1
                        status = "FP"
                    else:
                        true_negatives += 1
                        status = "TN"

                results.append({
                    "query": query_text,
                    "is_paraphrase": is_para,
                    "sim": round(best_sim, 4),
                    "replayed": replayed,
                    "matched_case_id": matched_case.case_id if matched_case else None,
                    "status": status,
                })
            
            total_negatives = false_positives + true_negatives
            false_replay_rate = false_positives / total_negatives if total_negatives > 0 else 0.0
            total_positives = true_positives + false_negatives
            recall = true_positives / total_positives if total_positives > 0 else 0.0
            
            # Report measured numbers
            print("\n=== PARAPHRASE FIXTURE REPLAY EVALUATION ===")
            print(f"Fixed Similarity Threshold: {threshold}")
            print(f"Total Queries: {len(data['queries'])}")
            print(f"True Positives: {true_positives}/{total_positives} (Recall: {recall:.2%})")
            print(f"False Positives: {false_positives}/{total_negatives}")
            print(f"True Negatives: {true_negatives}/{total_negatives}")
            print(f"False Negatives: {false_negatives}/{total_positives}")
            print(f"False Replay Rate: {false_replay_rate:.4f} ({false_replay_rate:.2%})")
            for r in results:
                print(f"  [{r['status']}] sim={r['sim']:.4f} replayed={r['replayed']} | {r['query']}")
            print("============================================\n")
            
            # Pin empirical measurement baseline for regression protection (F4)
            self.assertEqual(true_positives, 5)
            self.assertEqual(false_positives, 2)
            self.assertEqual(true_negatives, 4)
            self.assertEqual(false_negatives, 2)
            self.assertAlmostEqual(false_replay_rate, 2 / 6, places=4)
            self.assertAlmostEqual(recall, 5 / 7, places=4)
            self.assertIsInstance(false_replay_rate, float)


if __name__ == "__main__":
    unittest.main()
