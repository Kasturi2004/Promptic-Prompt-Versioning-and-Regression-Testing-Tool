"""Tests for the framework-neutral evaluator registry and result contract."""
import unittest

from core.scorer import evaluate_response, get_evaluator, score_response, validate_evaluation_config


class DeterministicEvaluatorTests(unittest.TestCase):
    def assert_evaluation(self, method, actual, expected, status):
        result = evaluate_response(actual, expected, method)
        self.assertEqual(result.status, status)
        self.assertEqual(result.passed, status == "PASS")
        self.assertEqual(result.evaluation_method, method)
        self.assertEqual(result.expected, expected)
        self.assertEqual(result.actual, actual)
        self.assertIsInstance(result.reason, str)
        self.assertTrue(result.reason)
        return result

    def test_contains_keyword_pass_and_fail(self):
        result = self.assert_evaluation("contains_keyword", "Refund is available", "refund", "FAIL")
        self.assertIn("refund", result.reason)
        self.assert_evaluation("contains_keyword", "A refund is available", "refund", "PASS")

    def test_exact_match_pass_and_fail(self):
        self.assert_evaluation("exact_match", "hello", "hello", "PASS")
        self.assert_evaluation("exact_match", "hello there", "hello", "FAIL")

    def test_contains_all_keywords_pass_and_fail(self):
        self.assert_evaluation("contains_all_keywords", "refund policy applies",
                               ["refund", "policy"], "PASS")
        result = self.assert_evaluation("contains_all_keywords", "refund available",
                                        ["refund", "policy"], "FAIL")
        self.assertIn("policy", result.reason)

    def test_contains_any_keyword_pass_and_fail(self):
        self.assert_evaluation("contains_any_keyword", "refund is available",
                               ["refund", "credit"], "PASS")
        self.assert_evaluation("contains_any_keyword", "case closed",
                               ["refund", "credit"], "FAIL")

    def test_regex_match_pass_and_fail(self):
        self.assert_evaluation("regex_match", "Your order #12345 shipped",
                               r"order #[0-9]+", "PASS")
        self.assert_evaluation("regex_match", "Your order #ABC shipped",
                               r"order #[0-9]+", "FAIL")

    def test_response_not_empty_pass_and_fail(self):
        self.assert_evaluation("response_not_empty", "done", None, "PASS")
        self.assert_evaluation("response_not_empty", "  ", None, "FAIL")

    def test_evaluator_interface_registry_and_structured_error(self):
        self.assertIsNotNone(get_evaluator("contains_keyword"))
        self.assertEqual(validate_evaluation_config('["refund", "policy"]', "contains_all_keywords"),
                         ["refund", "policy"])
        invalid = evaluate_response("text", "[", "regex_match")
        self.assertEqual(invalid.status, "ERROR")
        self.assertIsNone(invalid.passed)
        self.assertIn("Invalid regex", invalid.reason)
        self.assertEqual(evaluate_response(None, "x", "contains_keyword").status, "ERROR")

    def test_legacy_methods_remain_supported(self):
        self.assertTrue(score_response("refund policy", "refund", "keyword_contains")[0])
        self.assertTrue(score_response("Refund issued", "refund", "keyword_contains_ci")[0])
        self.assertTrue(score_response("safe response", "danger", "keyword_not_contains")[0])
        self.assertTrue(score_response("one two three", "2", "min_word_count")[0])
        self.assertTrue(score_response("one two", "2", "max_word_count")[0])

    def test_legacy_alias_reports_canonical_method(self):
        result = evaluate_response("a refund", "refund", "keyword_contains")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.evaluation_method, "contains_keyword")


if __name__ == "__main__":
    unittest.main()
