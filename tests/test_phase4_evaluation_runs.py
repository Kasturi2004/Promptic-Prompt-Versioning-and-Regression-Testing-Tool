"""Phase 4 run identity, snapshots, summaries, retrieval, and failure-state tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, test_runner
from core.ai_client import AIClientError, AIResponse


class EvaluationRunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase4.db"))
        self.db_patch.start()
        db.init_db()
        self.prompt_id = db.create_prompt_version("Answer: {input}", "baseline")
        self.case_pass = db.create_test_case("Pass case", "pass", "contains ok", "ok", "contains_keyword")
        self.case_fail = db.create_test_case("Fail case", "fail", "contains ok", "ok", "contains_keyword")
        self.case_error = db.create_test_case("Error case", "error", "contains ok", "ok", "contains_keyword")
        self.suite_id = db.create_test_suite("All outcomes", "suite snapshot",
                                             [self.case_pass, self.case_fail, self.case_error])

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def execute_suite(self):
        def provider(prompt):
            if "pass" in prompt:
                return AIResponse("ok", {"provider": "gemini", "model": "test-model", "attempts": 1})
            if "fail" in prompt:
                return "no match"
            raise AIClientError("temporarily unavailable", "provider_error", 2)

        with patch.object(test_runner, "generate_response", side_effect=provider):
            return test_runner.run_suite(self.prompt_id, "Answer: {input}", self.suite_id, max_workers=1)

    def test_run_id_unique_for_same_prompt_and_suite_and_results_are_isolated(self):
        first_results = self.execute_suite()
        second_results = self.execute_suite()
        first_id, second_id = first_results[0]["run_id"], second_results[0]["run_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(db.get_results_for_run(first_id)), 3)
        self.assertEqual(len(db.get_results_for_run(second_id)), 3)
        self.assertTrue(all(result["run_id"] == first_id for result in db.get_results_for_run(first_id)))
        self.assertTrue(all(result["run_id"] == second_id for result in db.get_results_for_run(second_id)))
        self.assertEqual(db.get_evaluation_run(first_id)["prompt_id"], self.prompt_id)
        self.assertEqual(db.get_evaluation_run(first_id)["suite_id"], self.suite_id)

    def test_run_summary_counts_pass_fail_error_and_pass_rate(self):
        results = self.execute_suite()
        run = db.get_evaluation_run(results[0]["run_id"])
        self.assertEqual([result["status"] for result in results], ["PASS", "FAIL", "ERROR"])
        self.assertEqual(run["status"], "completed_with_errors")
        self.assertEqual(run["total_tests"], 3)
        self.assertEqual(run["passed_count"], 1)
        self.assertEqual(run["failed_count"], 1)
        self.assertEqual(run["error_count"], 1)
        self.assertEqual(run["pass_rate"], 33.33)
        self.assertTrue(run["started_at"])
        self.assertTrue(run["completed_at"])
        self.assertEqual(run["metadata"]["provider"], "gemini")
        self.assertEqual(run["metadata"]["model"], "gemini-3.6-flash")
        self.assertEqual(run["metadata"]["suite_snapshot"]["name"], "All outcomes")
        self.assertEqual(run["summary"], {"total": 3, "passed": 1, "failed": 1,
                                          "errors": 1, "pass_rate": 33.33})
        with self.assertRaisesRegex(ValueError, "already finalized"):
            db.save_test_result(self.prompt_id, self.case_pass, "late", True, "late write", 0,
                                run_id=results[0]["run_id"], status="PASS")

    def test_pass_fail_only_run_is_completed_not_failed(self):
        case_ids = [self.case_pass, self.case_fail]
        cases = [case for case in db.get_all_test_cases() if case["id"] in case_ids]

        def provider(prompt):
            return "ok" if "pass" in prompt else "bad"

        with patch.object(test_runner, "generate_response", side_effect=provider):
            result = test_runner.run_tests(self.prompt_id, "Answer: {input}", cases, max_workers=1)
        run = db.get_evaluation_run(result[0]["run_id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual((run["total_tests"], run["passed_count"], run["failed_count"], run["error_count"]),
                         (2, 1, 1, 0))
        self.assertEqual(run["pass_rate"], 50.0)

    def test_run_snapshots_survive_case_prompt_and_suite_edits(self):
        with patch.object(test_runner, "generate_response", return_value="ok"):
            result = test_runner.run_suite(self.prompt_id, "Answer: {input}", self.suite_id, max_workers=1)
        run_id = result[0]["run_id"]
        original_run = db.get_evaluation_run(run_id)
        original_snapshot = original_run["metadata"]["test_cases"]
        self.assertEqual(original_run["test_case_ids"], [self.case_pass, self.case_fail, self.case_error])

        db.update_test_case(self.case_pass, "renamed", "changed input", "changed", "changed",
                            "contains_keyword", name="renamed")
        db.set_test_suite_cases(self.suite_id, [self.case_fail])
        conn = db.get_connection()
        try:
            conn.execute("UPDATE prompts SET prompt_text = ? WHERE id = ?", ("Changed {input}", self.prompt_id))
            conn.commit()
        finally:
            conn.close()

        retained = db.get_evaluation_run(run_id)
        self.assertEqual(retained["metadata"]["prompt_text"], "Answer: {input}")
        self.assertEqual(retained["test_case_ids"], [self.case_pass, self.case_fail, self.case_error])
        self.assertEqual(retained["metadata"]["test_cases"], original_snapshot)
        self.assertEqual(retained["metadata"]["suite_snapshot"]["name"], "All outcomes")

    def test_run_query_apis_and_latest_completed_do_not_mix_prompt_runs(self):
        results_a = self.execute_suite()
        results_b = self.execute_suite()
        prompt_runs = db.get_evaluation_runs_for_prompt(self.prompt_id)
        suite_runs = db.get_evaluation_runs_for_suite(self.suite_id)
        self.assertEqual({run["run_id"] for run in prompt_runs},
                         {results_a[0]["run_id"], results_b[0]["run_id"]})
        self.assertEqual({run["run_id"] for run in suite_runs},
                         {results_a[0]["run_id"], results_b[0]["run_id"]})
        self.assertEqual(db.get_latest_completed_run(prompt_id=self.prompt_id)["run_id"], prompt_runs[0]["run_id"])
        self.assertIsNone(db.get_latest_completed_run(prompt_id=99999))

    def test_fatal_executor_startup_failure_marks_run_failed(self):
        cases = [db.get_all_test_cases()[0]]
        with patch.object(test_runner, "ThreadPoolExecutor", side_effect=RuntimeError("executor unavailable")):
            with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
                test_runner.run_tests(self.prompt_id, "Answer: {input}", cases, max_workers=1)
        run = db.get_evaluation_runs_for_prompt(self.prompt_id)[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["completed_at"] is not None, True)
        self.assertEqual(run["total_tests"], 1)
        self.assertEqual(run["pass_rate"], 0.0)

    def test_fresh_and_legacy_database_schemas_keep_run_and_result_history(self):
        run_results = self.execute_suite()
        self.assertEqual(len(db.get_results_for_run(run_results[0]["run_id"])), 3)
        legacy_path = Path(self.temp.name) / "legacy-run.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript("""
            CREATE TABLE prompts (id INTEGER PRIMARY KEY, version_number INTEGER NOT NULL, prompt_text TEXT NOT NULL, commit_message TEXT, created_at TEXT NOT NULL);
            CREATE TABLE test_cases (id INTEGER PRIMARY KEY, description TEXT, input_text TEXT NOT NULL, expected_condition TEXT, expected_keyword TEXT, scoring_type TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE evaluation_runs (run_id TEXT PRIMARY KEY, prompt_id INTEGER NOT NULL, test_case_ids TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running', metadata_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE test_results (id INTEGER PRIMARY KEY, run_id TEXT, prompt_id INTEGER NOT NULL, test_case_id INTEGER NOT NULL, ai_response TEXT, passed INTEGER NOT NULL, score_reason TEXT, execution_time REAL, run_at TEXT NOT NULL, error_message TEXT);
            INSERT INTO prompts VALUES (5, 1, 'Old {input}', 'old', '2025-01-01T00:00:00');
            INSERT INTO test_cases VALUES (8, 'Old case', 'input', 'contains ok', 'ok', 'keyword_contains', '2025-01-01T00:00:00');
            INSERT INTO evaluation_runs VALUES ('legacy-run-id', 5, '[8]', '2025-01-02T00:00:00', 'completed', '{}');
            INSERT INTO test_results VALUES (12, 'legacy-run-id', 5, 8, 'ok', 1, 'matched', 0.1, '2025-01-02T00:00:01', NULL);
        """)
        conn.close()
        with patch.object(db, "DB_PATH", str(legacy_path)):
            db.init_db()
            migrated_run = db.get_evaluation_run("legacy-run-id")
            migrated_result = db.get_results_for_run("legacy-run-id")
        self.assertEqual(migrated_run["run_id"], "legacy-run-id")
        self.assertEqual(migrated_run["test_case_ids"], [8])
        self.assertEqual(migrated_run["total_tests"], 1)
        self.assertEqual(migrated_run["passed_count"], 1)
        self.assertEqual(migrated_run["pass_rate"], 100.0)
        self.assertIsNone(migrated_run["completed_at"])  # old schema did not record a finish time
        self.assertEqual(migrated_result[0]["id"], 12)
        self.assertEqual(migrated_result[0]["test_case_id"], 8)


if __name__ == "__main__":
    unittest.main()
