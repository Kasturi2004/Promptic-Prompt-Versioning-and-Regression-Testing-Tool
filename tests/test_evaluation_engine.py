"""Core evaluation-engine and provider failure tests; no live API calls."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import ai_client, db, test_runner


class EvaluationEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "engine.db"))
        self.db_patch.start()
        db.init_db()
        self.prompt_id = db.create_prompt_version("Summarize: {input}", "baseline")
        self.case_id = db.create_test_case("contains ok", "hello", "contains ok", "ok", "keyword_contains")
        self.case = db.get_all_test_cases()[0]

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_prompt_templates_require_only_input_and_render_escaped_braces(self):
        rendered, warning = test_runner.build_final_prompt("JSON {{value}}: {input}", "hello")
        self.assertEqual(rendered, "JSON {value}: hello")
        self.assertIsNone(warning)
        for template in ("No placeholder", "{input} {context}", "{input", "{}"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                test_runner.validate_prompt_template(template)

    def test_pass_stores_raw_response_status_timestamps_and_reproducibility_metadata(self):
        raw = "  ok\nwith original spacing  "
        with patch.object(test_runner, "generate_response", return_value=raw):
            result = test_runner.run_tests(self.prompt_id, "Summarize: {input}", [self.case], max_workers=1)[0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["raw_response"], raw)
        stored = db.get_results_for_run(result["run_id"])[0]
        self.assertEqual(stored["ai_response"], raw)
        self.assertEqual(stored["status"], "PASS")
        self.assertTrue(stored["run_at"])
        self.assertEqual(stored["prompt_id"], self.prompt_id)
        self.assertEqual(stored["test_case_id"], self.case_id)
        self.assertEqual(stored["metadata"]["provider"], "gemini")
        run = db.get_evaluation_run(result["run_id"])
        self.assertEqual(run["metadata"]["prompt_text"], "Summarize: {input}")
        self.assertEqual(run["metadata"]["test_cases"][0]["input_text"], "hello")

    def test_quality_failure_and_provider_error_are_distinct_and_independent(self):
        case2_id = db.create_test_case("also contains ok", "second", "contains ok", "ok", "keyword_contains")
        case2 = db.get_all_test_cases()[1]

        def provider(prompt):
            if "hello" in prompt:
                raise ai_client.AIClientError("offline", "timeout", 2)
            return "does not match"

        with patch.object(test_runner, "generate_response", side_effect=provider):
            results = test_runner.run_tests(self.prompt_id, "Summarize: {input}", [self.case, case2], max_workers=1)
        self.assertEqual([r["status"] for r in results], ["ERROR", "FAIL"])
        self.assertEqual(results[0]["error_type"], "timeout")
        stored = {r["test_case_id"]: r for r in db.get_results_for_run(results[0]["run_id"])}
        self.assertEqual(stored[self.case_id]["status"], "ERROR")
        self.assertEqual(stored[case2_id]["status"], "FAIL")
        self.assertEqual(db.get_evaluation_run(results[0]["run_id"])["status"], "completed_with_errors")

    def test_invalid_template_prompt_mismatch_and_duplicate_case_rejected_before_run(self):
        with patch.object(test_runner, "generate_response") as provider:
            with self.assertRaisesRegex(ValueError, "missing required"):
                test_runner.run_tests(self.prompt_id, "No input", [self.case])
            with self.assertRaisesRegex(ValueError, "does not match"):
                test_runner.run_tests(self.prompt_id, "Different {input}", [self.case])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                test_runner.run_tests(self.prompt_id, "Summarize: {input}", [self.case, self.case])
        provider.assert_not_called()

    def test_duplicate_result_for_same_run_case_is_rejected(self):
        with patch.object(test_runner, "generate_response", return_value="ok"):
            result = test_runner.run_tests(self.prompt_id, "Summarize: {input}", [self.case], max_workers=1)[0]
        stored = db.get_results_for_run(result["run_id"])[0]
        with self.assertRaises(Exception):
            db.save_test_result(self.prompt_id, self.case_id, "again", True, "duplicate", 0,
                                 run_id=result["run_id"], status="PASS")
        self.assertEqual(len(db.get_results_for_run(result["run_id"])), 1)

    def test_list_expected_values_survive_sqlite_round_trip_and_return_structured_evaluation(self):
        case_id = db.create_test_case("refund policy", "refund request", "both keywords",
                                      ["refund", "policy"], "contains_all_keywords")
        case = next(item for item in db.get_all_test_cases() if item["id"] == case_id)
        with patch.object(test_runner, "generate_response", return_value="refund follows the policy"):
            result = test_runner.run_tests(self.prompt_id, "Summarize: {input}", [case], max_workers=1)[0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["expected"], ["refund", "policy"])
        self.assertEqual(result["evaluation_method"], "contains_all_keywords")
        self.assertEqual(result["evaluation"]["actual"], "refund follows the policy")

class GeminiClientTests(unittest.TestCase):
    def _fake_sdk(self, model):
        return SimpleNamespace(
            configure=lambda **kwargs: None,
            GenerativeModel=lambda name: model,
            types=SimpleNamespace(GenerationConfig=lambda **kwargs: kwargs),
        )

    def test_timeout_and_rate_limit_are_retried_then_classified(self):
        class DeadlineExceeded(Exception):
            code = 504

        for failure, kind in ((TimeoutError("socket timed out"), "timeout"),
                              (DeadlineExceeded("deadline"), "timeout"),
                              (RuntimeError("429 rate limit"), "rate_limit")):
            model = SimpleNamespace(generate_content=unittest.mock.Mock(side_effect=[failure, failure]))
            with self.subTest(kind=kind), patch.object(ai_client, "_API_KEY", "test-key"), \
                    patch.object(ai_client, "_configured", True), patch.object(ai_client, "genai", self._fake_sdk(model)), \
                    patch.object(ai_client.time, "sleep"):
                with self.assertRaises(ai_client.AIClientError) as caught:
                    ai_client.generate_response("prompt", max_retries=1)
                self.assertEqual(caught.exception.error_type, kind)
                self.assertEqual(caught.exception.attempts, 2)
                self.assertEqual(model.generate_content.call_count, 2)

    def test_empty_provider_response_is_classified_without_retry(self):
        model = SimpleNamespace(generate_content=unittest.mock.Mock(return_value=SimpleNamespace(text="  ")))
        with patch.object(ai_client, "_API_KEY", "test-key"), patch.object(ai_client, "_configured", True), \
                patch.object(ai_client, "genai", self._fake_sdk(model)):
            with self.assertRaises(ai_client.AIClientError) as caught:
                ai_client.generate_response("prompt", max_retries=3)
        self.assertEqual(caught.exception.error_type, "empty_response")
        self.assertEqual(model.generate_content.call_count, 1)

    def test_provider_response_remains_text_and_contains_attempt_metadata(self):
        model = SimpleNamespace(generate_content=unittest.mock.Mock(return_value=SimpleNamespace(text=" response ")))
        with patch.object(ai_client, "_API_KEY", "test-key"), patch.object(ai_client, "_configured", True), \
                patch.object(ai_client, "genai", self._fake_sdk(model)):
            response = ai_client.generate_response("prompt")
        self.assertIsInstance(response, str)
        self.assertEqual(response, " response ")
        self.assertEqual(response.metadata["provider"], "gemini")
        self.assertEqual(response.metadata["attempts"], 1)
        request_kwargs = model.generate_content.call_args.kwargs
        self.assertEqual(request_kwargs["request_options"]["timeout"], 30.0)


if __name__ == "__main__":
    unittest.main()
