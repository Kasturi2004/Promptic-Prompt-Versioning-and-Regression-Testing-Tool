"""Streamlit-independent test-case and test-suite presentation helpers."""

import yaml

from core import db
from core.scorer import EVALUATORS, normalize_method, normalize_expected


EVALUATOR_LABELS = {
    "contains_keyword": "Contains text",
    "keyword_contains": "Contains text (legacy)",
    "exact_match": "Exact match",
    "contains_all_keywords": "Contains all keywords",
    "contains_any_keyword": "Contains any keyword",
    "regex_match": "Regular expression",
    "response_not_empty": "Response is not empty",
    "keyword_not_contains": "Does not contain text (legacy)",
    "keyword_contains_ci": "Contains text (case-insensitive, legacy)",
    "min_word_count": "Minimum word count (legacy)",
    "max_word_count": "Maximum word count (legacy)",
}

EVALUATOR_ORDER = (
    "contains_keyword", "exact_match", "contains_all_keywords", "contains_any_keyword",
    "regex_match", "response_not_empty", "keyword_not_contains", "keyword_contains_ci",
    "min_word_count", "max_word_count", "keyword_contains",
)


def evaluator_options(current_method=None):
    options = [method for method in EVALUATOR_ORDER if method in EVALUATORS]
    if current_method and current_method not in options:
        options.append(current_method)
    return options


def evaluator_label(method):
    return EVALUATOR_LABELS.get(method, f"Unsupported legacy evaluator ({method})")


def is_keyword_list(method):
    return normalize_method(method) in {"contains_all_keywords", "contains_any_keyword"}


def is_word_count(method):
    return normalize_method(method) in {"min_word_count", "max_word_count"}


def has_expected_value(method):
    return normalize_method(method) != "response_not_empty"


def format_expected_config(method, stored_value):
    """Convert the existing DB representation into a small form-friendly value."""
    if not has_expected_value(method):
        return None
    try:
        value = normalize_expected(method, stored_value)
    except (TypeError, ValueError):
        value = stored_value
    if is_keyword_list(method):
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return "" if value is None else str(value)
    if value is None:
        return ""
    return str(value)


def _expected_from_form(method, raw_value):
    if not has_expected_value(method):
        return None
    if is_keyword_list(method):
        if isinstance(raw_value, list):
            return raw_value
        return [part.strip() for part in str(raw_value or "").split(",") if part.strip()]
    if is_word_count(method):
        try:
            return int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a whole number for the word-count limit.") from exc
    return str(raw_value or "").strip()


def validate_case_form(name, description, input_text, method, expected_value, enabled=True):
    """Validate fields using the real DB/evaluator validator and humanize errors."""
    try:
        expected = _expected_from_form(method, expected_value)
    except ValueError as exc:
        return False, str(exc), None
    case = {
        "name": name,
        "input_text": input_text,
        "expected_keyword": expected,
        "scoring_type": method,
        "enabled": enabled,
    }
    try:
        db.validate_test_case(case)
    except ValueError as exc:
        message = str(exc)
        if "name is required" in message:
            message = "Enter a name for this test case."
        elif "input_text is required" in message:
            message = "Enter the input that the prompt should receive."
        elif "Invalid regex" in message:
            message = "Your regular expression is invalid. Check its syntax."
        elif "non-empty list" in message:
            message = "Please provide at least one keyword, separated by commas."
        elif "Unknown evaluation method" in message:
            message = "Choose a supported evaluation method before saving."
        elif "expects an integer" in message or "non-negative integer" in message:
            message = "Enter a non-negative whole number for the word-count limit."
        elif "non-empty string" in message:
            message = "Provide an expected value for the selected evaluation method."
        elif "does not take an expected value" in message:
            message = "Response-is-not-empty does not need an expected value."
        elif "enabled must" in message:
            message = "Choose whether this test case is enabled."
        return False, message, None
    return True, "Test case configuration is valid.", case


def filter_test_cases(cases, state="All", query=""):
    query = (query or "").strip().casefold()
    filtered = []
    for case in cases:
        enabled = bool(case.get("enabled", True))
        if state == "Enabled" and not enabled:
            continue
        if state == "Disabled" and enabled:
            continue
        haystack = " ".join(str(case.get(key) or "") for key in ("name", "description")).casefold()
        if query and query not in haystack:
            continue
        filtered.append(case)
    return filtered


def case_label(case):
    name = case.get("name") or case.get("description") or f"Test case {case.get('id', '?')}"
    status = "Enabled" if bool(case.get("enabled", True)) else "Disabled"
    return f"{name} · {status} · {evaluator_label(case.get('scoring_type'))}"


def suite_counts(suite, members):
    enabled = sum(bool(case.get("enabled", True)) for case in members)
    return {"total": len(members), "enabled": enabled, "disabled": len(members) - enabled,
            "suite_enabled": bool(suite.get("enabled", True))}


def suite_label(suite, members):
    counts = suite_counts(suite, members)
    return (f"{suite.get('name') or 'Untitled suite'} · {counts['total']} case(s) · "
            f"{counts['enabled']} enabled · {'Enabled' if counts['suite_enabled'] else 'Disabled'}")


def execute_evaluation(mode, prompt_id, prompt_text, suite_id=None, enabled_cases=None,
                       progress_callback=None, suite_runner=None, all_tests_runner=None):
    """Route UI actions into the existing run_suite/run_tests engines."""
    if not prompt_id or not prompt_text:
        raise ValueError("Choose a saved prompt version before running an evaluation.")
    if mode == "suite":
        if not suite_id:
            raise ValueError("Choose a test suite before running it.")
        if suite_runner is None:
            from core.test_runner import run_suite as suite_runner
        return suite_runner(prompt_id, prompt_text, suite_id, progress_callback=progress_callback)
    if mode == "all":
        if all_tests_runner is None:
            from core.test_runner import run_tests as all_tests_runner
        cases = [case for case in (enabled_cases or []) if case.get("enabled", True)]
        if not cases:
            raise ValueError("There are no enabled test cases to run.")
        return all_tests_runner(prompt_id, prompt_text, cases, progress_callback=progress_callback)
    raise ValueError("Choose Run Suite or Run All Enabled Tests.")


def parse_import_cases(contents):
    try:
        data = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise ValueError("The YAML file is invalid. Check its indentation and values.") from exc
    if not isinstance(data, list):
        raise ValueError("The import file must contain a list of test cases.")
    prepared = []
    for index, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Test case {index} must be a mapping of fields and values.")
        description = raw.get("description", "")
        name = raw.get("name") or description or f"Test case {index}"
        method = raw.get("scoring_type", "keyword_contains")
        expected = raw.get("expected_keyword")
        case = {"name": name, "description": description,
                "input_text": raw.get("input_text"),
                "expected_condition": raw.get("expected_condition", ""),
                "expected_keyword": expected, "scoring_type": method,
                "enabled": raw.get("enabled", True)}
        valid, message, normalized = validate_case_form(
            case["name"], case["description"], case["input_text"], method, expected, case["enabled"])
        if not valid:
            raise ValueError(f"Test case {index} ({name}): {message}")
        case["expected_keyword"] = normalized["expected_keyword"]
        prepared.append(case)
    return prepared


def export_cases(cases):
    """Keep the current YAML field names and ordering for backward compatibility."""
    return [{
        "name": case.get("name") or case.get("description") or "Test case",
        "description": case.get("description"),
        "input_text": case.get("input_text"),
        "expected_condition": case.get("expected_condition"),
        "expected_keyword": case.get("expected_keyword"),
        "scoring_type": case.get("scoring_type"),
        "enabled": bool(case.get("enabled", True)),
    } for case in cases]
