"""Phase 6 UI-facing helper tests; no Streamlit server or Gemini calls required."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import db
from core.regression import compare_runs
from ui.result_helpers import (
    compare_selected_runs, comparison_groups, comparison_interpretation,
    filter_results, load_selected_run, result_status, run_label, run_summary,
    safe_error_message, snapshot_map,
)


class ResultsPageHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase6.db"))
        self.db_patch.start()
        db.init_db()
        self.prompt = db.create_prompt_version("Answer {input}", "current")
        self.case = db.create_test_case("Case original", "input original", "contains answer", "answer", "contains_keyword")
        self.run_id = db.create_evaluation_run(self.prompt, [self.case], metadata={
            "prompt_text": "Answer {input}", "test_cases": [{
                "id": self.case, "name": "Snapshot case", "description": "Historical description",
                "input_text": "Historical input", "expected_keyword": "answer", "scoring_type": "contains_keyword",
            }],
        })
        db.save_test_result(self.prompt, self.case, "answer returned", True, "matched", 0.25,
                            run_id=self.run_id, status="PASS",
                            metadata={"evaluation": {"expected": "answer", "actual": "answer returned",
                                                      "evaluation_method": "contains_keyword", "reason": "matched"}})
        db.finish_evaluation_run(self.run_id)

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_selected_run_loads_its_persisted_results_only(self):
        run, results = load_selected_run(self.run_id)
        self.assertEqual(run["run_id"], self.run_id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["run_id"], self.run_id)
        with self.assertRaisesRegex(ValueError, "no longer available"):
            load_selected_run("deleted-run")
        with self.assertRaisesRegex(ValueError, "do not match"):
            load_selected_run(self.run_id, run_loader=lambda _: run,
                              results_loader=lambda _: [{"run_id": "another-run"}])

    def test_run_summary_uses_phase4_persisted_counts_and_rate(self):
        run = db.get_evaluation_run(self.run_id)
        self.assertEqual(run_summary(run), {
            "total": 1, "passed": 1, "failed": 0, "errors": 0,
            "pass_rate": 100.0, "status": "completed",
        })

    def test_status_filters_keep_error_distinct_from_fail(self):
        results = [{"status": value, "passed": value == "PASS"} for value in ("PASS", "FAIL", "ERROR")]
        self.assertEqual([result_status(r) for r in filter_results(results, "Errors")], ["ERROR"])
        self.assertEqual([result_status(r) for r in filter_results(results, "Failed")], ["FAIL"])
        self.assertEqual(len(filter_results(results, "All")), 3)

    def test_snapshot_and_human_readable_run_label(self):
        run = db.get_evaluation_run(self.run_id)
        self.assertEqual(snapshot_map(run)[self.case]["input_text"], "Historical input")
        prompt = db.get_all_prompts()[0]
        label = run_label(run, {prompt["id"]: prompt}, {})
        self.assertIn("Prompt v1", label)
        self.assertIn("No suite", label)
        self.assertIn(self.run_id[:8], label)

    def test_error_display_redacts_key_shaped_text(self):
        text = safe_error_message("authorization: Bearer abc123 api_key=secret-value")
        self.assertNotIn("abc123", text)
        self.assertNotIn("secret-value", text)


class ComparisonPageHelperTests(unittest.TestCase):
    def test_exact_explicit_ids_are_forwarded_and_same_run_is_rejected(self):
        comparator = Mock(return_value="comparison")
        self.assertEqual(compare_selected_runs("baseline-uuid", "candidate-uuid", comparator), "comparison")
        comparator.assert_called_once_with("baseline-uuid", "candidate-uuid")
        with self.assertRaisesRegex(ValueError, "different runs"):
            compare_selected_runs("same", "same", comparator)
        with self.assertRaisesRegex(ValueError, "both"):
            compare_selected_runs(None, "candidate", comparator)

    def test_comparison_groups_and_deterministic_interpretation(self):
        temp = tempfile.TemporaryDirectory()
        try:
            with patch.object(db, "DB_PATH", str(Path(temp.name) / "comparison.db")):
                db.init_db()
                prompt_a = db.create_prompt_version("A {input}", "A")
                prompt_b = db.create_prompt_version("B {input}", "B")
                case_id = db.create_test_case("Case", "input", "contains x", "x", "contains_keyword")
                run_a = db.create_evaluation_run(prompt_a, [case_id])
                run_b = db.create_evaluation_run(prompt_b, [case_id])
                db.save_test_result(prompt_a, case_id, "x", True, "ok", 0, run_id=run_a, status="PASS")
                db.save_test_result(prompt_b, case_id, "none", False, "missing", 0, run_id=run_b, status="FAIL")
                db.finish_evaluation_run(run_a)
                db.finish_evaluation_run(run_b)
                comparison = compare_runs(run_a, run_b)
                groups = comparison_groups(comparison)
                self.assertEqual(len(groups["REGRESSION"]), 1)
                self.assertEqual(comparison_interpretation(comparison), "Candidate regressed overall.")
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
