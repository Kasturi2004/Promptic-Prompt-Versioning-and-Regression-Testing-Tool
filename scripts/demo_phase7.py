"""Manual Gemini demo; this file is intentionally not named as a test module.

Run explicitly with ``python demo_phase7.py`` only when a live evaluation is
intended. Automated tests use mocked provider responses instead.
"""


def main():
    from core.db import init_db, get_all_prompts, get_all_test_cases
    from core.test_runner import run_tests

    init_db()
    prompts = get_all_prompts()
    if not prompts:
        raise SystemExit("Create a prompt version before running this demo.")
    prompt = prompts[0]
    cases = get_all_test_cases()

    def show_progress(done, total):
        print(f"Progress: {done}/{total}")

    results = run_tests(prompt["id"], prompt["prompt_text"], cases, progress_callback=show_progress)
    for result in results:
        status = result["status"]
        print(f"[{status}] {result['description']} -> {result['score_reason']}")


if __name__ == "__main__":
    main()
