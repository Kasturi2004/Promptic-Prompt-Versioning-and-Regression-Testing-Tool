"""Deterministic dashboard view-model helpers, independent of Streamlit."""


def build_dashboard_model(snapshot):
    counts = snapshot.get("counts") or {}
    recent_runs = list(snapshot.get("recent_runs") or [])
    recent_runs.sort(
        key=lambda run: (run.get("started_at") or run.get("created_at") or "", run.get("run_id", "")),
        reverse=True,
    )
    latest_run = recent_runs[0] if recent_runs else None
    latest_finalized_run = next((run for run in recent_runs if run.get("status") != "running"), None)
    prompt_versions = int(counts.get("prompt_versions", 0) or 0)
    test_case_count = int(counts.get("test_cases", 0) or 0)
    enabled_cases = int(counts.get("enabled_test_cases", 0) or 0)

    if not prompt_versions:
        next_action = {"page": "Prompt Workspace", "label": "Create your first prompt",
                       "message": "Create a prompt version to begin evaluating prompt behavior."}
    elif not test_case_count:
        next_action = {"page": "Test Cases", "label": "Add test cases",
                       "message": "Test cases define how PromptLab checks a prompt response."}
    elif not enabled_cases:
        next_action = {"page": "Test Cases", "label": "Enable test cases",
                       "message": "All test cases are disabled. Enable at least one before evaluating."}
    elif not int(counts.get("evaluation_runs", len(recent_runs)) or 0):
        next_action = {"page": "Run Evaluation", "label": "Run your first evaluation",
                       "message": "Your prompt and enabled tests are ready. Run an evaluation to create results."}
    else:
        next_action = {"page": "Results", "label": "Review the latest results",
                       "message": "An evaluation run is available. Review its outcomes and execution errors."}

    return {
        "metrics": {
            "prompt_versions": prompt_versions,
            "enabled_test_cases": enabled_cases,
            "test_suites": int(counts.get("test_suites", 0) or 0),
            "enabled_suites": int(counts.get("enabled_suites", 0) or 0),
            "evaluation_runs": int(counts.get("evaluation_runs", len(recent_runs)) or 0),
        },
        "latest_run": latest_run,
        "latest_finalized_run": latest_finalized_run,
        "recent_runs": recent_runs,
        "recent_prompts": list(snapshot.get("recent_prompts") or []),
        "next_action": next_action,
        "can_compare": int(counts.get("finalized_runs", sum(run.get("status") != "running" for run in recent_runs)) or 0) >= 2,
    }


def build_recent_activity(prompts, test_cases, suites, runs, limit=8):
    """Build a concise activity list from existing creation/update/run timestamps."""
    events = []
    for prompt in prompts:
        events.append({"timestamp": prompt.get("created_at"), "kind": "Prompt version saved",
                       "detail": f"Version {prompt.get('version_number', '?')} - {prompt.get('commit_message') or 'No version note'}"})
    for case in test_cases:
        created, updated = case.get("created_at"), case.get("updated_at")
        changed = updated and created and updated != created
        events.append({"timestamp": updated or created, "kind": "Test case updated" if changed else "Test case created",
                       "detail": case.get("name") or case.get("description") or f"Test case {case.get('id', '?')}"})
    for suite in suites:
        created, updated = suite.get("created_at"), suite.get("updated_at")
        changed = updated and created and updated != created
        events.append({"timestamp": updated or created, "kind": "Test suite updated" if changed else "Test suite created",
                       "detail": suite.get("name") or "Untitled suite"})
    for run in runs:
        events.append({"timestamp": run.get("completed_at") or run.get("started_at") or run.get("created_at"),
                       "kind": "Evaluation run " + str(run.get("status", "created")).replace("_", " "),
                       "detail": run.get("run_id")})
    events = [event for event in events if event.get("timestamp")]
    return sorted(events, key=lambda event: event["timestamp"], reverse=True)[:max(0, limit)]
