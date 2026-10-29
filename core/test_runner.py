"""Prompt rendering, provider execution, scoring, and durable run results."""

import hashlib
import json
import logging
import string
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from core import ai_client
from core.ai_client import AIClientError, generate_response
from core.scorer import evaluate_response
from core.db import (save_test_result, validate_test_case, create_evaluation_run,
                     finish_evaluation_run, get_prompt_by_id, get_test_suite,
                     get_test_cases_for_suite)

DEFAULT_MAX_CONCURRENT_REQUESTS = 3
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_TEMPERATURE = 0.2
_LOGGER = logging.getLogger(__name__)


def validate_prompt_template(prompt_template: str) -> set[str]:
    """Validate Python-format fields; the evaluator currently supports only {input}."""
    if not isinstance(prompt_template, str) or not prompt_template.strip():
        raise ValueError("Prompt text cannot be empty.")
    fields = set()
    try:
        for _, field_name, format_spec, conversion in string.Formatter().parse(prompt_template):
            if field_name is None:
                continue
            if not field_name:
                raise ValueError("Prompt template contains an unnamed variable; use {input}.")
            fields.add(field_name)
            if format_spec or conversion:
                raise ValueError("Prompt template variables cannot use format specs or conversions.")
    except ValueError as exc:
        if "Prompt template" in str(exc) or "unnamed variable" in str(exc):
            raise
        raise ValueError(f"Invalid prompt template: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Invalid prompt template: {exc}") from exc
    if "input" not in fields:
        raise ValueError("Prompt template is missing required variable {input}.")
    missing = fields - {"input"}
    if missing:
        raise ValueError("Unsupported or missing template variable(s): " + ", ".join(sorted(missing)))
    return fields


def build_final_prompt(prompt_template: str, input_text: str) -> tuple[str, str | None]:
    """Render a validated template and return (rendered_prompt, warning)."""
    validate_prompt_template(prompt_template)
    if not isinstance(input_text, str):
        raise ValueError("Test case input_text must be a string.")
    try:
        return prompt_template.format(input=input_text), None
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"Invalid prompt template: {exc}") from exc


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _run_single_test(prompt_id: int, run_id: str, prompt_text: str, test_case: dict,
                     prompt_hash: str) -> dict:
    started_at = _utc_now()
    started = time.monotonic()
    ai_response = None
    error_message = None
    error_type = None
    evaluation = None
    metadata = {
        "provider": "gemini", "model": ai_client._MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE, "timeout_seconds": DEFAULT_REQUEST_TIMEOUT,
        "prompt_sha256": prompt_hash,
    }
    try:
        final_prompt, _ = build_final_prompt(prompt_text, test_case["input_text"])
        response = generate_response(final_prompt)
        ai_response = str(response)  # retain the unmodified provider text
        metadata.update(getattr(response, "metadata", {}) or {})
        evaluation = evaluate_response(
            ai_response, test_case["expected_keyword"], test_case["scoring_type"]
        )
        status_bool = evaluation.passed is True
        status = evaluation.status
        score_reason = evaluation.reason
    except AIClientError as exc:
        error_type = exc.error_type
        error_message = str(exc)
        metadata["attempts"] = exc.attempts
        metadata["error_type"] = error_type
        status_bool = False
        status = "ERROR"
        score_reason = "Evaluation could not complete because the AI provider failed."
        evaluation = evaluate_response(None, test_case.get("expected_keyword"), test_case.get("scoring_type"))
    except Exception as exc:
        error_type = "evaluation_error"
        error_message = f"Evaluation failed: {exc}"
        metadata["error_type"] = error_type
        status_bool = False
        status = "ERROR"
        score_reason = "Evaluation could not complete because an execution error occurred."
        evaluation = evaluate_response(None, test_case.get("expected_keyword"), test_case.get("scoring_type"))

    finished_at = _utc_now()
    execution_time = round(time.monotonic() - started, 3)
    metadata.update({"started_at": started_at, "finished_at": finished_at,
                     "execution_time_seconds": execution_time})
    evaluation_data = evaluation.to_dict()
    if status == "ERROR" and evaluation_data["reason"] == "No response was available to evaluate.":
        evaluation_data["reason"] = score_reason
    metadata["evaluation"] = evaluation_data
    result = {
        "test_case_id": test_case["id"], "prompt_id": prompt_id,
        "description": test_case.get("description", ""),
        "ai_response": ai_response, "raw_response": ai_response,
        "passed": status_bool, "status": status, "score_reason": score_reason,
        "expected": evaluation_data["expected"], "actual": ai_response,
        "evaluation_method": evaluation_data["evaluation_method"],
        "evaluation": evaluation_data,
        "execution_time": execution_time, "error_message": error_message,
        "error_type": error_type, "evaluation_timestamp": finished_at,
        "metadata": metadata, "run_id": run_id,
    }
    save_test_result(
        prompt_id=prompt_id, test_case_id=test_case["id"],
        ai_response=ai_response, passed=status_bool, score_reason=score_reason,
        execution_time=execution_time, error_message=error_message,
        run_id=run_id, status=status, metadata=metadata,
    )
    return result


def run_tests(prompt_id: int, prompt_text: str, test_cases: list[dict],
              max_workers: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
              progress_callback=None, suite_id=None) -> list[dict]:
    """Evaluate each case independently and return structured, persisted results."""
    if not isinstance(prompt_id, int) or isinstance(prompt_id, bool) or prompt_id <= 0:
        raise ValueError("A valid prompt version is required.")
    validate_prompt_template(prompt_text)
    if not isinstance(test_cases, list):
        raise ValueError("test_cases must be a list.")
    if any(not isinstance(case, dict) for case in test_cases):
        raise ValueError("Each test case must be an object.")
    if not isinstance(max_workers, int) or isinstance(max_workers, bool) or not 1 <= max_workers <= 5:
        raise ValueError("max_workers must be between 1 and 5.")
    saved_prompt = get_prompt_by_id(prompt_id)
    if not saved_prompt:
        raise ValueError("Prompt version does not exist.")
    if saved_prompt["prompt_text"] != prompt_text:
        raise ValueError("Prompt text does not match the selected saved prompt version.")

    # Keep callers' dictionaries intact and omit disabled cases from ordinary runs.
    test_cases = [dict(case) for case in test_cases if case.get("enabled", True)]
    for case in test_cases:
        # Older callers and test-case exports did not include a separate name.
        if not case.get("name"):
            case["name"] = case.get("description") or f"Test case {case.get('id', '')}".strip()
        validate_test_case(case)
        if not isinstance(case.get("id"), int) or isinstance(case.get("id"), bool) or case["id"] <= 0:
            raise ValueError("Each test case must have a valid database id.")
    case_ids = [case["id"] for case in test_cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("Evaluation suite contains duplicate test case IDs.")

    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    run_metadata = {
        "prompt_text": prompt_text, "prompt_sha256": prompt_hash,
        "prompt_version_id": prompt_id, "provider": "gemini",
        "model": ai_client._MODEL_NAME, "temperature": DEFAULT_TEMPERATURE,
        "timeout_seconds": DEFAULT_REQUEST_TIMEOUT, "max_workers": max_workers,
        "suite_id": suite_id, "started_at": _utc_now(),
        "test_cases": [{key: case.get(key) for key in (
            "id", "name", "description", "input_text", "expected_condition",
            "expected_keyword", "scoring_type", "enabled")} for case in test_cases],
    }
    run_id = create_evaluation_run(prompt_id, case_ids, run_metadata, suite_id=suite_id)
    if not test_cases:
        finish_evaluation_run(run_id, "completed")
        return []

    results_by_id = {}
    fatal_execution_error = None
    fatal_persistence_error = False
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_single_test, prompt_id, run_id, prompt_text, case, prompt_hash): case
                for case in test_cases
            }
            for future in as_completed(futures):
                case = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    # Isolate an unexpected single-case failure and persist it as ERROR.
                    timestamp = _utc_now()
                    error_reason = "Evaluation could not complete because an execution error occurred."
                    evaluation = evaluate_response(None, case.get("expected_keyword"), case.get("scoring_type")).to_dict()
                    if evaluation["reason"] == "No response was available to evaluate.":
                        evaluation["reason"] = error_reason
                    metadata = {"provider": "gemini", "model": ai_client._MODEL_NAME,
                                "error_type": "evaluation_error", "prompt_sha256": prompt_hash,
                                "started_at": timestamp, "finished_at": timestamp,
                                "evaluation": evaluation}
                    result = {
                        "test_case_id": case["id"], "prompt_id": prompt_id,
                        "description": case.get("description", ""),
                        "ai_response": None, "raw_response": None, "passed": False,
                        "status": "ERROR", "score_reason": error_reason,
                        "expected": evaluation["expected"], "actual": None,
                        "evaluation_method": evaluation["evaluation_method"], "evaluation": evaluation,
                        "execution_time": 0, "error_message": f"Evaluation failed: {exc}",
                        "error_type": "evaluation_error", "evaluation_timestamp": timestamp,
                        "metadata": metadata,
                        "run_id": run_id,
                    }
                    try:
                        save_test_result(prompt_id, case["id"], None, False, error_reason,
                                         0, result["error_message"], run_id=run_id,
                                         status="ERROR", metadata=metadata)
                    except Exception:
                        fatal_persistence_error = True
                        _LOGGER.exception("Could not persist evaluation error result for case %s", case["id"])
                results_by_id[case["id"]] = result
                if progress_callback:
                    try:
                        progress_callback(len(results_by_id), len(test_cases))
                    except Exception:
                        _LOGGER.exception("Evaluation progress callback failed")
    except Exception as exc:
        fatal_execution_error = exc
    finally:
        all_cases_accounted_for = set(results_by_id) == set(case_ids)
        final_status = "failed" if fatal_execution_error or fatal_persistence_error or not all_cases_accounted_for else None
        finish_evaluation_run(run_id, final_status)

    if fatal_execution_error:
        raise fatal_execution_error

    return [results_by_id[case_id] for case_id in case_ids]


def run_suite(prompt_id: int, prompt_text: str, suite_id: int,
              max_workers: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
              progress_callback=None) -> list[dict]:
    """Run the enabled members of a stored suite, preserving their database IDs."""
    suite = get_test_suite(suite_id)
    if suite is None:
        raise ValueError("Test suite does not exist.")
    if not suite["enabled"]:
        raise ValueError("Test suite is disabled and cannot be evaluated.")
    cases = get_test_cases_for_suite(suite_id, enabled_only=True)
    return run_tests(prompt_id, prompt_text, cases, max_workers=max_workers,
                     progress_callback=progress_callback, suite_id=suite_id)
