"""Manual live Gemini demo; invoke explicitly with ``python demo_phase9.py``.

This script creates a prompt version and evaluation records in the configured
local database. It is deliberately excluded from unittest discovery.
"""


def main():
    from core.db import init_db, create_prompt_version, get_all_test_cases
    from core.test_runner import run_tests
    from core.regression import compare_versions, summarize_comparison

    init_db()
    prompt_text = "Summarize the following text in exactly 5 words:\n{input}"
    new_id = create_prompt_version(prompt_text, "Reduced response length")
    print("Created prompt id", new_id)

    cases = get_all_test_cases()

    def show_progress(done, total):
        print(f"Progress: {done}/{total}")

    results = run_tests(new_id, prompt_text, cases, progress_callback=show_progress)
    for result in results:
        print(f"[{result['status']}] {result['description']} -> {result['score_reason']}")

    # Comparisons are keyed by persisted run IDs, and the user chooses the baseline.
    candidate_run_id = results[0]["run_id"] if results else None
    if not candidate_run_id:
        print("\nNo evaluation run was created, so there is nothing to compare.")
        return
    print("\nEnter an existing evaluation run ID to compare against, or press Enter to skip.")
    baseline_run_id = input("Baseline run ID: ").strip()
    if not baseline_run_id:
        print("Comparison skipped; no baseline was selected.")
        return

    print("\n--- COMPARISON: Selected baseline vs candidate run ---")
    comparison = compare_versions(baseline_run_id, candidate_run_id)
    for entry in comparison:
        print(f"{entry['status']:15} | {entry['description']}")

    print("\n--- SUMMARY ---")
    print(summarize_comparison(comparison))


if __name__ == "__main__":
    main()
