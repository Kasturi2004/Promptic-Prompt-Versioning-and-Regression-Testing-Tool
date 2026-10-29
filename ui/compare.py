"""Compare two explicitly selected evaluation runs using the Phase 5 API."""

import logging

import streamlit as st

from core.db import get_all_prompts, get_all_test_suites, get_evaluation_runs_for_prompt
from ui.result_helpers import (
    compare_selected_runs, comparison_groups, comparison_interpretation,
    format_run_status, format_timestamp, run_label,
)
from ui.prompt_workspace import consume_compare_candidate_context
from ui.prompt_workspace import request_workspace_navigation

_LOGGER = logging.getLogger(__name__)


def _load_runs(prompts):
    runs = []
    for prompt in prompts:
        runs.extend(get_evaluation_runs_for_prompt(prompt["id"]))
    return sorted(runs, key=lambda run: (run.get("started_at") or run.get("created_at") or "", run.get("run_id", "")), reverse=True)


def _show_comparison_test(test, kind):
    st.markdown(f"**{test.description}** · Test case #{test.test_case_id}")
    if kind == "ERROR":
        st.warning("This result could not be reliably classified as prompt quality because at least one run has an error or missing result.")
    elif kind == "ADDED":
        st.info("This test exists in the candidate run but not the baseline.")
    elif kind == "REMOVED":
        st.info("This test existed in the baseline run but not the candidate.")
    st.caption(f"Baseline: {test.baseline_status or 'Not run'} · Candidate: {test.candidate_status or 'Not run'}")
    expected = test.candidate_expected if test.candidate_expected is not None else test.baseline_expected
    if expected is not None:
        st.write("**Expected:**", expected)
    if test.baseline_evaluation_method or test.candidate_evaluation_method:
        st.caption(f"Evaluation method: {test.baseline_evaluation_method or 'Unknown'} → {test.candidate_evaluation_method or 'Unknown'}")
    old, new = st.columns(2)
    with old:
        st.markdown("**Baseline response**")
        st.code(test.baseline_response if test.baseline_response is not None else "(no response)", language="text")
        if test.baseline_reason:
            st.caption(test.baseline_reason)
    with new:
        st.markdown("**Candidate response**")
        st.code(test.candidate_response if test.candidate_response is not None else "(no response)", language="text")
        if test.candidate_reason:
            st.caption(test.candidate_reason)


def _render_group(title, items, kind, empty_message, expanded=False):
    with st.expander(f"{title} ({len(items)})", expanded=expanded):
        if not items:
            st.info(empty_message)
            return
        for index, test in enumerate(items):
            if index:
                st.divider()
            _show_comparison_test(test, kind)


def render():
    st.title("Compare Runs")
    st.caption("Select two exact runs to see what improved, regressed, or could not be compared.")

    try:
        prompts = get_all_prompts()
        runs = _load_runs(prompts)
        runs = [run for run in runs if run.get("status") != "running"]
        suites = get_all_test_suites()
    except Exception:
        _LOGGER.exception("Unable to load runs for comparison")
        st.error("Could not load evaluation runs. Check database availability and try again.")
        return

    if not runs:
        st.info("No finalized evaluation runs are available to compare yet. Complete two runs first.")
        if st.button("Run Evaluation", key="compare_empty_run_evaluation"):
            request_workspace_navigation(st.session_state, "Run Evaluation")
            st.rerun()
        return
    if len(runs) < 2:
        st.info("One finalized run is available. Run another evaluation before comparing results.")

    prompts_by_id = {prompt["id"]: prompt for prompt in prompts}
    suites_by_id = {suite["id"]: suite for suite in suites}
    runs_by_id = {run["run_id"]: run for run in runs}
    options = [None] + [run["run_id"] for run in runs]
    candidate_context = consume_compare_candidate_context(st.session_state, runs_by_id)
    if candidate_context == "unavailable":
        st.info("The selected version's evaluation run is no longer available.")
    label = lambda run_id: "Select a run…" if run_id is None else run_label(runs_by_id[run_id], prompts_by_id, suites_by_id)
    baseline_col, candidate_col = st.columns(2)
    with baseline_col:
        baseline_id = st.selectbox("Baseline run", options, index=0, format_func=label, key="compare_baseline_run")
    with candidate_col:
        candidate_id = st.selectbox("Candidate run", options, index=0, format_func=label, key="compare_candidate_run")

    st.caption("Choose a baseline and candidate explicitly. Opening Compare Runs from Results prefills only the candidate.")
    st.caption("Run details")
    details_a, details_b = st.columns(2)
    with details_a:
        st.write("**Baseline**")
        _render_selected_run(runs_by_id.get(baseline_id), prompts_by_id, suites_by_id)
    with details_b:
        st.write("**Candidate**")
        _render_selected_run(runs_by_id.get(candidate_id), prompts_by_id, suites_by_id)

    if not baseline_id or not candidate_id:
        st.info("Select a baseline run and a candidate run to compare prompt performance.")
        return
    if baseline_id == candidate_id:
        st.warning("Choose two different runs to compare.")
        return

    try:
        comparison = compare_selected_runs(baseline_id, candidate_id)
    except ValueError as exc:
        st.error(str(exc))
        return
    except Exception:
        _LOGGER.exception("Comparison failed for selected runs")
        st.error("Could not compare these runs. One may have been deleted or its stored data may be invalid.")
        return

    if not comparison.tests:
        st.info("These runs contain no test cases to compare.")

    st.divider()
    st.subheader("Comparison summary")
    rate_a, rate_b, delta_col = st.columns(3)
    rate_a.metric("Baseline pass rate", _percent(comparison.baseline_pass_rate))
    rate_b.metric("Candidate pass rate", _percent(comparison.candidate_pass_rate))
    delta = comparison.pass_rate_delta
    delta_label = "N/A" if delta is None else f"{delta:+.2f} pp"
    delta_col.metric("Pass-rate change", delta_label, help="Percentage-point difference, not relative percentage growth.")
    st.info(comparison_interpretation(comparison))

    counts = st.columns(6)
    for column, label_text, value in zip(counts, ("Regressions", "Improvements", "Unchanged", "Errors", "Added", "Removed"),
                                         (comparison.regressions, comparison.improvements, comparison.unchanged,
                                          comparison.errors, comparison.added, comparison.removed)):
        column.metric(label_text, value)

    groups = comparison_groups(comparison)
    _render_group("Regressions", groups["REGRESSION"], "REGRESSION", "No regressions detected.", expanded=True)
    _render_group("Improvements", groups["IMPROVEMENT"], "IMPROVEMENT", "No improvements detected.", expanded=True)
    _render_group("Execution errors / missing results", groups["ERROR"], "ERROR",
                  "No errors or missing results in this comparison.")
    _render_group("Added tests", groups["ADDED"], "ADDED", "No candidate-only tests.")
    _render_group("Removed tests", groups["REMOVED"], "REMOVED", "No baseline-only tests.")
    _render_group("Unchanged tests", groups["UNCHANGED"], "UNCHANGED", "No unchanged tests.")

    candidate_prompt = prompts_by_id.get(runs_by_id[candidate_id].get("prompt_id"), {})
    if st.button("Improve Prompt", disabled=not candidate_prompt, key=f"compare_improve_{candidate_id}"):
        st.session_state["prompt_workspace_pending_version_id"] = candidate_prompt["id"]
        request_workspace_navigation(st.session_state, "Prompt Workspace")
        st.rerun()


def _render_selected_run(run, prompts_by_id, suites_by_id):
    if run is None:
        st.caption("No run selected.")
        return
    prompt = prompts_by_id.get(run.get("prompt_id"), {})
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    suite_id = run.get("suite_id")
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    suite_snapshot = metadata.get("suite_snapshot")
    suite_snapshot = suite_snapshot if isinstance(suite_snapshot, dict) else {}
    suite = suites_by_id.get(suite_id, {}) if suite_id is not None else {}
    suite_name = suite.get("name") or suite_snapshot.get("name") or (f"Suite #{suite_id}" if suite_id else "No suite")
    rate = summary.get("pass_rate")
    st.caption(f"Prompt v{prompt.get('version_number', '?')} · {suite_name} · {rate:.2f}% pass rate" if isinstance(rate, (int, float)) else f"Prompt v{prompt.get('version_number', '?')} · {suite_name} · Pass rate unavailable")
    st.caption(f"Run ID: {run.get('run_id')}")
    st.caption(f"Status: {format_run_status(run.get('status'))}")
    st.caption(f"Started: {format_timestamp(run.get('started_at') or run.get('created_at'))}")


def _percent(value):
    return "N/A" if value is None else f"{value:.2f}%"
