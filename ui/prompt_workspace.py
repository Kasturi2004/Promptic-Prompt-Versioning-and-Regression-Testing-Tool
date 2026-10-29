"""Streamlit-independent helpers for the prompt workspace."""

from core.db import create_prompt_version
from core.test_runner import validate_prompt_template


def validate_template_for_editor(prompt_text):
    """Reuse execution validation and translate its errors into editor language."""
    try:
        validate_prompt_template(prompt_text)
    except ValueError as exc:
        message = str(exc)
        if "missing required variable {input}" in message:
            return False, "Your prompt template is missing the required {input} variable."
        if "Unsupported or missing template variable(s)" in message:
            return False, "Unsupported template variable detected. PromptLab currently supports {input}."
        if "format specs or conversions" in message:
            return False, "Template variables cannot include formatting or conversion options. Use {input}."
        if "cannot be empty" in message:
            return False, "Enter a prompt template before saving."
        if "unnamed variable" in message:
            return False, "The prompt has an empty template variable. Use {input}."
        return False, "The prompt template has malformed braces. Check each opening and closing brace."
    return True, "Template is valid and ready for evaluation."


def prompt_version_label(prompt):
    note = (prompt.get("commit_message") or "No version note").strip()
    return f"Version {prompt.get('version_number', '?')} · {note}"


def prompt_version_options(prompts):
    """Return a create-new option followed by the existing immutable versions."""
    return [None] + [prompt["id"] for prompt in prompts]


def find_prompt_version(prompts, version_id):
    return next((prompt for prompt in prompts if prompt.get("id") == version_id), None)


def is_prompt_dirty(draft_text, original_text, version_note="", is_new=False):
    if is_new:
        return bool((draft_text or "").strip() or (version_note or "").strip())
    return (draft_text or "") != (original_text or "") or bool((version_note or "").strip())


def save_new_prompt_version(prompt_text, version_note, source_prompt=None, saver=None):
    """Validate and append a new immutable prompt version."""
    text = prompt_text.strip() if isinstance(prompt_text, str) else ""
    note = version_note.strip() if isinstance(version_note, str) else ""
    valid, message = validate_template_for_editor(text)
    if not valid:
        raise ValueError(message)
    if not note:
        raise ValueError("Add a short version note before saving.")
    if source_prompt and text == source_prompt.get("prompt_text", ""):
        raise ValueError("No prompt changes to save. Edit the template before creating another version.")
    return (saver or create_prompt_version)(text, note)


def request_workspace_navigation(session_state, page, **context):
    """Defer sidebar widget changes until app.py recreates its navigation widget."""
    session_state["prompt_workspace_navigation_target"] = page
    session_state.update(context)


def consume_workspace_navigation(session_state, allowed_pages):
    """Return a pending route only when it belongs to the app's navigation map."""
    page = session_state.pop("prompt_workspace_navigation_target", None)
    return page if page in allowed_pages else None


def normalize_workspace_page(page, aliases):
    """Map legacy navigation labels to their current sidebar name."""
    return aliases.get(page, page)


def consume_compare_candidate_context(session_state, available_run_ids):
    """Prefill a requested candidate while deliberately leaving baseline unset."""
    candidate_id = session_state.pop("compare_candidate_pending_run_id", None)
    if candidate_id not in available_run_ids:
        if candidate_id is None:
            return None
        session_state["compare_baseline_run"] = None
        session_state["compare_candidate_run"] = None
        return "unavailable"
    session_state["compare_baseline_run"] = None
    session_state["compare_candidate_run"] = candidate_id
    return candidate_id


def consume_selection_context(session_state, context_key, widget_key, available_values):
    """Apply a pending exact selection before its Streamlit widget is created."""
    value = session_state.pop(context_key, None)
    if value is None:
        return "absent", None
    if value not in available_values:
        return "unavailable", value
    session_state[widget_key] = value
    return "applied", value
