"""Phase 8 UI-independent test-case management behavior."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from core import db
from ui import test_case_helpers as helpers
from ui.prompt_workspace import consume_selection_context, request_workspace_navigation


class TestManagementHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase8.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_friendly_evaluator_options_and_form_validation(self):
        options = helpers.evaluator_options()
        self.assertEqual(options[:6], ["contains_keyword", "exact_match", "contains_all_keywords",
                                      "contains_any_keyword", "regex_match", "response_not_empty"])
        self.assertEqual(helpers.evaluator_label("contains_all_keywords"), "Contains all keywords")
        self.assertIn("keyword_contains", helpers.evaluator_options("keyword_contains"))
        self.assertTrue(helpers.validate_case_form("name", "", "input", "contains_all_keywords",
                                                   "refund, policy")[0])
        valid, message, _ = helpers.validate_case_form("name", "", "input", "regex_match", "[")
        self.assertFalse(valid)
        self.assertIn("regular expression", message)
        valid, _, _ = helpers.validate_case_form("name", "", "input", "contains_any_keyword", " , ")
        self.assertFalse(valid)

    def test_filters_and_suite_counts_include_disabled_without_running_them(self):
        cases = [
            {"id": 1, "name": "Refund policy", "description": "card", "enabled": 1},
            {"id": 2, "name": "Other", "description": "", "enabled": 0},
        ]
        self.assertEqual([c["id"] for c in helpers.filter_test_cases(cases, "Enabled")], [1])
        self.assertEqual([c["id"] for c in helpers.filter_test_cases(cases, "Disabled")], [2])
        self.assertEqual([c["id"] for c in helpers.filter_test_cases(cases, query="REFUND")], [1])
        counts = helpers.suite_counts({"enabled": 1}, cases)
        self.assertEqual(counts, {"total": 2, "enabled": 1, "disabled": 1, "suite_enabled": True})

    def test_evaluation_actions_route_to_existing_runners_and_only_enabled_cases(self):
        suite_runner = Mock(return_value=[{"run_id": "suite-run"}])
        all_runner = Mock(return_value=[{"run_id": "all-run"}])
        progress = Mock()
        cases = [{"id": 1, "enabled": 1}, {"id": 2, "enabled": 0}]
        result = helpers.execute_evaluation("suite", 5, "Prompt {input}", suite_id=9,
                                            progress_callback=progress, suite_runner=suite_runner)
        self.assertEqual(result[0]["run_id"], "suite-run")
        suite_runner.assert_called_once_with(5, "Prompt {input}", 9, progress_callback=progress)
        helpers.execute_evaluation("all", 5, "Prompt {input}", enabled_cases=cases,
                                   progress_callback=progress, all_tests_runner=all_runner)
        all_runner.assert_called_once_with(5, "Prompt {input}", [cases[0]], progress_callback=progress)
        with self.assertRaisesRegex(ValueError, "enabled test cases"):
            helpers.execute_evaluation("all", 5, "Prompt {input}", enabled_cases=[cases[1]])
        with self.assertRaisesRegex(ValueError, "saved prompt"):
            helpers.execute_evaluation("suite", None, "Prompt", suite_id=9, suite_runner=suite_runner)

    def test_legacy_yaml_round_trip_keeps_import_fields_and_method(self):
        source = """\n- description: Old format\n  input_text: Refund please\n  expected_condition: response includes keyword\n  expected_keyword: refund\n  scoring_type: keyword_contains\n"""
        prepared = helpers.parse_import_cases(source)
        self.assertEqual(prepared[0]["scoring_type"], "keyword_contains")
        ids = db.create_test_cases_bulk(prepared)
        stored = db.get_all_test_cases()
        self.assertEqual(ids, [stored[0]["id"]])
        exported = helpers.export_cases(stored)
        self.assertEqual(list(exported[0]), ["name", "description", "input_text", "expected_condition",
                                             "expected_keyword", "scoring_type", "enabled"])
        self.assertEqual(exported[0]["scoring_type"], "keyword_contains")
        self.assertEqual(yaml.safe_load(yaml.safe_dump(exported, sort_keys=False)), exported)

    def test_invalid_yaml_import_is_rejected_before_any_rows_are_inserted(self):
        with self.assertRaisesRegex(ValueError, "Test case 2"):
            prepared = helpers.parse_import_cases("""\n- name: valid\n  input_text: hi\n  expected_keyword: ok\n  scoring_type: contains_keyword\n- name: bad regex\n  input_text: hi\n  expected_keyword: '['\n  scoring_type: regex_match\n""")
            db.create_test_cases_bulk(prepared)
        self.assertEqual(db.get_all_test_cases(), [])
        with self.assertRaisesRegex(ValueError, "list of test cases"):
            helpers.parse_import_cases("not: a list")

    def test_bulk_import_validates_then_inserts_all_rows(self):
        rows = helpers.parse_import_cases("""\n- name: first\n  input_text: hello\n  expected_keyword: hi\n  scoring_type: contains_keyword\n- name: second\n  input_text: world\n  expected_keyword: [world, earth]\n  scoring_type: contains_all_keywords\n""")
        ids = db.create_test_cases_bulk(rows)
        self.assertEqual(len(ids), 2)
        self.assertEqual([case["name"] for case in db.get_all_test_cases()], ["first", "second"])

    def test_suite_details_update_keeps_id_and_existing_run_snapshot(self):
        prompt_id = db.create_prompt_version("Answer {input}", "baseline")
        case_id = db.create_test_case("Refund", "refund", "contains refund", "refund", "contains_keyword")
        suite_id = db.create_test_suite("Original suite", "Before edit", [case_id])
        run_id = db.create_evaluation_run(prompt_id, [case_id], suite_id=suite_id)
        db.update_test_suite(suite_id, "Renamed suite", "After edit")
        suite = db.get_test_suite(suite_id)
        run = db.get_evaluation_run(run_id)
        self.assertEqual(suite["id"], suite_id)
        self.assertEqual(suite["name"], "Renamed suite")
        self.assertEqual(suite["description"], "After edit")
        self.assertEqual(run["metadata"]["suite_snapshot"]["name"], "Original suite")
        with self.assertRaisesRegex(ValueError, "name is required"):
            db.update_test_suite(suite_id, "  ")

    def test_prompt_context_survives_workspace_navigation_to_test_cases(self):
        state = {}
        request_workspace_navigation(state, "Test Cases", test_cases_prompt_id=17)
        self.assertEqual(consume_selection_context(state, "test_cases_prompt_id",
                                                  "test_cases_evaluation_prompt_id", [17]),
                         ("applied", 17))
        self.assertEqual(state["test_cases_evaluation_prompt_id"], 17)


if __name__ == "__main__":
    unittest.main()
