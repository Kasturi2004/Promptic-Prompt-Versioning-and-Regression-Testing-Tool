"""Reliability tests; all provider calls are mocked and use a temporary DB."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, test_runner
from core.regression import compare_versions, summarize_comparison


class ReliabilityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def case(self):
        cid = db.create_test_case("must retain key", "input", "contains gold", "gold", "keyword_contains")
        return db.get_all_test_cases()[0]

    def test_full_prompt_version_evaluation_and_regression_workflow(self):
        case = self.case()
        first = db.create_prompt_version("Answer {input}", "baseline")
        with patch.object(test_runner, "generate_response", return_value="gold answer"):
            first_result = test_runner.run_tests(first, "Answer {input}", [case], max_workers=1)
        self.assertTrue(first_result[0]["passed"])
        run_a = first_result[0]["run_id"]
        self.assertEqual(len(db.get_results_for_run(run_a)), 1)

        second = db.create_prompt_version("Answer {input} briefly", "changed")
        with patch.object(test_runner, "generate_response", return_value="silver answer"):
            second_result = test_runner.run_tests(second, "Answer {input} briefly", [case], max_workers=1)
        self.assertFalse(second_result[0]["passed"])
        run_b = second_result[0]["run_id"]
        comparison = compare_versions(run_a, run_b)
        self.assertEqual(comparison[0]["status"], "REGRESSION")
        self.assertEqual(summarize_comparison(comparison)["regressions"], 1)

    def test_provider_failure_is_stored_as_failed_result(self):
        case = self.case()
        prompt = db.create_prompt_version("Answer {input}", "baseline")
        with patch.object(test_runner, "generate_response", side_effect=test_runner.AIClientError("offline")):
            result = test_runner.run_tests(prompt, "Answer {input}", [case], max_workers=1)
        self.assertFalse(result[0]["passed"])
        self.assertIn("offline", result[0]["error_message"])
        self.assertEqual(len(db.get_results_for_run(result[0]["run_id"])), 1)

    def test_invalid_case_rejected_before_provider_call(self):
        prompt = db.create_prompt_version("Answer {input}", "baseline")
        bad = {"id": 1, "input_text": "x", "expected_keyword": "[", "scoring_type": "regex_match"}
        with patch.object(test_runner, "generate_response") as provider:
            with self.assertRaisesRegex(ValueError, "Invalid regex"):
                test_runner.run_tests(prompt, "Answer {input}", [bad])
        provider.assert_not_called()

    def test_invalid_prompt_and_concurrency_rejected(self):
        with self.assertRaises(ValueError):
            db.create_prompt_version("  ", "empty")
        with self.assertRaises(ValueError):
            test_runner.run_tests(1, "prompt", [], max_workers=0)

    def test_latest_result_uses_insert_order_on_timestamp_tie(self):
        case = self.case()
        prompt = db.create_prompt_version("Answer {input}", "baseline")
        with patch.object(db, "datetime") as mocked_datetime:
            mocked_datetime.now.return_value.isoformat.return_value = "same-time"
            db.save_test_result(prompt, case["id"], "old", 1, "ok", 0)
            db.save_test_result(prompt, case["id"], "new", 0, "bad", 0)
        self.assertEqual(db.get_latest_result_per_test_case(prompt)[0]["ai_response"], "new")


if __name__ == "__main__":
    unittest.main()
