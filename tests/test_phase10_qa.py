"""Final QA regression checks using isolated SQLite and mocked provider calls."""
import sqlite3
import tempfile
import unittest
import importlib
from pathlib import Path
from unittest.mock import Mock, patch

from core import db, test_runner
from core.regression import compare_runs
from ui.prompt_workspace import (
    consume_compare_candidate_context, consume_workspace_navigation,
    normalize_workspace_page, request_workspace_navigation,
)


class FinalWorkflowQATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "qa.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_complete_version_suite_run_result_compare_improve_workflow(self):
        first_prompt_id = db.create_prompt_version("Version one: {input}", "baseline")
        first_prompt = db.get_prompt_by_id(first_prompt_id)
        case_id = db.create_test_case("Refund request", "refund please", "contains gold",
                                      "gold", "contains_keyword", name="Refund request")
        suite_id = db.create_test_suite("Support suite", "Customer support checks", [case_id])

        second_prompt_id = db.create_prompt_version("Version two: {input}", "tightened response")
        self.assertEqual(db.get_prompt_by_id(first_prompt_id), first_prompt)
        self.assertEqual(db.get_prompt_by_id(second_prompt_id)["commit_message"], "tightened response")
        self.assertEqual([case["id"] for case in db.get_test_cases_for_suite(suite_id)], [case_id])

        def fake_provider(rendered_prompt):
            return "gold response" if rendered_prompt.startswith("Version one:") else "silver response"

        with patch.object(test_runner, "generate_response", side_effect=fake_provider):
            baseline_results = test_runner.run_suite(first_prompt_id, first_prompt["prompt_text"], suite_id,
                                                     max_workers=1)
            candidate_text = db.get_prompt_by_id(second_prompt_id)["prompt_text"]
            candidate_results = test_runner.run_suite(second_prompt_id, candidate_text, suite_id, max_workers=1)

        baseline_id = baseline_results[0]["run_id"]
        candidate_id = candidate_results[0]["run_id"]
        self.assertEqual(baseline_results[0]["status"], "PASS")
        self.assertEqual(candidate_results[0]["status"], "FAIL")
        self.assertEqual(db.get_results_for_run(baseline_id)[0]["test_case_id"], case_id)
        self.assertEqual(db.get_evaluation_run(candidate_id)["suite_id"], suite_id)

        comparison = compare_runs(baseline_id, candidate_id)
        self.assertEqual(comparison.overall_status, "WORSE")
        self.assertEqual(comparison.tests[0].classification, "REGRESSION")
        self.assertEqual(comparison.baseline_run_id, baseline_id)
        self.assertEqual(comparison.candidate_run_id, candidate_id)

        state = {"compare_baseline_run": baseline_id,
                 "compare_candidate_pending_run_id": candidate_id}
        self.assertEqual(consume_compare_candidate_context(state, {baseline_id, candidate_id}), candidate_id)
        self.assertIsNone(state["compare_baseline_run"], "Candidate context must not choose a baseline.")
        request_workspace_navigation(state, "Prompt Workspace",
                                     prompt_workspace_pending_version_id=second_prompt_id)
        target = consume_workspace_navigation(state, {"Prompt Workspace"})
        self.assertEqual(normalize_workspace_page(target, {}), "Prompt Workspace")
        self.assertEqual(state["prompt_workspace_pending_version_id"], second_prompt_id)

    def test_stale_test_case_update_fails_instead_of_reporting_success(self):
        with self.assertRaisesRegex(ValueError, "Test case does not exist"):
            db.update_test_case(99999, "Missing", "input", "expected", "value",
                                "contains_keyword", name="Missing")

    def test_failed_test_case_insert_rolls_back_and_closes_connection(self):
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = sqlite3.OperationalError("disk unavailable")
        with patch.object(db, "get_connection", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                db.create_test_case("Example", "input", "contains expected", "expected", "contains_keyword")
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_manual_gemini_demos_are_import_safe(self):
        with patch.object(db, "init_db", side_effect=AssertionError("demo ran during import")), \
                patch.object(test_runner, "run_tests", side_effect=AssertionError("provider run during import")):
            importlib.import_module("scripts.demo_diff")
            importlib.import_module("scripts.demo_phase7")
            importlib.import_module("scripts.demo_phase9")


if __name__ == "__main__":
    unittest.main()
