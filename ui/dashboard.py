"""Actionable PromptLab overview backed by lightweight persisted summaries."""

import logging

import streamlit as st

from core.db import get_dashboard_snapshot
from ui.dashboard_helpers import build_dashboard_model, build_recent_activity
from ui.prompt_workspace import request_workspace_navigation
from ui.result_helpers import format_run_status, format_timestamp, run_summary

_LOGGER = logging.getLogger(__name__)


def _navigate(page, **context):
    request_workspace_navigation(st.session_state, page, **context)
    st.rerun()


def _run_context(prompt):
    return {"run_tests_prompt_id": prompt["id"]} if prompt else {}


def render():
    st.title("PromptLab Dashboard")
    st.caption("Your prompt evaluation workflow: create a version, test it, review results, and compare runs.")

    try:
        snapshot = get_dashboard_snapshot(recent_run_limit=10)
    except Exception:
        _LOGGER.exception("Unable to load PromptLab dashboard data")
        st.error("Could not load dashboard data. Check database availability and try again.")
        return

    model = build_dashboard_model(snapshot)
    recent_prompts = model["recent_prompts"]
    latest_prompt = recent_prompts[0] if recent_prompts else None
    metrics = model["metrics"]
    latest = model["latest_run"]
    latest_summary = run_summary(latest) if latest else None

    st.subheader("Project overview")
    metric_cols = st.columns(5)
    metric_cols[0].metric("Prompt versions", metrics["prompt_versions"])
    metric_cols[1].metric("Enabled test cases", metrics["enabled_test_cases"])
    metric_cols[2].metric("Test suites", metrics["test_suites"],
                          delta=f"{metrics['enabled_suites']} enabled" if metrics["test_suites"] else None,
                          delta_color="off")
    metric_cols[3].metric("Evaluation runs", metrics["evaluation_runs"])
    latest_rate = latest_summary["pass_rate"] if latest_summary else None
    metric_cols[4].metric("Latest run pass rate", f"{latest_rate:.2f}%" if isinstance(latest_rate, (int, float)) else "N/A")

    st.divider()
    action, latest_col = st.columns([1, 1.5])
    with action:
        st.subheader("What to do next")
        next_action = model["next_action"]
        st.info(next_action["message"])
        context = {}
        if next_action["page"] == "Run Evaluation":
            context.update(_run_context(latest_prompt))
        elif next_action["page"] == "Results" and latest:
            context["results_pending_run_id"] = latest["run_id"]
        elif next_action["page"] == "Test Cases" and latest_prompt:
            context["test_cases_prompt_id"] = latest_prompt["id"]
        if st.button(next_action["label"], type="primary", key="dashboard_next_action"):
            _navigate(next_action["page"], **context)

    with latest_col:
        st.subheader("Latest evaluation")
        if latest is None:
            st.info("No evaluation runs yet. Run an evaluation to see results here.")
        else:
            metadata = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
            suite_snapshot = metadata.get("suite_snapshot") if isinstance(metadata.get("suite_snapshot"), dict) else {}
            recent_suites = {suite["id"]: suite for suite in snapshot.get("recent_suites", [])}
            suite = recent_suites.get(latest.get("suite_id"), {}) if latest.get("suite_id") else {}
            suite_name = suite.get("name") or suite_snapshot.get("name") or "All enabled tests"
            st.markdown(f"**Prompt version:** Version {latest.get('prompt_version_number', '?')} - {latest.get('prompt_version_note') or 'No version note'}")
            st.markdown(f"**Test selection:** {suite_name}")
            st.markdown(f"**Status:** {format_run_status(latest.get('status'))}")
            st.caption(f"Started {format_timestamp(latest.get('started_at') or latest.get('created_at'))}")
            result_cols = st.columns(4)
            result_cols[0].metric("Planned", latest_summary["total"])
            result_cols[1].metric("Passed", latest_summary["passed"])
            result_cols[2].metric("Failed", latest_summary["failed"])
            result_cols[3].metric("Errors", latest_summary["errors"])
            if st.button("Open latest results", key="dashboard_latest_results"):
                _navigate("Results", results_pending_run_id=latest["run_id"])

    st.divider()
    st.subheader("Quick actions")
    quick_cols = st.columns(4)
    if quick_cols[0].button("Prompt Workspace", key="dashboard_prompt_workspace"):
        context = {"prompt_workspace_pending_version_id": latest_prompt["id"]} if latest_prompt else {}
        _navigate("Prompt Workspace", **context)
    if quick_cols[1].button("Test Cases", key="dashboard_test_cases"):
        context = {"test_cases_prompt_id": latest_prompt["id"]} if latest_prompt else {}
        _navigate("Test Cases", **context)
    if quick_cols[2].button("Run Evaluation", disabled=latest_prompt is None or not metrics["enabled_test_cases"],
                            key="dashboard_run_evaluation"):
        _navigate("Run Evaluation", **_run_context(latest_prompt))
    if quick_cols[3].button("Compare Runs", disabled=not model["can_compare"], key="dashboard_compare_runs"):
        candidate = model["latest_finalized_run"]
        if candidate:
            _navigate("Compare Runs", compare_candidate_pending_run_id=candidate["run_id"])
        else:
            _navigate("Compare Runs")
    if not model["can_compare"]:
        st.caption("Compare Runs becomes available after at least two runs are finalized. You will choose the baseline explicitly.")

    st.divider()
    activity_col, prompts_col = st.columns([1.2, 1])
    with activity_col:
        st.subheader("Recent activity")
        activity = build_recent_activity(
            snapshot.get("recent_prompts", []), snapshot.get("recent_test_cases", []),
            snapshot.get("recent_suites", []), model["recent_runs"], limit=8,
        )
        if not activity:
            st.info("No project activity yet. Create a prompt version to get started.")
        else:
            for event in activity:
                st.markdown(f"**{event['kind']}** - {event['detail']}")
                st.caption(format_timestamp(event["timestamp"]))

    with prompts_col:
        st.subheader("Recent prompt versions")
        if not recent_prompts:
            st.info("No prompt versions yet. Create your first prompt in Prompt Workspace.")
        else:
            for prompt in recent_prompts:
                st.markdown(f"**Version {prompt.get('version_number', '?')}** - {prompt.get('commit_message') or 'No version note'}")
                st.caption(format_timestamp(prompt.get("created_at")))
                if st.button("Open this version", key=f"dashboard_open_prompt_{prompt['id']}"):
                    _navigate("Prompt Workspace", prompt_workspace_pending_version_id=prompt["id"])
