"""Phase 3 test-case and test-suite behavior using isolated SQLite databases."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, test_runner
from core.scorer import evaluate_response


class Phase3CaseAndSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase3.db"))
        self.db_patch.start()
        db.init_db()
        self.prompt_a = db.create_prompt_version("Answer: {input}", "baseline")
        self.prompt_b = db.create_prompt_version("Answer clearly: {input}", "revision")

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def add_case(self, name, expected="refund", method="contains_keyword", enabled=True):
        return db.create_test_case(name, "refund request", "response includes keyword",
                                   expected, method, name=name, enabled=enabled)

    def test_case_id_survives_rename_and_runs_against_multiple_prompt_versions(self):
        case_id = self.add_case("Customer refund")
        first_case = db.get_all_test_cases()[0]
        with patch.object(test_runner, "generate_response", return_value="refund approved"):
            run_a = test_runner.run_tests(self.prompt_a, "Answer: {input}", [first_case], max_workers=1)[0]

        db.update_test_case(case_id, "Refund updated", "refund request", "contains refund",
                            "refund", "contains_keyword", name="Refund renamed")
        renamed = next(case for case in db.get_all_test_cases() if case["id"] == case_id)
        self.assertEqual(renamed["name"], "Refund renamed")
        self.assertEqual(renamed["id"], case_id)
        with patch.object(test_runner, "generate_response", return_value="refund processed"):
            run_b = test_runner.run_tests(self.prompt_b, "Answer clearly: {input}", [renamed], max_workers=1)[0]
        self.assertEqual(run_a["test_case_id"], run_b["test_case_id"])
        self.assertEqual(db.get_results_for_run(run_a["run_id"])[0]["test_case_id"], case_id)
        self.assertEqual(db.get_results_for_run(run_b["run_id"])[0]["test_case_id"], case_id)

    def test_required_fields_evaluator_and_configuration_validation(self):
        valid = {"id": 1, "name": "Valid", "input_text": "hello", "expected_keyword": "ok",
                 "scoring_type": "contains_keyword", "enabled": True}
        db.validate_test_case(valid)
        for bad, message in (
            ({"input_text": "hello", "expected_keyword": "ok", "scoring_type": "contains_keyword"}, "name"),
            ({"name": "No input", "expected_keyword": "ok", "scoring_type": "contains_keyword"}, "input_text"),
            ({"name": "No method", "input_text": "hello", "expected_keyword": "ok"}, "Unknown evaluation method"),
            ({"name": "Bad method", "input_text": "hello", "expected_keyword": "ok", "scoring_type": "judge"}, "Unknown evaluation method"),
            ({"name": "Bad regex", "input_text": "hello", "expected_keyword": "[", "scoring_type": "regex_match"}, "Invalid regex"),
            ({"name": "Bad list", "input_text": "hello", "expected_keyword": ["ok", 4], "scoring_type": "contains_all_keywords"}, "non-empty list"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                db.validate_test_case(bad)

    def test_phase2_evaluators_integrate_with_test_case_config(self):
        examples = [
            ("contains_keyword", "refund", "a refund is available", "PASS"),
            ("exact_match", "same", "same", "PASS"),
            ("contains_all_keywords", ["refund", "policy"], "refund policy", "PASS"),
            ("contains_any_keyword", ["refund", "credit"], "store credit", "PASS"),
            ("regex_match", r"order #[0-9]+", "order #123", "PASS"),
            ("response_not_empty", None, "text", "PASS"),
        ]
        for method, expected, actual, status in examples:
            with self.subTest(method=method):
                case_id = self.add_case(method, expected, method)
                case = next(case for case in db.get_all_test_cases() if case["id"] == case_id)
                evaluation = evaluate_response(actual, case["expected_keyword"], case["scoring_type"])
                self.assertEqual(evaluation.status, status)
                self.assertEqual(evaluation.evaluation_method, method)

    def test_disabled_case_is_retained_and_skipped_by_normal_run(self):
        enabled_id = self.add_case("Enabled")
        disabled_id = self.add_case("Disabled", enabled=False)
        cases = db.get_all_test_cases()
        with patch.object(test_runner, "generate_response", return_value="refund approved") as provider:
            result = test_runner.run_tests(self.prompt_a, "Answer: {input}", cases, max_workers=1)
        self.assertEqual([item["test_case_id"] for item in result], [enabled_id])
        self.assertEqual(provider.call_count, 1)
        retained = {case["id"]: case for case in db.get_all_test_cases()}
        self.assertIn(disabled_id, retained)
        self.assertEqual(retained[disabled_id]["enabled"], 0)
        db.set_test_case_enabled(disabled_id, True)
        self.assertEqual(next(c for c in db.get_all_test_cases() if c["id"] == disabled_id)["enabled"], 1)

    def test_suite_membership_enabled_filter_validation_and_stable_ids(self):
        first = self.add_case("First")
        disabled = self.add_case("Disabled", enabled=False)
        third = self.add_case("Third")
        suite_id = db.create_test_suite("Refund suite", "suite description", [first, disabled, third])
        self.assertEqual([case["id"] for case in db.get_test_cases_for_suite(suite_id)], [first, third])
        all_members = db.get_test_cases_for_suite(suite_id, enabled_only=False)
        self.assertEqual([case["id"] for case in all_members], [first, disabled, third])
        db.set_test_suite_cases(suite_id, [third, first, disabled])
        self.assertEqual({case["id"] for case in db.get_test_cases_for_suite(suite_id)}, {first, third})
        self.assertEqual(next(case for case in db.get_all_test_cases() if case["id"] == first)["id"], first)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            db.set_test_suite_cases(suite_id, [first, first])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            db.set_test_suite_cases(suite_id, [9999])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            db.get_test_cases_for_suite(9999)

    def test_run_suite_executes_only_enabled_members_and_records_suite_and_case_ids(self):
        case_a = self.add_case("A")
        case_b = self.add_case("B", enabled=False)
        suite_id = db.create_test_suite("Only enabled", test_case_ids=[case_a, case_b])
        with patch.object(test_runner, "generate_response", return_value="refund"):
            results = test_runner.run_suite(self.prompt_a, "Answer: {input}", suite_id, max_workers=1)
        self.assertEqual([result["test_case_id"] for result in results], [case_a])
        run = db.get_evaluation_run(results[0]["run_id"])
        self.assertEqual(run["metadata"]["suite_id"], suite_id)
        self.assertEqual(run["test_case_ids"], [case_a])
        db.set_test_suite_enabled(suite_id, False)
        with self.assertRaisesRegex(ValueError, "disabled"):
            test_runner.run_suite(self.prompt_a, "Answer: {input}", suite_id, max_workers=1)

    def test_old_yaml_shape_imports_and_historical_result_survives_migration(self):
        # This is the field shape produced by yaml.safe_load for an existing-format file.
        legacy_case = {"description": "Old format case", "input_text": "keep me",
                       "expected_condition": "must contain ok", "expected_keyword": "ok",
                       "scoring_type": "keyword_contains"}
        case_id = db.create_test_case(legacy_case["description"], legacy_case["input_text"],
                                      legacy_case["expected_condition"], legacy_case["expected_keyword"],
                                      legacy_case["scoring_type"])
        case = next(item for item in db.get_all_test_cases() if item["id"] == case_id)
        self.assertEqual(case["name"], "Old format case")
        with patch.object(test_runner, "generate_response", return_value="ok"):
            result = test_runner.run_tests(self.prompt_a, "Answer: {input}", [case], max_workers=1)[0]
        db.update_test_case(case_id, "renamed", "keep me", "must contain ok", "ok",
                            "keyword_contains", name="new display name")
        historical = db.get_results_for_run(result["run_id"])[0]
        self.assertEqual(historical["test_case_id"], case_id)
        self.assertEqual(next(item for item in db.get_all_test_cases() if item["id"] == case_id)["name"], "new display name")

    def test_existing_schema_migrates_without_losing_ids_or_results(self):
        legacy_path = Path(self.temp.name) / "legacy.db"
        conn = db.sqlite3.connect(legacy_path)
        conn.executescript("""
            CREATE TABLE prompts (id INTEGER PRIMARY KEY, version_number INTEGER NOT NULL, prompt_text TEXT NOT NULL, commit_message TEXT, created_at TEXT NOT NULL);
            CREATE TABLE test_cases (id INTEGER PRIMARY KEY, description TEXT, input_text TEXT NOT NULL, expected_condition TEXT, expected_keyword TEXT, scoring_type TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE test_results (id INTEGER PRIMARY KEY, prompt_id INTEGER NOT NULL, test_case_id INTEGER NOT NULL, ai_response TEXT, passed INTEGER NOT NULL, score_reason TEXT, execution_time REAL, run_at TEXT NOT NULL, error_message TEXT);
            INSERT INTO prompts VALUES (41, 1, 'Answer {input}', 'old', '2025-01-01T00:00:00');
            INSERT INTO test_cases VALUES (77, 'Legacy name', 'input', 'contains ok', 'ok', 'keyword_contains', '2025-01-01T00:00:00');
            INSERT INTO test_results VALUES (91, 41, 77, 'ok', 1, 'matched', 0.1, '2025-01-02T00:00:00', NULL);
        """)
        conn.close()
        with patch.object(db, "DB_PATH", str(legacy_path)):
            db.init_db()
            case = db.get_all_test_cases()[0]
            historical = db.get_results_for_prompt(41)[0]
            suites = db.get_all_test_suites()
        self.assertEqual(case["id"], 77)
        self.assertEqual(case["name"], "Legacy name")
        self.assertEqual(case["enabled"], 1)
        self.assertEqual(historical["id"], 91)
        self.assertEqual(historical["test_case_id"], 77)
        self.assertEqual(suites, [])


if __name__ == "__main__":
    unittest.main()
