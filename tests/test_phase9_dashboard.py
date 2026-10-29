"""Phase 9 dashboard, persisted summary, and workflow context tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from ui.dashboard_helpers import build_dashboard_model, build_recent_activity
from ui.prompt_workspace import (
    consume_compare_candidate_context, consume_selection_context,
    consume_workspace_navigation, normalize_workspace_page, request_workspace_navigation,
)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase9.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_empty_dashboard_has_real_first_action_and_zero_metrics(self):
        snapshot = db.get_dashboard_snapshot()
        model = build_dashboard_model(snapshot)
        self.assertEqual(model["metrics"]["prompt_versions"], 0)
        self.assertEqual(model["metrics"]["evaluation_runs"], 0)
        self.assertIsNone(model["latest_run"])
        self.assertFalse(model["can_compare"])
        self.assertEqual(model["next_action"]["page"], "Prompt Workspace")

    def test_next_actions_follow_actual_project_state(self):
        prompt_id = db.create_prompt_version("Answer {input}", "baseline")
        case_id = db.create_test_case("Refund", "refund", "contains refund", "refund", "contains_keyword")
        snapshot = db.get_dashboard_snapshot()
        self.assertEqual(build_dashboard_model(snapshot)["next_action"]["page"], "Run Evaluation")

        run_id = db.create_evaluation_run(prompt_id, [case_id])
        db.save_test_result(prompt_id, case_id, "refund", True, "matched", 0.1,
                            run_id=run_id, status="PASS")
        db.finish_evaluation_run(run_id)
        snapshot = db.get_dashboard_snapshot()
        model = build_dashboard_model(snapshot)
        self.assertEqual(model["next_action"]["page"], "Results")
        self.assertEqual(model["latest_run"]["run_id"], run_id)
        self.assertEqual(model["latest_run"]["summary"]["passed"], 1)
        self.assertEqual(model["latest_run"]["summary"]["errors"], 0)
        self.assertFalse(model["can_compare"], "One latest run must not be treated as a comparison.")

        second_prompt = db.create_prompt_version("Reply briefly: {input}", "revision")
        second_run = db.create_evaluation_run(second_prompt, [case_id])
        db.save_test_result(second_prompt, case_id, "refund", True, "matched", 0.1,
                            run_id=second_run, status="PASS")
        db.finish_evaluation_run(second_run)
        model = build_dashboard_model(db.get_dashboard_snapshot())
        self.assertEqual(model["latest_run"]["run_id"], second_run)
        self.assertTrue(model["can_compare"])

    def test_disabled_cases_and_suites_are_counted_from_persisted_state(self):
        db.create_test_case("Off", "input", "", "value", "contains_keyword", enabled=False)
        db.create_test_suite("Disabled suite", enabled=False)
        metrics = build_dashboard_model(db.get_dashboard_snapshot())["metrics"]
        self.assertEqual(metrics["enabled_test_cases"], 0)
        self.assertEqual(metrics["test_suites"], 1)
        self.assertEqual(metrics["enabled_suites"], 0)

    def test_recent_activity_uses_existing_timestamped_records(self):
        events = build_recent_activity(
            [{"version_number": 2, "commit_message": "tone", "created_at": "2026-01-03"}],
            [{"id": 7, "name": "Refund", "created_at": "2026-01-01", "updated_at": "2026-01-02"}],
            [{"name": "Support", "created_at": "2026-01-01", "updated_at": "2026-01-01"}],
            [{"run_id": "r1", "status": "completed", "completed_at": "2026-01-04"}],
        )
        self.assertEqual([event["kind"] for event in events], [
            "Evaluation run completed", "Prompt version saved", "Test case updated", "Test suite created",
        ])
        self.assertEqual(events[0]["detail"], "r1")

    def test_dashboard_snapshot_is_lightweight_and_rejects_bad_limits(self):
        self.assertEqual(db.get_dashboard_snapshot(1)["recent_runs"], [])
        with self.assertRaisesRegex(ValueError, "positive integer"):
            db.get_dashboard_snapshot(0)

    def test_workflow_context_preserves_prompt_exact_run_and_candidate_not_baseline(self):
        state = {}
        request_workspace_navigation(state, "Run Evaluation", run_tests_prompt_id=31)
        self.assertEqual(consume_workspace_navigation(state, {"Run Evaluation"}), "Run Evaluation")
        self.assertEqual(consume_selection_context(state, "run_tests_prompt_id", "run_tests_version_select", [31]),
                         ("applied", 31))

        state["results_pending_run_id"] = "run-exact"
        self.assertEqual(consume_selection_context(state, "results_pending_run_id", "results_run_select", ["run-exact"]),
                         ("applied", "run-exact"))

        state["compare_candidate_pending_run_id"] = "candidate"
        state["compare_baseline_run"] = "previous-baseline"
        self.assertEqual(consume_compare_candidate_context(state, {"candidate", "baseline"}), "candidate")
        self.assertEqual(state["compare_candidate_run"], "candidate")
        self.assertIsNone(state["compare_baseline_run"])

        request_workspace_navigation(state, "Prompt Workspace", prompt_workspace_pending_version_id=31)
        self.assertEqual(consume_workspace_navigation(state, {"Prompt Workspace"}), "Prompt Workspace")
        self.assertEqual(state["prompt_workspace_pending_version_id"], 31)

    def test_old_navigation_names_resolve_to_current_sidebar_pages(self):
        aliases = {"Prompt Versions": "Prompt Workspace", "Run Tests": "Run Evaluation",
                   "Compare Versions": "Compare Runs"}
        self.assertEqual(normalize_workspace_page("Run Tests", aliases), "Run Evaluation")
        self.assertEqual(normalize_workspace_page("Results", aliases), "Results")


if __name__ == "__main__":
    unittest.main()
