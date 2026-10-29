"""
ui/run_tests.py
Streamlit page to select a prompt version and run all test cases against it,
showing live progress while tests execute concurrently.
"""

import logging

import streamlit as st
from core.db import get_all_prompts, get_all_test_cases
from core.test_runner import run_tests
from core.settings import load_settings
from ui.result_helpers import result_status, safe_error_message
from ui.prompt_workspace import consume_selection_context, request_workspace_navigation

_LOGGER = logging.getLogger(__name__)


def render():
    st.title("Run Evaluation")
    st.caption("Choose a prompt version and evaluate it against all enabled test cases.")

    try:
        prompts = get_all_prompts()
        test_cases = [case for case in get_all_test_cases() if case.get("enabled", True)]
    except Exception:
        _LOGGER.exception("Could not load PromptLab evaluation options")
        st.error("Could not load prompts or test cases. Check database availability and try again.")
        return

    if not prompts:
        st.info("No prompt versions yet. Create a prompt before running an evaluation.")
        if st.button("Open Prompt Workspace", key="run_eval_no_prompt_workspace"):
            request_workspace_navigation(st.session_state, "Prompt Workspace")
            st.rerun()
        return

    if not test_cases:
        st.info("No enabled test cases are available. Add or enable test cases before running an evaluation.")
        if st.button("Open Test Cases", key="run_eval_no_cases"):
            request_workspace_navigation(st.session_state, "Test Cases",
                                         test_cases_prompt_id=st.session_state.get("run_tests_version_select", prompts[0]["id"]))
            st.rerun()
        return

    prompt_labels = []
    for p in prompts:
        label = "Version " + str(p["version_number"]) + " · " + str(p.get("commit_message") or "No version note")
        prompt_labels.append(label)

    prompt_ids = [prompt["id"] for prompt in prompts]
    context_status, _ = consume_selection_context(
        st.session_state, "run_tests_prompt_id", "run_tests_version_select", prompt_ids,
    )
    if context_status == "unavailable":
        st.error("The selected prompt version is no longer available. Choose a saved version on the Prompt Workspace first.")
        return
    selected_prompt_state = st.session_state.get("run_tests_version_select")
    default_index = prompt_ids.index(selected_prompt_state) if selected_prompt_state in prompt_ids else 0
    selected_index = st.selectbox(
        "Prompt version to test",
        prompt_ids,
        index=default_index,
        format_func=lambda prompt_id: prompt_labels[prompt_ids.index(prompt_id)],
        key="run_tests_version_select",
    )
    selected_prompt = next(prompt for prompt in prompts if prompt["id"] == selected_index)

    with st.expander("View selected prompt text"):
        st.code(selected_prompt["prompt_text"], language="text")

    st.write(str(len(test_cases)) + " test case(s) will be run against this version.")

    max_workers = st.slider(
        "Max concurrent requests",
        min_value=1,
        max_value=5,
        value=load_settings()["max_concurrent_requests"],
        help="Higher means faster, but more likely to hit Gemini's rate limits.",
        key="run_tests_max_workers",
    )

    if st.button("Run Evaluation", type="primary", key="run_tests_button"):
        progress_bar = st.progress(0, text="Starting...")
        status_text = st.empty()

        def update_progress(done, total):
            progress_bar.progress(done / total, text="Completed: " + str(done) + "/" + str(total))

        try:
            with st.spinner("Running " + str(len(test_cases)) + " tests..."):
                results = run_tests(
                    selected_prompt["id"],
                    selected_prompt["prompt_text"],
                    test_cases,
                    max_workers=max_workers,
                    progress_callback=update_progress,
                )
        except Exception:
            _LOGGER.exception("PromptLab evaluation run failed")
            status_text.error("The evaluation could not be completed. Check the run history and application logs for details.")
            return

        progress_bar.progress(1.0, text="All tests completed.")
        errors_count = sum(1 for r in results if result_status(r) == "ERROR")
        status_text.warning(f"Evaluation finished with {errors_count} execution error(s)." if errors_count else "All tests completed.")

        passed_count = sum(1 for r in results if result_status(r) == "PASS")
        failed_count = sum(1 for r in results if result_status(r) == "FAIL")
        total_count = len(results)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total", total_count)
        col2.metric("Passed", passed_count)
        col3.metric("Failed", failed_count)
        col4.metric("Errors", errors_count)
        if results and results[0].get("run_id"):
            st.caption(f"Evaluation run ID: {results[0]['run_id']}")
            if st.button("Open Results", key=f"run_eval_open_results_{results[0]['run_id']}"):
                request_workspace_navigation(st.session_state, "Results",
                                             results_pending_run_id=results[0]["run_id"])
                st.rerun()

        st.divider()
        st.subheader("Results")

        for r in results:
            status_label = result_status(r)

            expander_title = r["description"] + " - " + status_label

            with st.expander(expander_title):
                st.write("Evaluation reason:", r.get("score_reason") or "Unavailable")
                st.write("Evaluation method:", r.get("evaluation_method") or "Unavailable")
                st.write("Execution time:", str(r["execution_time"]) + "s")
                if r["error_message"]:
                    if status_label == "ERROR":
                        st.warning("The evaluation could not determine prompt quality for this case. " + safe_error_message(r["error_message"]))
                    else:
                        st.warning("Note: " + safe_error_message(r["error_message"]))
                st.write("AI response:")
                st.code(r["ai_response"] or "(no response)", language="text")
