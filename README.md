# PromptLab

PromptLab is a local Streamlit application for versioning prompts, managing test cases and suites, evaluating prompts with Gemini, reviewing results, and comparing evaluation runs.

## Requirements

- Python 3.10 or newer
- A Gemini API key to run live evaluations

## Run locally (Windows PowerShell)

From the project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and replace the placeholder with your Gemini API key. Keep `.env` private; it is excluded by `.gitignore`.

Start PromptLab:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

On first launch, PromptLab asks you to create the local account used to access the app. Its SQLite database and local settings are stored under `data/`.

During administrator setup, PromptLab displays a one-time recovery code. Save it somewhere safe; it is required to reset the administrator password from the login screen, and a successful reset invalidates the code. Passwords can also be changed while signed in from **Settings** by entering the current password.

## Project structure

- `app.py` — Streamlit entry point
- `core/` — application and business logic
- `ui/` — Streamlit pages and presentation helpers
- `tests/` — automated tests
- `scripts/` — manual developer and demo scripts
- `data/` — local database and test-case sample data

## Tests

Install the requirements above, then run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q app.py core ui tests scripts
```

Automated tests use isolated databases and mocked provider responses; they do not call Gemini. Demo scripts are manual utilities, not tests. Run them explicitly from the project root with `python -m scripts.demo_diff`, `python -m scripts.demo_phase7`, or `python -m scripts.demo_phase9`. The phase 7 and phase 9 demos make live Gemini calls and write to the configured local database.
