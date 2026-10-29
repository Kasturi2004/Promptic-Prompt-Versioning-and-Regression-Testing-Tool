"""Prompt workspace validation, versioning, and navigation-context tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import db
from ui.prompt_workspace import (
    consume_compare_candidate_context, consume_workspace_navigation,
    consume_selection_context, find_prompt_version, is_prompt_dirty, prompt_version_label,
    prompt_version_options, request_workspace_navigation,
    save_new_prompt_version, validate_template_for_editor,
)


class PromptWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "phase7.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_template_validation_reuses_core_semantics_with_human_messages(self):
        valid, message = validate_template_for_editor("Reply to {input}. Literal braces: {{ok}}")
        self.assertTrue(valid)
        self.assertIn("valid", message)

        valid, message = validate_template_for_editor("No variable here")
        self.assertFalse(valid)
        self.assertIn("missing the required {input}", message)

        valid, message = validate_template_for_editor("Hello {input}; also {name}")
        self.assertFalse(valid)
        self.assertIn("currently supports {input}", message)

        valid, message = validate_template_for_editor("Broken {input")
        self.assertFalse(valid)
        self.assertIn("malformed braces", message)

    def test_new_version_saves_note_and_does_not_mutate_historical_version(self):
        first_id = save_new_prompt_version("  First {input}  ", "Initial version")
        original = db.get_prompt_by_id(first_id)
        second_id = save_new_prompt_version("Second {input}", "Improved answer style", source_prompt=original)
        updated = db.get_prompt_by_id(second_id)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(updated["commit_message"], "Improved answer style")
        self.assertEqual(updated["prompt_text"], "Second {input}")
        self.assertEqual(db.get_prompt_by_id(first_id)["prompt_text"], "First {input}")
        self.assertEqual(db.get_prompt_by_id(first_id)["commit_message"], "Initial version")
        self.assertEqual(prompt_version_label(updated), "Version 2 · Improved answer style")
        self.assertEqual([p["version_number"] for p in db.get_all_prompts()], [2, 1])

    def test_version_options_and_selection_use_human_context_not_database_order(self):
        first = save_new_prompt_version("First {input}", "first")
        second = save_new_prompt_version("Second {input}", "second", source_prompt=db.get_prompt_by_id(first))
        prompts = db.get_all_prompts()
        self.assertEqual(prompt_version_options(prompts), [None, second, first])
        self.assertEqual(find_prompt_version(prompts, first)["prompt_text"], "First {input}")
        self.assertIsNone(find_prompt_version(prompts, 999))
        self.assertEqual(prompt_version_options([]), [None])

    def test_invalid_or_duplicate_prompt_cannot_create_a_version(self):
        saver = Mock(wraps=db.create_prompt_version)
        with self.assertRaisesRegex(ValueError, "missing the required"):
            save_new_prompt_version("No input variable", "note", saver=saver)
        with self.assertRaisesRegex(ValueError, "version note"):
            save_new_prompt_version("Hello {input}", "   ", saver=saver)
        source_id = save_new_prompt_version("Hello {input}", "first", saver=saver)
        source = db.get_prompt_by_id(source_id)
        with self.assertRaisesRegex(ValueError, "No prompt changes"):
            save_new_prompt_version("Hello {input}", "another note", source_prompt=source, saver=saver)
        self.assertEqual(saver.call_count, 1)

    def test_dirty_state_is_simple_and_per_draft(self):
        self.assertFalse(is_prompt_dirty("Prompt {input}", "Prompt {input}"))
        self.assertTrue(is_prompt_dirty("Changed {input}", "Prompt {input}"))
        self.assertTrue(is_prompt_dirty("Prompt {input}", "Prompt {input}", "note"))
        self.assertFalse(is_prompt_dirty("", "", is_new=True))
        self.assertTrue(is_prompt_dirty("Draft {input}", "", is_new=True))

    def test_navigation_carries_prompt_version_and_run_context(self):
        state = {}
        request_workspace_navigation(state, "Run Tests", run_tests_prompt_id=42)
        self.assertEqual(state["prompt_workspace_navigation_target"], "Run Tests")
        self.assertEqual(state["run_tests_prompt_id"], 42)

        request_workspace_navigation(state, "Results", results_pending_run_id="run-abc")
        self.assertEqual(state["prompt_workspace_navigation_target"], "Results")
        self.assertEqual(state["results_pending_run_id"], "run-abc")
        self.assertEqual(consume_workspace_navigation(state, {"Results", "Run Tests"}), "Results")
        self.assertIsNone(consume_workspace_navigation(state, {"Results", "Run Tests"}))

    def test_evaluation_and_results_selection_context_targets_exact_ids(self):
        state = {"run_tests_prompt_id": 24}
        self.assertEqual(consume_selection_context(state, "run_tests_prompt_id", "run_tests_version_select", [23, 24]),
                         ("applied", 24))
        self.assertEqual(state["run_tests_version_select"], 24)
        state["results_pending_run_id"] = "run-exact"
        self.assertEqual(consume_selection_context(state, "results_pending_run_id", "results_run_select", ["run-exact"]),
                         ("applied", "run-exact"))
        self.assertEqual(state["results_run_select"], "run-exact")
        state["results_pending_run_id"] = "missing"
        self.assertEqual(consume_selection_context(state, "results_pending_run_id", "results_run_select", ["run-exact"])[0],
                         "unavailable")

    def test_workspace_compare_context_sets_candidate_but_never_selects_baseline(self):
        state = {"compare_candidate_pending_run_id": "candidate-1", "compare_baseline_run": "old-baseline"}
        selected = consume_compare_candidate_context(state, {"baseline-1", "candidate-1"})
        self.assertEqual(selected, "candidate-1")
        self.assertIsNone(state["compare_baseline_run"])
        self.assertEqual(state["compare_candidate_run"], "candidate-1")
        stale = {"compare_candidate_pending_run_id": "deleted-run"}
        self.assertEqual(consume_compare_candidate_context(stale, {"available-run"}), "unavailable")
        self.assertIsNone(stale["compare_baseline_run"])
        self.assertIsNone(stale["compare_candidate_run"])


if __name__ == "__main__":
    unittest.main()
