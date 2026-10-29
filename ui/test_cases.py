"""Test case and suite management workspace backed by existing core APIs."""

import logging
from pathlib import Path
from datetime import datetime

try:
    import streamlit as st
except ModuleNotFoundError as exc:
    # unittest discovery imports this UI module because its filename begins
    # with "test_". Keep helper/test discovery usable without app dependencies.
    if exc.name != "streamlit":
        raise
    st = None
import yaml

from core import db
from core.test_runner import run_suite, run_tests
from ui.prompt_workspace import consume_selection_context, request_workspace_navigation
from ui.test_case_helpers import (
    case_label, evaluator_label, evaluator_options, execute_evaluation,
    export_cases, filter_test_cases, format_expected_config, is_keyword_list,
    is_word_count, parse_import_cases, suite_counts, suite_label,
    validate_case_form,
)

_LOGGER = logging.getLogger(__name__)


def _widget_value(key, initial):
    st.session_state.setdefault(key, initial)
    return st.session_state[key]


def _expected_widget(method, key, initial=None):
    if method == "response_not_empty":
        st.caption("No expected value is needed. PromptLab checks that the response contains text.")
        return None
    formatted = format_expected_config(method, initial)
    if is_keyword_list(method):
        _widget_value(key, formatted or "")
        return st.text_input("Keywords", key=key, help="Enter one or more keywords separated by commas.")
    if is_word_count(method):
        try:
            default = max(0, int(formatted or 0))
        except (TypeError, ValueError):
            default = 0
        _widget_value(key, default)
        return st.number_input("Word-count limit", min_value=0, step=1, key=key)
    _widget_value(key, formatted or "")
    label = "Regular expression" if method == "regex_match" else "Expected response" if method == "exact_match" else "Expected text"
    return st.text_input(label, key=key, help="Enter the value the evaluator should check for.")


def _humanize_error(message):
    text = str(message)
    if "Input text is required" in text or "input_text is required" in text:
        return "Enter the input that the prompt should receive."
    if "name is required" in text:
        return "Enter a name for this test case."
    if "Invalid regex" in text:
        return "Your regular expression is invalid. Check its syntax."
    if "duplicate" in text.casefold():
        return "A test case or suite cannot contain the same member more than once."
    if "does not exist" in text:
        return "The selected test case or suite is no longer available. Refresh the page and choose again."
    if "disabled" in text:
        return "This suite is disabled. Enable it before running an evaluation."
    return text if isinstance(message, ValueError) else "The operation could not be completed. Check the saved values and try again."


def _run_evaluation(mode, prompt, suite_id=None, enabled_cases=None):
    if not prompt:
        st.error("Choose a saved prompt version before running an evaluation.")
        return
    progress = st.progress(0, text="Starting evaluation…")

    def report_progress(done, total):
        progress.progress(done / total if total else 0, text=f"Evaluated {done} of {total} test cases")

    try:
        with st.spinner("Running the selected evaluation…"):
            results = execute_evaluation(
                mode, prompt["id"], prompt["prompt_text"], suite_id=suite_id,
                enabled_cases=enabled_cases, progress_callback=report_progress,
                suite_runner=run_suite, all_tests_runner=run_tests,
            )
    except ValueError as exc:
        st.error(_humanize_error(exc))
        return
    except Exception:
        _LOGGER.exception("Test case evaluation could not be completed")
        st.error("The evaluation could not be completed. Check the prompt, suite, and application logs.")
        return
    if not results:
        st.warning("No enabled test cases were evaluated. Enable cases in this suite or select another suite.")
        return
    run_id = results[0].get("run_id")
    if not run_id or any(result.get("run_id") != run_id for result in results):
        st.error("PromptLab returned results without one consistent evaluation run. Open Results and check the run history.")
        return
    request_workspace_navigation(st.session_state, "Results", results_pending_run_id=run_id)
    st.rerun()


def _render_expected_fields(prefix, initial=None):
    initial = initial or {}
    method_key = f"{prefix}_method"
    current_method = initial.get("scoring_type", "contains_keyword")
    options = evaluator_options(current_method)
    current_index = options.index(current_method) if current_method in options else 0
    method = st.selectbox(
        "Evaluation Method",
        options,
        index=current_index,
        format_func=evaluator_label,
        key=method_key,
        help="Choose how PromptLab should evaluate the generated response.",
    )
    expected_key = f"{prefix}_expected_{method}"
    expected = _expected_widget(method, expected_key, initial.get("expected_keyword"))
    return method, expected


def _case_form_fields(prefix, initial=None, include_enabled=True):
    initial = initial or {}
    name_key = f"{prefix}_name"
    description_key = f"{prefix}_description"
    input_key = f"{prefix}_input"
    note_key = f"{prefix}_expected_note"
    _widget_value(name_key, initial.get("name") or initial.get("description") or "")
    name = st.text_input("Test Case Name", key=name_key)
    _widget_value(description_key, initial.get("description") or "")
    description = st.text_input("Description", key=description_key, help="Optional context to help people recognize this test.")
    _widget_value(input_key, initial.get("input_text") or "")
    input_text = st.text_area("Input", key=input_key, help="The input inserted into the prompt's {input} variable.")
    method, expected = _render_expected_fields(prefix, initial)
    _widget_value(note_key, initial.get("expected_condition") or "")
    expected_note = st.text_input("Expected result note (optional)", key=note_key,
                                  help="Optional explanation in addition to the evaluator's expected value.")
    enabled = True
    if include_enabled:
        enabled_key = f"{prefix}_enabled"
        _widget_value(enabled_key, bool(initial.get("enabled", True)))
        enabled = st.checkbox("Enabled", key=enabled_key,
                              help="Disabled test cases are retained but excluded from new evaluations.")
    return {"name": name, "description": description, "input_text": input_text,
            "expected_condition": expected_note, "expected_keyword": expected,
            "scoring_type": method, "enabled": enabled}


def _render_create_case(cases):
    instance = st.session_state.get("test_case_create_instance", 0)
    st.subheader("Create Test Case")
    st.caption("Define one input and the evaluation rule PromptLab should apply to its response.")
    fields = _case_form_fields(f"test_case_create_{instance}")
    valid, message, normalized = validate_case_form(
        fields["name"], fields["description"], fields["input_text"], fields["scoring_type"],
        fields["expected_keyword"], fields["enabled"],
    )
    if fields["input_text"].strip():
        (st.success if valid else st.warning)(message)
    if st.button("Create Test Case", type="primary", key=f"test_case_create_submit_{instance}"):
        if not valid:
            st.error(message)
            return
        try:
            new_id = db.create_test_case(
                fields["description"].strip(), fields["input_text"], fields["expected_condition"].strip(),
                normalized["expected_keyword"], fields["scoring_type"],
                name=fields["name"].strip(), enabled=fields["enabled"],
            )
            st.session_state["test_case_selected_id"] = new_id
            st.session_state["test_case_create_instance"] = instance + 1
            st.success("Test case created.")
            st.rerun()
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not create a PromptLab test case")
            st.error("Could not create the test case. Check database availability and try again.")


def _render_cases_tab(cases, memberships):
    _render_create_case(cases)
    st.divider()
    st.subheader("Test Cases")
    filter_col, search_col = st.columns([1, 2])
    with filter_col:
        state_filter = st.selectbox("State", ["All", "Enabled", "Disabled"], key="test_case_state_filter")
    with search_col:
        query = st.text_input("Search test cases", key="test_case_search", placeholder="Name or description")
    filtered = filter_test_cases(cases, state_filter, query)
    st.caption(f"{len(filtered)} of {len(cases)} test case(s) · Disabled cases are not included in new evaluations.")
    if not cases:
        st.info("No test cases yet. Create a test case to define how PromptLab should evaluate your prompt.")
        return
    if not filtered:
        st.info("No test cases match this filter. Try another state or search term.")
        return

    st.dataframe([{
        "Test Case": case.get("name") or case.get("description") or "Untitled",
        "State": "Enabled" if bool(case.get("enabled", True)) else "Disabled",
        "Evaluation Method": evaluator_label(case.get("scoring_type")),
        "Suites": ", ".join(memberships.get(case["id"], [])) or "None",
        "Updated": str(case.get("updated_at") or case.get("created_at") or "Unavailable")[:19],
    } for case in filtered], use_container_width=True, hide_index=True)

    visible_ids = [case["id"] for case in filtered]
    selected_id = st.session_state.get("test_case_selected_id")
    if selected_id not in visible_ids:
        selected_id = visible_ids[0]
        st.session_state["test_case_selected_id"] = selected_id
    case_by_id = {case["id"]: case for case in cases}
    selected_id = st.selectbox(
        "Select a test case to view or edit",
        visible_ids,
        index=visible_ids.index(selected_id),
        format_func=lambda case_id: case_label(case_by_id[case_id]),
        key="test_case_selected_id",
    )
    selected = case_by_id.get(selected_id)
    if selected is None:
        st.warning("The selected test case is no longer available. Refresh the list and select another.")
        return

    st.subheader("Test Case Details")
    st.caption("Editing a test case keeps its ID stable. Previous evaluation results remain linked to the same test case.")
    st.write("**Current Evaluation Method:**", evaluator_label(selected.get("scoring_type")))
    st.write("**Current state:**", "Enabled" if bool(selected.get("enabled", True)) else "Disabled")
    suite_names = memberships.get(selected_id, [])
    st.write("**Test Suites:**", ", ".join(suite_names) if suite_names else "Not included in a suite")

    revision = selected.get("updated_at") or selected.get("created_at") or "legacy"
    prefix = f"test_case_edit_{selected_id}_{revision}"
    fields = _case_form_fields(prefix, selected)
    valid, message, normalized = validate_case_form(
        fields["name"], fields["description"], fields["input_text"], fields["scoring_type"],
        fields["expected_keyword"], fields["enabled"],
    )
    if fields["input_text"].strip() and not valid:
        st.warning(message)
    if st.button("Save Changes", type="primary", key=f"{prefix}_save"):
        if not valid:
            st.error(message)
            return
        try:
            db.update_test_case(
                selected_id, fields["description"].strip(), fields["input_text"],
                fields["expected_condition"].strip(), normalized["expected_keyword"],
                fields["scoring_type"], name=fields["name"].strip(), enabled=fields["enabled"],
            )
            st.success("Changes saved. Existing evaluation history remains associated with this test case.")
            st.rerun()
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not update PromptLab test case %s", selected_id)
            st.error("Could not save changes. Check database availability and try again.")

    with st.expander("Advanced test case details"):
        st.write("**Test case ID:**", selected_id)
        st.write("**Internal evaluator key:**", selected.get("scoring_type"))


def _load_suite_members(suites):
    result = {}
    for suite in suites:
        try:
            result[suite["id"]] = db.get_test_cases_for_suite(suite["id"], enabled_only=False)
        except Exception:
            _LOGGER.exception("Could not load membership for suite %s", suite.get("id"))
            result[suite["id"]] = []
    return result


def _render_create_suite():
    instance = st.session_state.get("test_suite_create_instance", 0)
    st.subheader("Create Test Suite")
    name_key, description_key = f"test_suite_create_name_{instance}", f"test_suite_create_description_{instance}"
    _widget_value(name_key, "")
    name = st.text_input("Suite Name", key=name_key)
    _widget_value(description_key, "")
    description = st.text_area("Description (optional)", key=description_key)
    if st.button("Create Suite", key=f"test_suite_create_submit_{instance}"):
        if not name.strip():
            st.error("Enter a name for this test suite.")
            return
        try:
            suite_id = db.create_test_suite(name, description)
            st.session_state["test_suite_selected_id"] = suite_id
            st.session_state["test_suite_create_instance"] = instance + 1
            st.success("Test suite created. Add test cases below.")
            st.rerun()
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not create a PromptLab test suite")
            st.error("Could not create the suite. Check database availability and try again.")


def _render_suites_tab(suites, suites_by_id, cases):
    _render_create_suite()
    st.divider()
    st.subheader("Test Suites")
    if not suites:
        st.info("No test suites yet. Create a suite to group related test cases.")
        return None

    options = [suite["id"] for suite in suites]
    selected_id = st.session_state.get("test_suite_selected_id")
    if selected_id not in options:
        selected_id = options[0]
        st.session_state["test_suite_selected_id"] = selected_id
    suite = st.selectbox(
        "Select a test suite",
        options,
        index=options.index(selected_id),
        format_func=lambda suite_id: suite_label(suites_by_id[suite_id], suites_by_id[suite_id].get("_members", [])),
        key="test_suite_selected_id",
    )
    selected_suite = suites_by_id.get(suite)
    if selected_suite is None:
        st.error("The selected suite is no longer available. Refresh the page and choose another.")
        return None
    members = selected_suite.get("_members", [])
    counts = suite_counts(selected_suite, members)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cases in suite", counts["total"])
    c2.metric("Enabled", counts["enabled"])
    c3.metric("Disabled", counts["disabled"])
    c4.metric("Cases in new run", counts["enabled"] if counts["suite_enabled"] else 0)
    if not counts["suite_enabled"]:
        st.warning("This suite is disabled and cannot be run.")

    st.markdown("**Suite details**")
    details_key = f"test_suite_details_{suite}_{selected_suite.get('updated_at', 'legacy')}"
    name_key, description_key = f"{details_key}_name", f"{details_key}_description"
    _widget_value(name_key, selected_suite.get("name") or "")
    _widget_value(description_key, selected_suite.get("description") or "")
    suite_name = st.text_input("Suite Name", key=name_key)
    suite_description = st.text_area("Description", key=description_key)
    if st.button("Save Suite Details", key=f"save_test_suite_details_{suite}"):
        if not suite_name.strip():
            st.error("Enter a name for this test suite.")
        else:
            try:
                db.update_test_suite(suite, suite_name, suite_description)
                st.success("Suite details saved. Existing evaluation history remains associated with this suite.")
                st.rerun()
            except ValueError as exc:
                st.error(_humanize_error(exc))
            except Exception:
                _LOGGER.exception("Could not update suite details %s", suite)
                st.error("Could not save suite details. Check database availability and try again.")

    st.markdown("**Included test cases**")
    if not members:
        st.info("This suite has no test cases. Add cases below.")
    else:
        for member in members:
            state = "Enabled" if bool(member.get("enabled", True)) else "Disabled · excluded from new evaluations"
            st.write(f"- **{member.get('name') or member.get('description') or 'Untitled'}** — {state}")

    status_key = f"test_suite_enabled_{suite}"
    _widget_value(status_key, bool(selected_suite.get("enabled", True)))
    enabled = st.checkbox("Suite is enabled", key=status_key,
                          help="Disabled suites cannot be evaluated.")
    if st.button("Save Suite Status", key=f"save_test_suite_status_{suite}"):
        try:
            db.set_test_suite_enabled(suite, bool(enabled))
            st.success("Suite status saved.")
            st.rerun()
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not update suite status %s", suite)
            st.error("Could not save suite status. Check database availability and try again.")

    st.markdown("**Suite membership**")
    all_ids = [case["id"] for case in cases]
    current_ids = [member["id"] for member in members if member["id"] in all_ids]
    membership_key = f"test_suite_members_{suite}"
    _widget_value(membership_key, current_ids)
    available_cases = {case["id"]: case for case in cases}
    selected_members = st.multiselect(
        "Included and available test cases",
        options=all_ids,
        format_func=lambda case_id: case_label(available_cases[case_id]),
        key=membership_key,
        help="Select the cases this suite should contain. Disabled members remain listed but are skipped when the suite runs.",
    )
    if st.button("Save Suite Membership", type="primary", key=f"save_test_suite_members_{suite}"):
        if len(selected_members) != len(set(selected_members)):
            st.error("A test case can only appear once in a suite.")
            return suite
        try:
            db.set_test_suite_cases(suite, selected_members)
            st.success("Suite membership saved.")
            st.rerun()
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not update membership for suite %s", suite)
            st.error("Could not save suite membership. Check database availability and try again.")

    included_ids = set(selected_members)
    st.caption(f"{len(included_ids)} included · {len(set(all_ids) - included_ids)} available to add")
    return suite


def _render_advanced_tab(cases):
    st.subheader("Advanced: Import / Export")
    st.caption("Import and export the existing YAML test-case format. Imports are validated as a batch before any cases are saved.")
    uploaded = st.file_uploader("Upload test cases YAML", type=["yaml", "yml"], key="test_case_yaml_upload")
    if st.button("Import Uploaded Test Cases", disabled=uploaded is None, key="test_case_yaml_import"):
        try:
            contents = uploaded.getvalue().decode("utf-8")
            prepared = parse_import_cases(contents)
            created_ids = db.create_test_cases_bulk(prepared)
            st.success(f"Imported {len(created_ids)} test case(s).")
            st.rerun()
        except UnicodeDecodeError:
            st.error("The import file must use UTF-8 text encoding.")
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not import PromptLab test cases")
            st.error("Could not import the file. No test cases were imported. Check the YAML and database, then try again.")

    if st.button("Load Sample Test Cases", key="test_case_load_sample"):
        sample_path = Path(__file__).resolve().parent.parent / "data" / "test_cases.yaml"
        try:
            prepared = parse_import_cases(sample_path.read_text(encoding="utf-8"))
            created_ids = db.create_test_cases_bulk(prepared)
            st.success(f"Loaded {len(created_ids)} sample test case(s).")
            st.rerun()
        except FileNotFoundError:
            st.error("The sample test-case file is not available in this project.")
        except ValueError as exc:
            st.error(_humanize_error(exc))
        except Exception:
            _LOGGER.exception("Could not load sample PromptLab test cases")
            st.error("Could not load the sample cases. No test cases were imported.")

    if not cases:
        st.info("Create test cases before exporting them.")
        return
    serialized = yaml.safe_dump(export_cases(cases), sort_keys=False, allow_unicode=True)
    st.download_button(
        "Download test cases YAML",
        data=serialized,
        file_name=f"test_cases_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml",
        mime="text/yaml",
        key="test_case_yaml_export",
    )


def render():
    if st is None:
        raise RuntimeError("Streamlit is required to render the Test Cases page. Install requirements.txt.")
    st.title("Test Cases & Suites")
    st.caption("Define what to test, group related cases into suites, and run evaluations against a saved prompt version.")
    try:
        cases = db.get_all_test_cases()
        suites = db.get_all_test_suites()
        prompts = db.get_all_prompts()
    except Exception:
        _LOGGER.exception("Could not load PromptLab test management data")
        st.error("Could not load test cases or suites. Check database availability and try again.")
        return

    suite_members = _load_suite_members(suites)
    suites_by_id = {suite["id"]: dict(suite, _members=suite_members.get(suite["id"], [])) for suite in suites}
    membership_names = {}
    for suite in suites:
        for case in suite_members.get(suite["id"], []):
            membership_names.setdefault(case["id"], []).append(suite.get("name") or "Untitled suite")

    prompt_ids = [prompt["id"] for prompt in prompts]
    context_status, _ = consume_selection_context(
        st.session_state, "test_cases_prompt_id", "test_cases_evaluation_prompt_id", prompt_ids,
    )
    prompt_context_unavailable = context_status == "unavailable"
    if prompt_context_unavailable:
        st.error("The prompt version selected in Prompt Workspace is no longer available. Choose a saved version below.")
    elif context_status == "applied":
        st.info("Prompt version context was carried over from Prompt Workspace. Test cases remain reusable across versions.")

    selected_prompt = None
    if prompts:
        prompt_by_id = {prompt["id"]: prompt for prompt in prompts}
        stored_prompt_id = st.session_state.get("test_cases_evaluation_prompt_id")
        if prompt_context_unavailable:
            st.session_state["test_cases_evaluation_prompt_id"] = None
            options = [None] + prompt_ids
            selected_index = 0
        else:
            options = prompt_ids
        if not prompt_context_unavailable and stored_prompt_id not in prompt_ids:
            stored_prompt_id = prompt_ids[0]
        if not prompt_context_unavailable:
            selected_index = options.index(stored_prompt_id)
        selected_prompt_id = st.selectbox(
            "Prompt version for evaluation",
            options,
            index=selected_index,
            format_func=lambda prompt_id: "Choose a saved version" if prompt_id is None else f"Version {prompt_by_id[prompt_id]['version_number']} · {prompt_by_id[prompt_id].get('commit_message') or 'No version note'}",
            key="test_cases_evaluation_prompt_id",
        )
        selected_prompt = prompt_by_id.get(selected_prompt_id) if selected_prompt_id is not None else None
    else:
        st.caption("Save a prompt version on Prompt Workspace before running evaluations.")

    cases_tab, suites_tab, advanced_tab = st.tabs(["Test Cases", "Test Suites", "Advanced: Import / Export"])
    with cases_tab:
        _render_cases_tab(cases, membership_names)
        st.divider()
        st.subheader("Run All Enabled Tests")
        st.caption("Runs every enabled test case, whether or not it belongs to a suite.")
        enabled_cases = [case for case in cases if bool(case.get("enabled", True))]
        if st.button("Run All Enabled Tests", type="primary", disabled=not selected_prompt or not enabled_cases,
                     key="run_all_enabled_test_cases"):
            _run_evaluation("all", selected_prompt, enabled_cases=enabled_cases)

    with suites_tab:
        selected_suite_id = _render_suites_tab(suites, suites_by_id, cases)
        if selected_suite_id is not None:
            selected_suite = suites_by_id.get(selected_suite_id, {})
            members = selected_suite.get("_members", [])
            counts = suite_counts(selected_suite, members)
            st.divider()
            st.subheader("Run Suite")
            st.caption(f"Runs enabled cases from this suite only: {counts['enabled']} of {counts['total']} case(s).")
            if st.button("Run This Suite", type="primary",
                         disabled=not selected_prompt or not counts["suite_enabled"] or not counts["enabled"],
                         key=f"run_test_suite_{selected_suite_id}"):
                _run_evaluation("suite", selected_prompt, suite_id=selected_suite_id)

    with advanced_tab:
        _render_advanced_tab(cases)
