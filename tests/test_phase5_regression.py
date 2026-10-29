"""Phase 5 regression comparison tests using temporary SQLite and no provider calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.regression import compare_runs, compare_versions, summarize_comparison


class RegressionComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase5.db"))
        self.db_patch.start()
        db.init_db()
        self.prompt_a = db.create_prompt_version("A {input}", "A")
        self.prompt_b = db.create_prompt_version("B {input}", "B")
        self.case_a = db.create_test_case("Case A", "input-a", "contains ok", "ok", "contains_keyword")
        self.case_b = db.create_test_case("Case B", "input-b", "contains ok", "ok", "contains_keyword")
        self.case_c = db.create_test_case("Case C", "input-c", "contains ok", "ok", "contains_keyword")

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def run_with(self, prompt, pairs, suite_id=None):
        ids = [case_id for case_id, _ in pairs]
        run_id = db.create_evaluation_run(prompt, ids, metadata={"test_cases": [
            {"id": cid, "name": f"Snapshot {cid}", "description": f"Snapshot {cid}", "input_text": f"input-{cid}"}
            for cid in ids]}, suite_id=suite_id)
        for case_id, status in pairs:
            db.save_test_result(prompt, case_id, f"response-{status}", status == "PASS", status,
                                0.01, run_id=run_id, status=status,
                                metadata={"evaluation": {"expected": "ok", "evaluation_method": "contains_keyword"}})
        db.finish_evaluation_run(run_id)
        return run_id

    def test_all_pass_all_pass_and_unchanged_fail(self):
        first = self.run_with(self.prompt_a, [(self.case_a, "PASS"), (self.case_b, "FAIL")])
        second = self.run_with(self.prompt_b, [(self.case_b, "FAIL"), (self.case_a, "PASS")])
        result = compare_runs(first, second)
        self.assertEqual(result.overall_status, "UNCHANGED")
        self.assertEqual((result.unchanged, result.regressions, result.improvements), (2, 0, 0))
        self.assertEqual([x.test_case_id for x in result.tests], sorted([self.case_a, self.case_b]))

    def test_regression_and_improvement(self):
        baseline = self.run_with(self.prompt_a, [(self.case_a, "PASS"), (self.case_b, "FAIL")])
        regressed = self.run_with(self.prompt_b, [(self.case_a, "FAIL"), (self.case_b, "FAIL")])
        result = compare_runs(baseline, regressed)
        self.assertEqual(result.regressions, 1)
        self.assertEqual(result.overall_status, "WORSE")
        improved = self.run_with(self.prompt_b, [(self.case_a, "PASS"), (self.case_b, "PASS")])
        result = compare_runs(regressed, improved)
        self.assertEqual(result.improvements, 2)
        self.assertEqual(result.overall_status, "BETTER")

    def test_errors_are_non_comparable_and_never_quality_regressions(self):
        for left, right in (("PASS", "ERROR"), ("FAIL", "ERROR"), ("ERROR", "PASS"),
                            ("ERROR", "FAIL"), ("ERROR", "ERROR")):
            with self.subTest(left=left, right=right):
                a = self.run_with(self.prompt_a, [(self.case_a, left)])
                b = self.run_with(self.prompt_b, [(self.case_a, right)])
                item = compare_runs(a, b).tests[0]
                self.assertEqual(item.classification, "ERROR")
                self.assertEqual(compare_runs(a, b).regressions, 0)
                self.assertEqual(compare_runs(a, b).improvements, 0)

    def test_added_removed_and_suite_identity_are_explicit(self):
        suite1 = db.create_test_suite("one", "", [self.case_a])
        suite2 = db.create_test_suite("two", "", [self.case_b])
        a = self.run_with(self.prompt_a, [(self.case_a, "PASS")], suite1)
        b = self.run_with(self.prompt_b, [(self.case_b, "PASS")], suite2)
        result = compare_runs(a, b)
        self.assertEqual(result.suite_compatibility, "different_suites_union")
        self.assertEqual((result.added, result.removed), (1, 1))
        self.assertEqual({t.classification for t in result.tests}, {"ADDED", "REMOVED"})
        self.assertEqual(result.overall_status, "INCONCLUSIVE")

    def test_missing_result_and_empty_runs_are_explicit(self):
        a = self.run_with(self.prompt_a, [(self.case_a, "PASS")])
        b = db.create_evaluation_run(self.prompt_b, [self.case_a])
        result = compare_runs(a, b)
        self.assertEqual(result.tests[0].classification, "MISSING_RESULT")
        self.assertEqual(result.errors, 1)
        empty_a = db.create_evaluation_run(self.prompt_a, [])
        empty_b = db.create_evaluation_run(self.prompt_b, [])
        empty = compare_runs(empty_a, empty_b)
        self.assertEqual(empty.overall_status, "INCONCLUSIVE")
        self.assertIsNone(empty.pass_rate_delta)

    def test_pass_rates_delta_denominator_and_run_metadata(self):
        a = self.run_with(self.prompt_a, [(self.case_a, "PASS"), (self.case_b, "ERROR"), (self.case_c, "FAIL")])
        b = self.run_with(self.prompt_b, [(self.case_a, "PASS"), (self.case_b, "PASS"), (self.case_c, "FAIL")])
        result = compare_runs(a, b)
        self.assertEqual(result.baseline_prompt_version_id, self.prompt_a)
        self.assertEqual(result.candidate_prompt_version_id, self.prompt_b)
        self.assertEqual(result.baseline_run_id, a)
        self.assertEqual(result.candidate_run_id, b)
        self.assertEqual(result.baseline_pass_rate, 33.33)
        self.assertEqual(result.candidate_pass_rate, 66.67)
        self.assertEqual(result.pass_rate_delta, 33.34)
        self.assertEqual(result.pass_rate_delta_unit, "percentage_points")

    def test_case_matching_survives_live_rename_and_runs_same_prompt_are_distinct(self):
        first = self.run_with(self.prompt_a, [(self.case_a, "PASS")])
        second = self.run_with(self.prompt_a, [(self.case_a, "FAIL")])
        db.update_test_case(self.case_a, "Renamed", "changed", "contains ok", "ok", "contains_keyword")
        result = compare_runs(first, second)
        self.assertEqual(result.tests[0].test_case_id, self.case_a)
        self.assertEqual(result.tests[0].classification, "REGRESSION")
        self.assertEqual(result.tests[0].description, f"Snapshot {self.case_a}")

    def test_validation_and_legacy_wrapper(self):
        with self.assertRaisesRegex(ValueError, "Baseline evaluation run"):
            compare_runs("missing", "also-missing")
        a = self.run_with(self.prompt_a, [(self.case_a, "PASS")])
        with self.assertRaisesRegex(ValueError, "Candidate evaluation run"):
            compare_runs(a, "missing")
        with self.assertRaisesRegex(ValueError, "distinct"):
            compare_runs(a, a)
        b = self.run_with(self.prompt_b, [(self.case_a, "PASS")])
        legacy = compare_versions(a, b)
        self.assertEqual(legacy[0]["status"], "UNCHANGED_PASS")
        self.assertEqual(summarize_comparison(legacy)["unchanged"], 1)
        self.assertEqual(summarize_comparison(compare_runs(a, b))["overall_status"], "UNCHANGED")


if __name__ == "__main__":
    unittest.main()
