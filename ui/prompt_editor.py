"""Prompt Workspace: edit immutable prompt versions and launch evaluation flows."""

import logging

import streamlit as st

from core.db import (
    create_prompt_version, get_all_prompts, get_all_test_cases,
    get_all_test_suites, get_evaluation_runs_for_prompt, get_prompt_by_id,
)
from ui.prompt_workspace import (
    find_prompt_version, is_prompt_dirty, prompt_version_label, prompt_version_options, request_workspace_navigation,
    save_new_prompt_version, validate_template_for_editor,
)

_LOGGER = logging.getLogger(__name__)


def _version_key(prefix, version_id):
    suffix = "new" if version_id is None else str(version_id)
    return f"prompt_workspace_{prefix}_{suffix}"


def _navigate(page, **context):
    request_workspace_navigation(st.session_state, page, **context)
    st.rerun()


def render():
    st.title("Prompt Workspace")
    st.caption("Edit a prompt, save an immutable version, then evaluate and compare its results.")

    saved_notice = st.session_state.pop("prompt_workspace_saved_notice", None)
    if saved_notice:
        st.success(saved_notice)

    # Apply deferred widget state before the selector is instantiated this rerun.
    pending_version_id = st.session_state.pop("prompt_workspace_pending_version_id", None)
    if pending_version_id is not None:
        st.session_state["prompt_workspace_version_id"] = pending_version_id

    try:
        prompts = get_all_prompts()
        test_cases = get_all_test_cases()
        suites = get_all_test_suites()
    except Exception:
        _LOGGER.exception("Could not load prompt workspace data")
        st.error("Could not load prompts and evaluation options. Check database availability and try again.")
        return

    prompts_by_id = {prompt["id"]: prompt for prompt in prompts}
    version_ids = prompt_version_options(prompts)
    active_id = st.session_state.get("prompt_workspace_version_id")
    if active_id not in version_ids:
        active_id = prompts[0]["id"] if prompts else None
    st.selectbox(
        "Prompt / version",
        options=version_ids,
        index=version_ids.index(active_id),
        format_func=lambda version_id: "Create a new version" if version_id is None else prompt_version_label(prompts_by_id[version_id]),
        key="prompt_workspace_version_id",
        help="Choose a saved version to edit into a new version, or start a separate version history entry.",
    )
    selected_id = st.session_state.get("prompt_workspace_version_id")
    selected_prompt = find_prompt_version(prompts, selected_id)
    if selected_id is not None and selected_prompt is None:
        st.error("The selected prompt version is no longer available. Choose another version.")
        return

    if selected_prompt:
        st.subheader(f"Prompt · Version {selected_prompt['version_number']}")
        st.caption("Saving edits creates a new version. This selected historical version will remain unchanged.")
        info_col1, info_col2 = st.columns(2)
        info_col1.metric("Selected version", f"Version {selected_prompt['version_number']}")
        info_col2.metric("Created", str(selected_prompt.get("created_at", "Unavailable"))[:19])
        st.markdown("**Version note**")
        st.write(selected_prompt.get("commit_message") or "No version note was recorded.")
    else:
        st.subheader("Create your first prompt version" if not prompts else "Create another prompt version")
        st.info("Start with a prompt template containing {input}. PromptLab replaces it with each test case input during evaluation.")

    source_text = selected_prompt.get("prompt_text", "") if selected_prompt else ""
    text_key = _version_key("template", selected_id)
    note_key = _version_key("note", selected_id)
    st.session_state.setdefault(text_key, source_text)
    st.session_state.setdefault(note_key, "")

    st.subheader("Prompt Template")
    prompt_text = st.text_area(
        "Prompt Template",
        height=360,
        key=text_key,
        help="Include {input}. PromptLab replaces it with each test case's input during evaluation.",
        placeholder="You are a helpful assistant. Respond to this request:\n{input}",
        label_visibility="collapsed",
    )

    valid, validation_message = validate_template_for_editor(prompt_text)
    if valid:
        st.success(validation_message)
    elif prompt_text.strip():
        st.warning(validation_message)
    else:
        st.info("Enter a prompt template containing {input}.")

    version_note = st.text_input(
        "Version note",
        key=note_key,
        help="Briefly describe what changed in this version.",
        placeholder="Improved refund instructions and made the tone more concise.",
    )
    dirty = is_prompt_dirty(prompt_text, source_text, version_note, is_new=selected_prompt is None)
    if dirty:
        st.warning("Unsaved changes")
    elif selected_prompt:
        st.caption("No unsaved changes to this version.")

    if st.button("Save New Version", type="primary", key="prompt_workspace_save_version"):
        try:
            new_id = save_new_prompt_version(prompt_text, version_note, source_prompt=selected_prompt,
                                             saver=create_prompt_version)
            new_prompt = get_prompt_by_id(new_id)
            version_number = new_prompt.get("version_number", "new") if new_prompt else "new"
            st.session_state["prompt_workspace_saved_notice"] = f"Version {version_number} saved successfully."
            st.session_state["prompt_workspace_pending_version_id"] = new_id
            st.session_state.pop(_version_key("template", new_id), None)
            st.session_state.pop(_version_key("note", new_id), None)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            _LOGGER.exception("Could not save a prompt version")
            st.error("Could not save this version. Check database availability and try again.")

    if prompts:
        with st.expander("Version history"):
            st.dataframe(
                [{"Version": prompt.get("version_number"),
                  "Version note": prompt.get("commit_message") or "No version note",
                  "Created": str(prompt.get("created_at") or "Unavailable")[:19],
                  "Selected": prompt.get("id") == selected_id}
                 for prompt in prompts],
                use_container_width=True,
                hide_index=True,
            )

    st.divider()
    st.subheader("Test and evaluate this prompt")
    enabled_cases = sum(bool(case.get("enabled", True)) for case in test_cases)
    enabled_suites = sum(bool(suite.get("enabled", True)) for suite in suites)
    st.caption(f"{enabled_cases} enabled test case(s) · {enabled_suites} enabled test suite(s) available")
    action_cols = st.columns(4)
    if action_cols[0].button("Test Cases", key="workspace_test_cases"):
        context = {"test_cases_prompt_id": selected_prompt["id"]} if selected_prompt else {}
        _navigate("Test Cases", **context)

    can_evaluate = selected_prompt is not None and not dirty and valid
    if action_cols[1].button("Run Evaluation", disabled=not can_evaluate, key="workspace_run_evaluation"):
        st.session_state["run_tests_prompt_id"] = selected_prompt["id"]
        _navigate("Run Evaluation")
    if selected_prompt and dirty:
        st.caption("Save a new version before evaluating these edits.")

    try:
        selected_runs = get_evaluation_runs_for_prompt(selected_prompt["id"]) if selected_prompt else []
    except Exception:
        _LOGGER.exception("Could not load runs for prompt version %s", selected_id)
        selected_runs = []
        st.warning("Existing evaluation runs for this version could not be loaded.")
    latest_run = selected_runs[0] if selected_runs else None

    if action_cols[2].button("View Results", disabled=latest_run is None, key="workspace_view_results"):
        st.session_state["results_pending_run_id"] = latest_run["run_id"]
        _navigate("Results")
    if action_cols[3].button("Compare Runs", disabled=latest_run is None, key="workspace_compare_runs"):
        st.session_state["compare_candidate_pending_run_id"] = latest_run["run_id"]
        _navigate("Compare Runs")
    if selected_prompt and latest_run is None:
        st.caption("No evaluation runs exist for this version yet. Run an evaluation to create its first results.")

    with st.expander("Advanced version information"):
        st.write("**Prompt version ID:**", selected_prompt.get("id") if selected_prompt else "Not saved yet")
        st.write("**Created at:**", selected_prompt.get("created_at") if selected_prompt else "Not saved yet")
