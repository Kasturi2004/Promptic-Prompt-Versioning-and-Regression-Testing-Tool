"""Explicit, test-case-ID based comparison of persisted evaluation runs."""

from dataclasses import asdict, dataclass

from core.db import get_evaluation_run, get_results_for_run


@dataclass(frozen=True)
class TestComparison:
    test_case_id: int
    description: str
    classification: str
    baseline_status: str | None
    candidate_status: str | None
    baseline_response: str | None
    candidate_response: str | None
    baseline_expected: object = None
    candidate_expected: object = None
    baseline_evaluation_method: str | None = None
    candidate_evaluation_method: str | None = None
    baseline_reason: str | None = None
    candidate_reason: str | None = None
    input_text: str | None = None
    baseline_metadata: dict | None = None
    candidate_metadata: dict | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ComparisonResult:
    baseline_run_id: str
    candidate_run_id: str
    baseline_prompt_version_id: int
    candidate_prompt_version_id: int
    baseline_suite_id: int | None
    candidate_suite_id: int | None
    suite_compatibility: str
    total_comparable_tests: int
    regressions: int
    improvements: int
    unchanged: int
    errors: int
    added: int
    removed: int
    baseline_pass_rate: float | None
    candidate_pass_rate: float | None
    pass_rate_delta: float | None
    pass_rate_delta_unit: str
    overall_status: str
    tests: tuple[TestComparison, ...]

    def to_dict(self):
        value = asdict(self)
        value["tests"] = [test.to_dict() for test in self.tests]
        return value


def _evaluation(result):
    metadata = result.get("metadata") or {}
    evaluation = result.get("evaluation") or metadata.get("evaluation") or {}
    return evaluation if isinstance(evaluation, dict) else {}


def _snapshot_map(run):
    snapshots = run.get("metadata", {}).get("test_cases", [])
    return {item.get("id"): item for item in snapshots if isinstance(item, dict)}


def _pass_rate(run, results):
    """Match Phase 4 semantics: PASS count / planned case count, in percent."""
    total = len(run.get("test_case_ids", []))
    passed = sum(result.get("status") == "PASS" for result in results)
    if not total:
        return None
    return round((passed / total) * 100, 2)


def compare_runs(baseline_run_id: str, candidate_run_id: str) -> ComparisonResult:
    """Compare two exact persisted runs; never infer a baseline.

    Suite differences are compared over the union of their snapshotted case IDs;
    candidate-only and baseline-only IDs are exposed as ADDED and REMOVED.
    ERROR and absent result rows are non-comparable execution states.
    """
    if not baseline_run_id or not candidate_run_id:
        raise ValueError("Baseline and candidate run IDs are required.")
    if baseline_run_id == candidate_run_id:
        raise ValueError("Baseline and candidate must be distinct evaluation runs.")
    baseline = get_evaluation_run(baseline_run_id)
    candidate = get_evaluation_run(candidate_run_id)
    if baseline is None:
        raise ValueError(f"Baseline evaluation run '{baseline_run_id}' does not exist.")
    if candidate is None:
        raise ValueError(f"Candidate evaluation run '{candidate_run_id}' does not exist.")

    left = {r["test_case_id"]: r for r in get_results_for_run(baseline_run_id)}
    right = {r["test_case_id"]: r for r in get_results_for_run(candidate_run_id)}
    left_ids, right_ids = set(baseline["test_case_ids"]), set(candidate["test_case_ids"])
    left_snapshots, right_snapshots = _snapshot_map(baseline), _snapshot_map(candidate)
    tests = []
    for case_id in sorted(left_ids | right_ids):
        a, b = left.get(case_id), right.get(case_id)
        snapshot = left_snapshots.get(case_id) or right_snapshots.get(case_id) or {}
        # Snapshots preserve the name/description at run time even if the live case is edited.
        description = snapshot.get("description") or snapshot.get("name") or f"Test case {case_id}"
        if case_id not in left_ids:
            classification = "ADDED"
        elif case_id not in right_ids:
            classification = "REMOVED"
        elif a is None or b is None or a.get("status") == "ERROR" or b.get("status") == "ERROR":
            classification = "ERROR" if a is not None and b is not None else "MISSING_RESULT"
        elif a.get("status") == "PASS" and b.get("status") == "FAIL":
            classification = "REGRESSION"
        elif a.get("status") == "FAIL" and b.get("status") == "PASS":
            classification = "IMPROVEMENT"
        elif a.get("status") in {"PASS", "FAIL"} and b.get("status") == a.get("status"):
            classification = "UNCHANGED"
        else:
            classification = "ERROR"
        ae, be = _evaluation(a) if a else {}, _evaluation(b) if b else {}
        tests.append(TestComparison(
            test_case_id=case_id, description=description, classification=classification,
            baseline_status=a.get("status") if a else ("NOT_RUN" if case_id not in left_ids else "MISSING_RESULT"),
            candidate_status=b.get("status") if b else ("NOT_RUN" if case_id not in right_ids else "MISSING_RESULT"),
            baseline_response=a.get("ai_response") if a else None,
            candidate_response=b.get("ai_response") if b else None,
            baseline_expected=ae.get("expected"), candidate_expected=be.get("expected"),
            baseline_evaluation_method=ae.get("evaluation_method"),
            candidate_evaluation_method=be.get("evaluation_method"),
            baseline_reason=a.get("score_reason") if a else None,
            candidate_reason=b.get("score_reason") if b else None,
            input_text=snapshot.get("input_text"),
            baseline_metadata=a.get("metadata") if a else None,
            candidate_metadata=b.get("metadata") if b else None,
        ))

    counts = {key: sum(t.classification == label for t in tests) for key, label in {
        "regressions": "REGRESSION", "improvements": "IMPROVEMENT", "unchanged": "UNCHANGED",
        "errors": "ERROR", "added": "ADDED", "removed": "REMOVED",
    }.items()}
    counts["errors"] += sum(t.classification == "MISSING_RESULT" for t in tests)
    comparable = counts["regressions"] + counts["improvements"] + counts["unchanged"]
    has_gaps = counts["errors"] + counts["added"] + counts["removed"] > 0
    if counts["regressions"] and not counts["improvements"] and not has_gaps:
        overall = "WORSE"
    elif counts["improvements"] and not counts["regressions"] and not has_gaps:
        overall = "BETTER"
    elif comparable and not counts["regressions"] and not counts["improvements"] and not has_gaps:
        overall = "UNCHANGED"
    else:
        overall = "INCONCLUSIVE"
    baseline_rate, candidate_rate = _pass_rate(baseline, list(left.values())), _pass_rate(candidate, list(right.values()))
    delta = round(candidate_rate - baseline_rate, 2) if baseline_rate is not None and candidate_rate is not None else None
    return ComparisonResult(
        baseline_run_id=baseline_run_id, candidate_run_id=candidate_run_id,
        baseline_prompt_version_id=baseline["prompt_id"], candidate_prompt_version_id=candidate["prompt_id"],
        baseline_suite_id=baseline.get("suite_id"), candidate_suite_id=candidate.get("suite_id"),
        suite_compatibility="same_suite" if baseline.get("suite_id") == candidate.get("suite_id") else "different_suites_union",
        total_comparable_tests=comparable, **counts,
        baseline_pass_rate=baseline_rate, candidate_pass_rate=candidate_rate,
        pass_rate_delta=delta, pass_rate_delta_unit="percentage_points",
        overall_status=overall, tests=tuple(tests),
    )


def compare_versions(baseline_run_id: str, candidate_run_id: str) -> list[dict]:
    """Legacy list API backed by explicit-run comparison.

    Keeps existing callers working while exposing the richer fields and preserving
    historical status spellings (UNCHANGED_PASS/UNCHANGED_FAIL, MISSING_RESULT).
    """
    result = compare_runs(baseline_run_id, candidate_run_id)
    rows = []
    for test in result.tests:
        row = test.to_dict()
        row.update({"a_passed": test.baseline_status == "PASS" if test.baseline_status in {"PASS", "FAIL"} else None,
                    "a_response": test.baseline_response, "a_reason": test.baseline_reason,
                    "b_passed": test.candidate_status == "PASS" if test.candidate_status in {"PASS", "FAIL"} else None,
                    "b_response": test.candidate_response, "b_reason": test.candidate_reason})
        if test.classification == "UNCHANGED":
            row["status"] = "UNCHANGED_PASS" if test.baseline_status == "PASS" else "UNCHANGED_FAIL"
        elif test.classification == "MISSING_RESULT":
            row["status"] = "MISSING_RESULT"
        else:
            row["status"] = test.classification
        rows.append(row)
    return rows


def summarize_comparison(comparison) -> dict:
    """Summarize either the structured result or the legacy comparison list."""
    if isinstance(comparison, ComparisonResult):
        return {key: getattr(comparison, key) for key in (
            "total_comparable_tests", "regressions", "improvements", "unchanged", "errors",
            "added", "removed", "baseline_pass_rate", "candidate_pass_rate", "pass_rate_delta",
            "pass_rate_delta_unit", "overall_status")}
    counts = {"regressions": 0, "improvements": 0, "unchanged_pass": 0,
              "unchanged_fail": 0, "errors": 0, "missing_results": 0,
              "added": 0, "removed": 0}
    mapping = {"REGRESSION": "regressions", "IMPROVEMENT": "improvements",
               "UNCHANGED_PASS": "unchanged_pass", "UNCHANGED_FAIL": "unchanged_fail",
               "ERROR": "errors", "MISSING_RESULT": "missing_results",
               "ADDED": "added", "REMOVED": "removed"}
    for entry in comparison:
        key = mapping.get(entry.get("status"))
        if key:
            counts[key] += 1
    counts["unchanged"] = counts["unchanged_pass"] + counts["unchanged_fail"]
    counts["total_comparable_tests"] = counts["regressions"] + counts["improvements"] + counts["unchanged"]
    return counts


def calculate_pass_rate(prompt_id: int) -> float:
    """Deprecated compatibility helper; latest-result metrics are not comparisons."""
    from core.db import get_latest_result_per_test_case
    results = get_latest_result_per_test_case(prompt_id)
    if not results:
        return None
    return round((sum(bool(r["passed"]) for r in results) / len(results)) * 100, 1)
