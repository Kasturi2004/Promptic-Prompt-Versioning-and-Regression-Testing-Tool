"""Run-centered evaluation results and test-level diagnostics."""

import logging

import streamlit as st

from core.db import (
    get_all_prompts, get_all_test_suites, get_evaluation_runs_for_prompt,
    get_results_for_prompt,
)
from core.test_runner import retry_incomplete_cases
from ui.result_helpers import (
    filter_results, format_run_status, format_timestamp, load_selected_run,
    result_status, run_label, run_summary, safe_error_message, snapshot_map,
)
from ui.prompt_workspace import consume_selection_context
from ui.prompt_workspace import request_workspace_navigation

_LOGGER = logging.getLogger(__name__)


def _load_runs(prompts):
    runs = []
    for prompt in prompts:
        runs.extend(get_evaluation_runs_for_prompt(prompt["id"]))
    return sorted(runs, key=lambda run: (run.get("started_at") or run.get("created_at") or "", run.get("run_id", "")), reverse=True)


def _render_run_metadata(run):
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    st.write("**Run ID:**", run.get("run_id", "Unavailable"))
    st.write("**Prompt version ID:**", run.get("prompt_id", "Unavailable"))
    st.write("**Suite ID:**", run.get("suite_id") or "No suite")
    st.write("**Provider:**", metadata.get("provider", "Unavailable"))
    st.write("**Model:**", metadata.get("model", "Unavailable"))
    st.write("**Prompt SHA-256:**", metadata.get("prompt_sha256", "Unavailable"))
    st.write("**Concurrency:**", metadata.get("max_workers", "Unavailable"))
    st.write("**Started:**", format_timestamp(run.get("started_at") or run.get("created_at")))
    st.write("**Completed:**", format_timestamp(run.get("completed_at")) if run.get("completed_at") else "Not completed")
    # The immutable prompt snapshot is useful when debugging historical runs.
    prompt_text = metadata.get("prompt_text")
    if prompt_text:
        st.markdown("**Evaluated prompt text**")
        st.code(prompt_text, language="text")


def _render_test_result(result, snapshot):
    status = result_status(result)
    name = snapshot.get("name") or snapshot.get("description") or result.get("description") or f"Test case {result.get('test_case_id')}"
    st_name = f"{name} · #{result.get('test_case_id', '?')} · {status}"
    with st.expander(st_name, expanded=status in {"FAIL", "ERROR"}):
        st.caption(f"Status: {status}")
        if snapshot.get("input_text") is not None:
            st.markdown("**Input**")
            st.code(snapshot["input_text"], language="text")

        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        evaluation = result.get("evaluation") or metadata.get("evaluation") or {}
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        expected = evaluation.get("expected", result.get("expected", snapshot.get("expected_keyword")))
        actual = evaluation.get("actual", result.get("actual"))
        method = evaluation.get("evaluation_method", result.get("evaluation_method", snapshot.get("scoring_type")))
        if expected is not None:
            st.write("**Expected:**", expected if isinstance(expected, (str, int, float, list, dict)) else str(expected))
        if actual is not None:
            st.write("**Evaluation actual:**", actual)
        if method:
            st.write("**Evaluation method:**", method)
        reason = evaluation.get("reason") or result.get("score_reason")
        if reason:
            st.write("**Evaluation:**", reason)

        response = result.get("ai_response")
        if response is not None:
            st.markdown("**Generated response**")
            st.code(response, language="text")
        elif status == "ERROR":
            st.info("No response was available because this test could not be evaluated.")

        if status == "ERROR":
            st.warning("This is an execution/evaluation error, not evidence that the prompt failed this test.")
            st.write("**Error:**", safe_error_message(result.get("error_message")))
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            if metadata.get("attempts") is not None:
                st.write("**Provider attempts:**", metadata["attempts"])
            if metadata.get("error_type"):
                st.write("**Error category:**", metadata["error_type"])

        try:
            duration = float(result.get("execution_time") or 0)
            st.write("**Execution time:**", f"{duration:.3f}s")
        except (TypeError, ValueError):
            st.write("**Execution time:**", "Unavailable")
        if result.get("run_at"):
            st.caption(f"Evaluated: {format_timestamp(result['run_at'])}")
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        safe_metadata = {key: metadata[key] for key in (
            "provider", "model", "temperature", "timeout_seconds", "attempts",
            "started_at", "finished_at", "execution_time_seconds", "prompt_sha256", "error_type",
        ) if key in metadata}
        if safe_metadata:
            st.markdown("**Execution details**")
            st.json(safe_metadata)


def _render_legacy_results(prompts):
    """Keep pre-run result rows visible without pretending they belong to a run."""
    if not prompts:
        return
    with st.expander("Historical results without evaluation run records"):
        labels = [f"Prompt v{prompt['version_number']} · {prompt.get('commit_message') or 'Untitled'}" for prompt in prompts]
        index = st.selectbox("Historical prompt version", range(len(prompts)), format_func=lambda i: labels[i], key="legacy_results_prompt")
        prompt = prompts[index]
        try:
            historical = [row for row in get_results_for_prompt(prompt["id"]) if row.get("run_id") is None]
        except Exception:
            _LOGGER.exception("Unable to load legacy PromptLab results")
            st.error("Could not load historical results. Check database availability.")
            return
        if not historical:
            st.info("No legacy result rows exist for this prompt version.")
            return
        st.info("These records predate explicit evaluation runs. They remain viewable but cannot be compared as a run.")
        for row in historical:
            status = result_status(row)
            st.markdown(f"**{row.get('description') or 'Test case'} · #{row.get('test_case_id')} · {status}**")
            st.write("**Input:**", row.get("input_text") or "Unavailable")
            st.write("**Expected:**", row.get("expected_keyword") or row.get("expected_condition") or "Unavailable")
            st.write("**Evaluation:**", row.get("score_reason") or "Unavailable")
            st.code(row.get("ai_response") or "(no response)", language="text")
            st.divider()


def render():
    st.title("Results")
    st.caption("Review a specific evaluation run, its outcomes, and the responses it produced.")

    try:
        prompts = get_all_prompts()
        runs = _load_runs(prompts)
        suites = get_all_test_suites()
    except Exception:
        _LOGGER.exception("Unable to load PromptLab evaluation runs")
        st.error("Could not load evaluation runs. Check database availability and try again.")
        return

    if not runs:
        st.info("No evaluation runs yet. Run an evaluation to see results here.")
        if st.button("Run Evaluation", key="results_empty_run_evaluation"):
            request_workspace_navigation(st.session_state, "Run Evaluation")
            st.rerun()
        _render_legacy_results(prompts)
        return

    prompts_by_id = {prompt["id"]: prompt for prompt in prompts}
    suites_by_id = {suite["id"]: suite for suite in suites}
    run_ids = [run["run_id"] for run in runs]
    run_by_id = {run["run_id"]: run for run in runs}
    context_status, _ = consume_selection_context(
        st.session_state, "results_pending_run_id", "results_run_select", run_ids,
    )
    if context_status == "unavailable":
        st.error("The selected version's evaluation run is no longer available. Choose a run explicitly from the selector.")
        return
    selected_run_id = st.selectbox(
        "Evaluation run", run_ids,
        format_func=lambda run_id: run_label(run_by_id[run_id], prompts_by_id, suites_by_id),
        key="results_run_select",
    )

    try:
        run, results = load_selected_run(selected_run_id)
    except Exception:
        _LOGGER.exception("Unable to load results for evaluation run %s", selected_run_id)
        st.error("Could not load this run's results. It may have been deleted or its stored data may be malformed.")
        return

    prompt = prompts_by_id.get(run.get("prompt_id"), {})
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    suite_snapshot = metadata.get("suite_snapshot")
    suite_snapshot = suite_snapshot if isinstance(suite_snapshot, dict) else {}
    suite = suites_by_id.get(run.get("suite_id"), {}) if run.get("suite_id") else {}
    suite_name = suite.get("name") or suite_snapshot.get("name") or (f"Suite #{run['suite_id']}" if run.get("suite_id") else "No suite")
    summary = run_summary(run)

    finalized_runs = [item for item in runs if item.get("status") != "running"]
    snapshot_ids = set(run.get("test_case_ids", []))
    retryable_count = sum(1 for item in results if result_status(item) == "ERROR")
    retryable_count += len(snapshot_ids - {item.get("test_case_id") for item in results})
    if retryable_count:
        st.warning(
            f"{retryable_count} case(s) need another attempt. Retry creates a new run and keeps this run's results for reference.",
            icon="⚠️",
        )
    action_cols = st.columns(4)
    can_compare_run = len(finalized_runs) >= 2 and run.get("status") != "running"
    if action_cols[0].button("Compare this run", disabled=not can_compare_run,
                             key=f"results_compare_run_{run['run_id']}"):
        st.session_state["compare_candidate_pending_run_id"] = run["run_id"]
        request_workspace_navigation(st.session_state, "Compare Runs")
        st.rerun()
    if action_cols[1].button("Improve Prompt", disabled=not prompt,
                             key=f"results_improve_prompt_{run['run_id']}"):
        st.session_state["prompt_workspace_pending_version_id"] = prompt["id"]
        request_workspace_navigation(st.session_state, "Prompt Workspace")
        st.rerun()
    if action_cols[2].button(f"Retry {retryable_count} case(s)", type="primary",
                             disabled=retryable_count == 0, use_container_width=True,
                             key=f"results_retry_run_{run['run_id']}"):
        try:
            retry_results = retry_incomplete_cases(run["run_id"])
            if retry_results:
                new_run_id = retry_results[0]["run_id"]
                st.success(f"Retry run created for {len(retry_results)} errored or unrecorded case(s).")
                st.session_state["results_pending_run_id"] = new_run_id
                st.rerun()
            else:
                st.info("This run has no errored or unrecorded cases to retry.")
        except Exception:
            _LOGGER.exception("Could not retry incomplete evaluation run %s", run["run_id"])
            st.error("Could not retry this run. Check its saved prompt and case snapshots, then review the application logs.")
    action_cols[3].caption("Compare Runs requires two finalized runs. The baseline must be selected explicitly.")

    st.subheader(f"Prompt v{prompt.get('version_number', '?')} · {prompt.get('commit_message') or 'Untitled version'}")
    st.caption(f"Suite: {suite_name} · Run status: {format_run_status(run.get('status'))}")
    if summary["pass_rate"] is None:
        st.metric("Pass rate", "N/A")
    else:
        st.metric("Pass rate", f"{summary['pass_rate']:.2f}%", help="Passed tests divided by all planned tests; errors remain in the denominator.")
    metric_cols = st.columns(4)
    metric_cols[0].metric("Total tests", summary["total"])
    metric_cols[1].metric("Passed", summary["passed"])
    metric_cols[2].metric("Failed", summary["failed"])
    metric_cols[3].metric("Errors", summary["errors"])

    started_at = run.get("started_at") or run.get("created_at")
    completed_at = run.get("completed_at")
    st.caption(f"Started: {format_timestamp(started_at)} · Completed: {format_timestamp(completed_at) if completed_at else 'Not completed'}")
    if run.get("status") == "completed_with_errors" or summary["errors"]:
        st.warning("Some tests could not be evaluated. Errors are execution problems and are not prompt-quality failures.")
    elif run.get("status") == "failed":
        st.error("This run did not complete successfully. Review missing results and execution errors below.")
    elif run.get("status") == "running":
        st.info("This run is still marked as running; its summary may be incomplete.")

    with st.expander("Run metadata"):
        _render_run_metadata(run)

    st.divider()
    st.subheader("Test results")
    if not results:
        if summary["total"] == 0:
            st.info("This run contains no planned tests.")
        else:
            st.warning("No result records are available for this run, although tests were planned.")
        _render_legacy_results(prompts)
        return

    filter_choice = st.selectbox("Show results", ["All", "Failed", "Errors", "Passed"], key="results_status_filter")
    visible = filter_results(results, filter_choice)
    st.caption(f"Showing {len(visible)} of {len(results)} persisted result(s). Disabled or unselected cases are not shown as evaluated.")
    snapshots = snapshot_map(run)
    for result in visible:
        snapshot = snapshots.get(result.get("test_case_id"), {})
        _render_test_result(result, snapshot)
    _render_legacy_results(prompts)
