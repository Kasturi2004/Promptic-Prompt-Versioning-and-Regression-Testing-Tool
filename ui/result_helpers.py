"""Small, Streamlit-independent helpers for run-based result pages."""

from datetime import datetime
import csv
import io
import re

from core.db import get_evaluation_run, get_results_for_run
from core.regression import compare_runs


def run_label(run, prompts_by_id, suites_by_id=None):
    prompt = prompts_by_id.get(run.get("prompt_id"), {})
    version = prompt.get("version_number")
    prompt_label = f"Prompt v{version}" if version is not None else f"Prompt #{run.get('prompt_id', '?')}"
    suite_id = run.get("suite_id")
    suite = (suites_by_id or {}).get(suite_id, {}) if suite_id is not None else {}
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    suite_snapshot = metadata.get("suite_snapshot")
    suite_snapshot = suite_snapshot if isinstance(suite_snapshot, dict) else {}
    suite_name = suite.get("name") or suite_snapshot.get("name") or (f"Suite #{suite_id}" if suite_id else "No suite")
    timestamp = format_timestamp(run.get("started_at") or run.get("created_at"))
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    rate = summary.get("pass_rate")
    rate_label = f"{rate:.2f}%" if isinstance(rate, (int, float)) else "N/A"
    status = format_run_status(run.get("status"))
    short_id = str(run.get("run_id", ""))[:8] or "unknown"
    return f"{prompt_label} · {suite_name} · {rate_label} · {status} · {timestamp} · {short_id}"


def format_timestamp(value):
    if not value:
        return "Time unavailable"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        local = parsed.astimezone()
        return f"{local.strftime('%b')} {local.day}, {local.strftime('%Y %I:%M %p')}"
    except (AttributeError, TypeError, ValueError):
        return str(value)


def format_run_status(status):
    return {
        "completed": "Completed",
        "completed_with_errors": "Completed with errors",
        "failed": "Failed",
        "partial": "Partially completed",
        "running": "Running",
    }.get(status, "Status unavailable" if not status else str(status).replace("_", " ").title())


def run_summary(run):
    """Use the persisted Phase 4 run summary and preserve PASS/FAIL/ERROR counts."""
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    return {
        "total": summary.get("total", run.get("total_tests", 0)) or 0,
        "passed": summary.get("passed", run.get("passed_count", 0)) or 0,
        "failed": summary.get("failed", run.get("failed_count", 0)) or 0,
        "errors": summary.get("errors", run.get("error_count", 0)) or 0,
        "pass_rate": summary.get("pass_rate", run.get("pass_rate")),
        "status": run.get("status"),
    }


def load_selected_run(run_id, run_loader=None, results_loader=None):
    """Load only records belonging to the selected run; reject stale IDs."""
    if not run_id:
        raise ValueError("Select an evaluation run.")
    run = (run_loader or get_evaluation_run)(run_id)
    if run is None:
        raise ValueError("That evaluation run is no longer available.")
    results = (results_loader or get_results_for_run)(run_id)
    if any(result.get("run_id") != run_id for result in results):
        raise ValueError("Stored results do not match the selected evaluation run.")
    return run, results


def snapshot_map(run):
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    snapshots = metadata.get("test_cases") or []
    return {case["id"]: case for case in snapshots if isinstance(case, dict) and "id" in case}


def export_run_results_csv(run, results):
    """Serialize a run and its test outcomes as a spreadsheet-friendly CSV."""
    output = io.StringIO(newline="")
    fields = [
        "run_id", "run_status", "prompt_id", "suite_id", "started_at",
        "completed_at", "test_case_id", "test_name", "status", "input",
        "expected", "actual", "evaluation_method", "evaluation_reason",
        "response", "error", "execution_time_seconds",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    snapshots = snapshot_map(run)
    for result in results:
        snapshot = snapshots.get(result.get("test_case_id"), {})
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        evaluation = result.get("evaluation") or metadata.get("evaluation") or {}
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        writer.writerow({
            "run_id": run.get("run_id"),
            "run_status": run.get("status"),
            "prompt_id": run.get("prompt_id"),
            "suite_id": run.get("suite_id"),
            "started_at": run.get("started_at") or run.get("created_at"),
            "completed_at": run.get("completed_at"),
            "test_case_id": result.get("test_case_id"),
            "test_name": snapshot.get("name") or snapshot.get("description") or result.get("description"),
            "status": result_status(result),
            "input": snapshot.get("input_text", result.get("input_text")),
            "expected": evaluation.get("expected", result.get("expected", snapshot.get("expected_keyword"))),
            "actual": evaluation.get("actual", result.get("actual")),
            "evaluation_method": evaluation.get("evaluation_method", result.get("evaluation_method", snapshot.get("scoring_type"))),
            "evaluation_reason": evaluation.get("reason") or result.get("score_reason"),
            "response": result.get("ai_response"),
            "error": safe_error_message(result.get("error_message")) if result.get("error_message") else None,
            "execution_time_seconds": result.get("execution_time"),
        })
    return output.getvalue()


def result_status(result):
    status = result.get("status")
    if status in {"PASS", "FAIL", "ERROR"}:
        return status
    # Legacy run rows can predate explicit status values.
    return "ERROR" if result.get("ai_response") is None else "PASS" if result.get("passed") else "FAIL"


def filter_results(results, selection):
    if selection == "All":
        return list(results)
    wanted = {"Failed": "FAIL", "Errors": "ERROR", "Passed": "PASS"}.get(selection, selection.upper())
    return [result for result in results if result_status(result) == wanted]


def safe_error_message(message):
    """Remove common credential/header-shaped fragments from provider errors."""
    if not message:
        return "Evaluation could not complete because the provider or evaluation engine returned an error."
    cleaned = str(message)
    cleaned = re.sub(r"(?i)authorization(?:\s+header)?\s*[:=]\s*(?:bearer\s+)?[^\s,;]+", "authorization=[redacted]", cleaned)
    cleaned = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", cleaned)
    cleaned = re.sub(r"(?i)\bapi\s+key\s+is\s+[^\s,;]+", "API key=[redacted]", cleaned)
    cleaned = re.sub(r"(?i)(api[_ -]?key)\s*[:= ]\s*[^\s,;]+", r"\1=[redacted]", cleaned)
    cleaned = re.sub(r"(?i)([?&]key=)[^\s&;,]+", r"\1[redacted]", cleaned)
    cleaned = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted]", cleaned)
    return cleaned[:1000]


def compare_selected_runs(baseline_run_id, candidate_run_id, compare_fn=None):
    """Validate explicit selections and pass their IDs directly to Phase 5."""
    if not baseline_run_id or not candidate_run_id:
        raise ValueError("Select both a baseline run and a candidate run.")
    if baseline_run_id == candidate_run_id:
        raise ValueError("Choose two different runs to compare.")
    return (compare_fn or compare_runs)(baseline_run_id, candidate_run_id)


def comparison_interpretation(comparison):
    status = comparison.overall_status
    return {
        "BETTER": "Candidate improved overall.",
        "WORSE": "Candidate regressed overall.",
        "UNCHANGED": "No meaningful quality change detected.",
        "INCONCLUSIVE": "Comparison is inconclusive because runs contain errors, differing test membership, or no comparable tests.",
    }.get(status, "Comparison outcome is unavailable.")


def comparison_groups(comparison):
    groups = {key: [] for key in ("REGRESSION", "IMPROVEMENT", "UNCHANGED", "ERROR", "ADDED", "REMOVED")}
    for test in comparison.tests:
        classification = test.classification
        groups["ERROR" if classification == "MISSING_RESULT" else classification].append(test)
    return groups
