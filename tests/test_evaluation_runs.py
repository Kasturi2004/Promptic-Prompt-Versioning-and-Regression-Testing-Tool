"""Explicit-run comparison tests against isolated temporary SQLite databases."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, test_runner
from core.regression import compare_versions


class ExplicitRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "runs.db"))
        self.path_patch.start()
        db.init_db()
        self.prompt_a = db.create_prompt_version("A {input}", "A")
        self.prompt_b = db.create_prompt_version("B {input}", "B")

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def add_case(self, label):
        case_id = db.create_test_case(label, label, "contains ok", "ok", "keyword_contains")
        return db.get_all_test_cases()[-1]

    def execute(self, prompt_id, prompt_text, cases, response):
        with patch.object(test_runner, "generate_response", return_value=response):
            results = test_runner.run_tests(prompt_id, prompt_text, cases, max_workers=1)
        return results[0]["run_id"]

    def test_run_results_are_isolated_and_later_run_does_not_change_comparison(self):
        case = self.add_case("one")
        run_a = self.execute(self.prompt_a, "A {input}", [case], "ok")
        run_b = self.execute(self.prompt_b, "B {input}", [case], "bad")
        before = compare_versions(run_a, run_b)
        self.assertEqual(before[0]["status"], "REGRESSION")
        self.assertEqual(db.get_results_for_run(run_a)[0]["ai_response"], "ok")
        self.assertEqual(db.get_results_for_run(run_b)[0]["ai_response"], "bad")

        self.execute(self.prompt_b, "B {input}", [case], "ok")
        self.assertEqual(compare_versions(run_a, run_b), before)

    def test_test_order_does_not_affect_pairing(self):
        c1, c2 = self.add_case("one"), self.add_case("two")
        run_a = self.execute(self.prompt_a, "A {input}", [c1, c2], "ok")
        # c1 passes and c2 fails via provider output depending on input.
        def response(prompt):
            return "ok" if "one" in prompt else "bad"
        with patch.object(test_runner, "generate_response", side_effect=response):
            run_b = test_runner.run_tests(self.prompt_b, "B {input}", [c2, c1], max_workers=1)[0]["run_id"]
        paired = {entry["test_case_id"]: entry["status"] for entry in compare_versions(run_a, run_b)}
        self.assertEqual(paired, {c1["id"]: "UNCHANGED_PASS", c2["id"]: "REGRESSION"})

    def test_missing_result_is_explicit(self):
        case = self.add_case("one")
        run_a = self.execute(self.prompt_a, "A {input}", [case], "ok")
        # Candidate run snapshots the case but deliberately has no persisted result.
        run_b = db.create_evaluation_run(self.prompt_b, [case["id"]])
        self.assertEqual(compare_versions(run_a, run_b)[0]["status"], "MISSING_RESULT")

    def test_api_error_is_not_a_regression(self):
        case = self.add_case("one")
        run_a = self.execute(self.prompt_a, "A {input}", [case], "ok")
        with patch.object(test_runner, "generate_response", side_effect=test_runner.AIClientError("offline")):
            run_b = test_runner.run_tests(self.prompt_b, "B {input}", [case], max_workers=1)[0]["run_id"]
        self.assertEqual(compare_versions(run_a, run_b)[0]["status"], "ERROR")

    def test_run_id_must_exist(self):
        with self.assertRaises(ValueError):
            compare_versions("missing-a", "missing-b")


if __name__ == "__main__":
    unittest.main()
