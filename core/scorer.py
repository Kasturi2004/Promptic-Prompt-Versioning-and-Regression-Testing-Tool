"""Framework-neutral evaluator interface and deterministic scoring methods."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class EvaluationResult:
    status: str
    passed: bool | None
    reason: str
    expected: Any
    actual: str | None
    evaluation_method: str

    def to_dict(self) -> dict:
        return asdict(self)


class Evaluator(ABC):
    """Interface for adding deterministic or future externally-backed evaluators."""

    method: str

    @abstractmethod
    def validate(self, expected: Any) -> Any:
        """Validate and normalize an evaluator's expected value/configuration."""

    @abstractmethod
    def check(self, actual: str, expected: Any) -> tuple[bool, str]:
        """Return (passed, human-readable reason)."""


class ContainsKeyword(Evaluator):
    method = "contains_keyword"

    def validate(self, expected):
        if not isinstance(expected, str) or not expected:
            raise ValueError("contains_keyword expects a non-empty string.")
        return expected

    def check(self, actual, expected):
        passed = expected in actual
        reason = (f"Found expected keyword '{expected}'." if passed
                  else f"Expected keyword '{expected}' was not found.")
        return passed, reason


class ExactMatch(Evaluator):
    method = "exact_match"

    def validate(self, expected):
        if not isinstance(expected, str):
            raise ValueError("exact_match expects a string.")
        return expected

    def check(self, actual, expected):
        passed = actual.strip() == expected.strip()
        return passed, "Response matches expected text exactly." if passed else "Response does not exactly match expected text."


class ContainsAllKeywords(Evaluator):
    method = "contains_all_keywords"

    def validate(self, expected):
        expected = _decode_keyword_list(expected)
        if not isinstance(expected, list) or not expected or any(not isinstance(word, str) or not word for word in expected):
            raise ValueError("contains_all_keywords expects a non-empty list of non-empty strings.")
        return expected

    def check(self, actual, expected):
        missing = [word for word in expected if word not in actual]
        if missing:
            return False, "Expected keywords not found: " + ", ".join(repr(word) for word in missing) + "."
        return True, "All expected keywords were found."


class ContainsAnyKeyword(Evaluator):
    method = "contains_any_keyword"

    def validate(self, expected):
        expected = _decode_keyword_list(expected)
        if not isinstance(expected, list) or not expected or any(not isinstance(word, str) or not word for word in expected):
            raise ValueError("contains_any_keyword expects a non-empty list of non-empty strings.")
        return expected

    def check(self, actual, expected):
        found = [word for word in expected if word in actual]
        if found:
            return True, "Found at least one expected keyword: " + ", ".join(repr(word) for word in found) + "."
        return False, "None of the expected keywords were found."


class RegexMatch(Evaluator):
    method = "regex_match"

    def validate(self, expected):
        if not isinstance(expected, str) or not expected:
            raise ValueError("regex_match expects a non-empty pattern string.")
        try:
            re.compile(expected)
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc
        return expected

    def check(self, actual, expected):
        passed = re.search(expected, actual) is not None
        return passed, f"Regex '{expected}' matched." if passed else f"Regex '{expected}' did not match response."


class ResponseNotEmpty(Evaluator):
    method = "response_not_empty"

    def validate(self, expected):
        if expected not in (None, ""):
            raise ValueError("response_not_empty does not take an expected value.")
        return None

    def check(self, actual, expected):
        passed = bool(actual.strip())
        return passed, "Response is not empty." if passed else "Response is empty."


class KeywordNotContains(Evaluator):
    """Legacy scorer retained for stored test cases."""
    method = "keyword_not_contains"

    def validate(self, expected):
        if not isinstance(expected, str) or not expected:
            raise ValueError("keyword_not_contains expects a non-empty string.")
        return expected

    def check(self, actual, expected):
        passed = expected not in actual
        return passed, f"Response should not contain '{expected}'." if not passed else f"Correctly did not contain '{expected}'."


class CaseInsensitiveContains(Evaluator):
    """Legacy scorer retained for stored test cases."""
    method = "keyword_contains_ci"

    def validate(self, expected):
        if not isinstance(expected, str) or not expected:
            raise ValueError("keyword_contains_ci expects a non-empty string.")
        return expected

    def check(self, actual, expected):
        passed = expected.casefold() in actual.casefold()
        return passed, f"Expected to find '{expected}' (case-insensitive)." if not passed else f"Found '{expected}' (case-insensitive)."


class WordCount(Evaluator):
    def __init__(self, method, minimum):
        self.method = method
        self.minimum = minimum

    def validate(self, expected):
        try:
            count = int(expected)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.method} expects an integer.") from exc
        if isinstance(expected, bool) or count < 0:
            raise ValueError(f"{self.method} expects a non-negative integer.")
        return count

    def check(self, actual, expected):
        count = len(actual.split())
        passed = count >= expected if self.minimum else count <= expected
        relation = "at least" if self.minimum else "at most"
        return passed, f"Response has {count} words; expected {relation} {expected}."


EVALUATORS: dict[str, Evaluator] = {
    evaluator.method: evaluator for evaluator in (
        ContainsKeyword(), ExactMatch(), ContainsAllKeywords(), ContainsAnyKeyword(),
        RegexMatch(), ResponseNotEmpty(), KeywordNotContains(),
        CaseInsensitiveContains(), WordCount("min_word_count", True),
        WordCount("max_word_count", False),
    )
}
METHOD_ALIASES = {"keyword_contains": "contains_keyword"}


def _decode_keyword_list(expected):
    if isinstance(expected, str):
        try:
            parsed = json.loads(expected)
        except json.JSONDecodeError:
            # A comma-separated string is convenient for the current text-only form.
            parsed = [item.strip() for item in expected.split(",")]
        return parsed
    return expected


def normalize_method(method: str) -> str:
    if not isinstance(method, str):
        return method
    return METHOD_ALIASES.get(method, method)


def get_evaluator(method: str) -> Evaluator:
    normalized = normalize_method(method)
    try:
        return EVALUATORS[normalized]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown evaluation method: {method!r}.") from None


def normalize_expected(method: str, expected: Any) -> Any:
    evaluator = get_evaluator(method)
    return evaluator.validate(expected)


def evaluate_response(actual: str | None, expected: Any, evaluation_method: str) -> EvaluationResult:
    """Evaluate text and always return a structured result, including config errors."""
    normalized = normalize_method(evaluation_method)
    if not isinstance(normalized, str):
        normalized = repr(normalized)
    evaluator = EVALUATORS.get(normalized)
    if evaluator is None:
        return EvaluationResult("ERROR", None, f"Unknown evaluation method: {evaluation_method!r}.",
                                expected, actual, evaluation_method)
    try:
        normalized_expected = evaluator.validate(expected)
        if actual is None:
            return EvaluationResult("ERROR", None, "No response was available to evaluate.",
                                    normalized_expected, None, normalized)
        if not isinstance(actual, str):
            raise ValueError("Actual response must be a string.")
        passed, reason = evaluator.check(actual, normalized_expected)
        return EvaluationResult("PASS" if passed else "FAIL", passed, reason,
                                normalized_expected, actual, normalized)
    except Exception as exc:
        return EvaluationResult("ERROR", None, str(exc), expected, actual, normalized)


def validate_evaluation_config(expected: Any, evaluation_method: str) -> Any:
    """Raise on invalid case configuration; used before database writes/provider calls."""
    return normalize_expected(evaluation_method, expected)


def score_response(ai_response: str | None, expected_keyword: Any,
                   scoring_type: str) -> tuple[bool, str]:
    """Backward-compatible tuple wrapper for callers using the former scorer API."""
    result = evaluate_response(ai_response, expected_keyword, scoring_type)
    return result.passed is True, result.reason
