"""
core/db.py
Handles all SQLite database operations for PromptLab.
Every function here uses parameterized queries — never raw string
concatenation — to prevent SQL injection.
"""

import sqlite3
import os
import json
import uuid
import hashlib
from datetime import datetime

# Database file lives inside the data/ folder
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "prompts.db")


def get_connection():
    """Create and return a SQLite connection with foreign keys enabled."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    return conn


def init_db():
    """Create all tables if they don't already exist. Safe to call every app run."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user'))
        )
    """)

    user_columns = {row["name"] for row in cur.execute("PRAGMA table_info(users)")}
    if "recovery_code_hash" not in user_columns:
        cur.execute("ALTER TABLE users ADD COLUMN recovery_code_hash TEXT")
    if "role" not in user_columns:
        legacy_user_count = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if legacy_user_count > 1:
            conn.rollback()
            conn.close()
            raise RuntimeError(
                "Cannot safely migrate legacy accounts: more than one account exists and roles are unknown."
            )
        cur.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' "
            "CHECK (role IN ('admin', 'user'))"
        )
        if legacy_user_count == 1:
            # The legacy database has one unambiguous existing administrator.
            cur.execute("UPDATE users SET role = 'admin'")

    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_admin ON users(role) WHERE role = 'admin'")
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS prevent_second_admin
        BEFORE INSERT ON users
        WHEN NEW.role = 'admin' AND EXISTS (SELECT 1 FROM users WHERE role = 'admin')
        BEGIN
            SELECT RAISE(ABORT, 'administrator already exists');
        END
    """)
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS prevent_user_role_change
        BEFORE UPDATE OF role ON users
        WHEN OLD.role <> NEW.role
        BEGIN
            SELECT RAISE(ABORT, 'account roles cannot be changed');
        END
    """)
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS prevent_admin_deletion
        BEFORE DELETE ON users
        WHEN OLD.role = 'admin'
        BEGIN
            SELECT RAISE(ABORT, 'administrator account cannot be deleted');
        END
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_number INTEGER NOT NULL,
            prompt_text TEXT NOT NULL,
            commit_message TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS test_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            description TEXT,
            input_text TEXT NOT NULL,
            expected_condition TEXT,
            expected_keyword TEXT,
            scoring_type TEXT NOT NULL DEFAULT 'keyword_contains',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS evaluation_runs (
            run_id TEXT PRIMARY KEY,
            prompt_id INTEGER NOT NULL,
            suite_id INTEGER,
            test_case_ids TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            total_tests INTEGER NOT NULL DEFAULT 0,
            passed_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            pass_rate REAL,
            FOREIGN KEY (prompt_id) REFERENCES prompts(id),
            FOREIGN KEY (suite_id) REFERENCES test_suites(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            prompt_id INTEGER NOT NULL,
            test_case_id INTEGER NOT NULL,
            ai_response TEXT,
            passed INTEGER NOT NULL,
            score_reason TEXT,
            execution_time REAL,
            run_at TEXT NOT NULL,
            error_message TEXT,
            status TEXT NOT NULL DEFAULT 'FAIL',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (prompt_id) REFERENCES prompts(id),
            FOREIGN KEY (test_case_id) REFERENCES test_cases(id)
            ,FOREIGN KEY (run_id) REFERENCES evaluation_runs(run_id)
        )
    """)

    case_columns = {row["name"] for row in cur.execute("PRAGMA table_info(test_cases)")}
    if "name" not in case_columns:
        cur.execute("ALTER TABLE test_cases ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    if "enabled" not in case_columns:
        cur.execute("ALTER TABLE test_cases ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    if "updated_at" not in case_columns:
        cur.execute("ALTER TABLE test_cases ADD COLUMN updated_at TEXT")
    cur.execute("UPDATE test_cases SET name = CASE WHEN TRIM(COALESCE(description, '')) <> '' THEN description ELSE 'Test case' END WHERE TRIM(COALESCE(name, '')) = ''")
    cur.execute("UPDATE test_cases SET updated_at = created_at WHERE updated_at IS NULL")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS test_suites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS test_suite_cases (
            suite_id INTEGER NOT NULL,
            test_case_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (suite_id, test_case_id),
            FOREIGN KEY (suite_id) REFERENCES test_suites(id),
            FOREIGN KEY (test_case_id) REFERENCES test_cases(id)
        )
    """)

    # Existing result history is retained with NULL run_id. New runs are
    # explicit and never infer membership from timestamps.
    columns = {row["name"] for row in cur.execute("PRAGMA table_info(test_results)")}
    if "run_id" not in columns:
        cur.execute("ALTER TABLE test_results ADD COLUMN run_id TEXT REFERENCES evaluation_runs(run_id)")
    if "status" not in columns:
        cur.execute("ALTER TABLE test_results ADD COLUMN status TEXT NOT NULL DEFAULT 'FAIL'")
        cur.execute("UPDATE test_results SET status = CASE WHEN ai_response IS NULL THEN 'ERROR' WHEN passed = 1 THEN 'PASS' ELSE 'FAIL' END")
    if "metadata_json" not in columns:
        cur.execute("ALTER TABLE test_results ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
    run_columns = {row["name"] for row in cur.execute("PRAGMA table_info(evaluation_runs)")}
    run_migrations = {
        "metadata_json": "ALTER TABLE evaluation_runs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
        "suite_id": "ALTER TABLE evaluation_runs ADD COLUMN suite_id INTEGER REFERENCES test_suites(id)",
        "started_at": "ALTER TABLE evaluation_runs ADD COLUMN started_at TEXT",
        "completed_at": "ALTER TABLE evaluation_runs ADD COLUMN completed_at TEXT",
        "total_tests": "ALTER TABLE evaluation_runs ADD COLUMN total_tests INTEGER NOT NULL DEFAULT 0",
        "passed_count": "ALTER TABLE evaluation_runs ADD COLUMN passed_count INTEGER NOT NULL DEFAULT 0",
        "failed_count": "ALTER TABLE evaluation_runs ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0",
        "error_count": "ALTER TABLE evaluation_runs ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0",
        "pass_rate": "ALTER TABLE evaluation_runs ADD COLUMN pass_rate REAL",
    }
    for column, statement in run_migrations.items():
        if column not in run_columns:
            cur.execute(statement)
    cur.execute("UPDATE evaluation_runs SET started_at = created_at WHERE started_at IS NULL")
    # Backfill summary fields without changing any existing run IDs or result rows.
    for run in cur.execute("SELECT run_id, test_case_ids, status, created_at FROM evaluation_runs").fetchall():
        try:
            test_ids = json.loads(run["test_case_ids"])
            counts = {row["status"]: row["n"] for row in cur.execute(
                "SELECT status, COUNT(*) AS n FROM test_results WHERE run_id = ? GROUP BY status",
                (run["run_id"],),
            )}
            total = len(test_ids)
            passed = counts.get("PASS", 0)
            failed = counts.get("FAIL", 0)
            errors = counts.get("ERROR", 0)
            rate = round(passed / total * 100, 2) if total else None
            cur.execute("""UPDATE evaluation_runs SET total_tests = ?, passed_count = ?, failed_count = ?,
                           error_count = ?, pass_rate = COALESCE(pass_rate, ?) WHERE run_id = ?""",
                        (total, passed, failed, errors, rate, run["run_id"]))
        except (TypeError, json.JSONDecodeError):
            # Leave malformed legacy snapshot contents untouched for manual recovery.
            continue
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_test_results_run_case ON test_results(run_id, test_case_id)")

    conn.commit()
    conn.close()


# ---------- PROMPTS ----------

def create_prompt_version(prompt_text: str, commit_message: str) -> int:
    """Insert a new prompt version. Auto-increments version_number. Returns new prompt id."""
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise ValueError("Prompt text cannot be empty.")
    if not isinstance(commit_message, str) or not commit_message.strip():
        raise ValueError("Commit message cannot be empty.")
    conn = get_connection()
    try:
        # Serialize version allocation and insert as one write transaction.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT COALESCE(MAX(version_number), 0) AS max_v FROM prompts").fetchone()
        cur = conn.execute(
            "INSERT INTO prompts (version_number, prompt_text, commit_message, created_at) VALUES (?, ?, ?, ?)",
            (row["max_v"] + 1, prompt_text.strip(), commit_message.strip(), datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_all_prompts():
    """Return all prompt versions, newest first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM prompts ORDER BY version_number DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_prompt_by_id(prompt_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- TEST CASES ----------

def create_test_case(description, input_text, expected_condition, expected_keyword, scoring_type,
                     name=None, enabled=True):
    case = {"name": name if name is not None else description or "Test case",
            "input_text": input_text, "expected_keyword": expected_keyword,
            "scoring_type": scoring_type, "enabled": enabled}
    validate_test_case(case)
    expected_keyword = _serialize_expected(case["expected_keyword"])
    now = datetime.now().isoformat()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO test_cases
               (name, description, input_text, expected_condition, expected_keyword, scoring_type, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (case["name"].strip(), description, input_text, expected_condition, expected_keyword,
             scoring_type, int(case["enabled"]), now, now)
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_test_cases_bulk(cases):
    """Validate and insert a test-case batch atomically, returning stable IDs."""
    if not isinstance(cases, list):
        raise ValueError("Test case import must contain a list.")
    prepared = []
    for index, item in enumerate(cases, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Test case {index} must be an object.")
        description = item.get("description", "")
        name = item.get("name") or description or "Test case"
        expected_condition = item.get("expected_condition", "")
        if not isinstance(description, str) or not isinstance(expected_condition, str):
            raise ValueError(f"Test case {index}: description and expected result must be text.")
        case = {
            "name": name,
            "input_text": item.get("input_text"),
            "expected_keyword": item.get("expected_keyword"),
            "scoring_type": item.get("scoring_type", "keyword_contains"),
            "enabled": item.get("enabled", True),
        }
        try:
            validate_test_case(case)
        except ValueError as exc:
            raise ValueError(f"Test case {index}: {exc}") from exc
        prepared.append((case["name"].strip(), description, case["input_text"], expected_condition,
                         _serialize_expected(case["expected_keyword"]), case["scoring_type"],
                         int(case["enabled"])))

    if not prepared:
        return []
    conn = get_connection()
    new_ids = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for name, description, input_text, expected_condition, expected, scoring_type, enabled in prepared:
            now = datetime.now().isoformat()
            cursor = conn.execute(
                """INSERT INTO test_cases
                   (name, description, input_text, expected_condition, expected_keyword, scoring_type, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, description, input_text, expected_condition, expected, scoring_type, enabled, now, now),
            )
            new_ids.append(cursor.lastrowid)
        conn.commit()
        return new_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_test_case(case):
    """Reject malformed cases before they reach SQLite or the evaluator."""
    if not isinstance(case, dict):
        raise ValueError("Test case must be an object.")
    if not isinstance(case.get("input_text"), str) or not case["input_text"].strip():
        raise ValueError("Test case input_text is required.")
    if not isinstance(case.get("name"), str) or not case["name"].strip():
        raise ValueError("Test case name is required.")
    if not isinstance(case.get("enabled", True), (bool, int)) or case.get("enabled", True) not in (True, False, 0, 1):
        raise ValueError("Test case enabled must be true or false.")
    from core.scorer import validate_evaluation_config
    case["expected_keyword"] = validate_evaluation_config(
        case.get("expected_keyword"), case.get("scoring_type"))


def _serialize_expected(expected):
    return json.dumps(expected, ensure_ascii=False, sort_keys=True) if isinstance(expected, (list, dict)) else expected


def get_all_test_cases():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM test_cases ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_test_case(test_case_id, description, input_text, expected_condition, expected_keyword, scoring_type,
                     name=None, enabled=None):
    case = {"name": name if name is not None else description or "Test case",
            "input_text": input_text, "expected_keyword": expected_keyword,
            "scoring_type": scoring_type, "enabled": enabled if enabled is not None else True}
    validate_test_case(case)
    expected_keyword = _serialize_expected(case["expected_keyword"])
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE test_cases
               SET name = ?, description = ?, input_text = ?, expected_condition = ?,
                   expected_keyword = ?, scoring_type = ?, enabled = COALESCE(?, enabled), updated_at = ?
               WHERE id = ?""",
            (case["name"].strip(), description, input_text, expected_condition, expected_keyword, scoring_type,
             int(enabled) if enabled is not None else None, datetime.now().isoformat(), test_case_id)
        )
        if not cur.rowcount:
            raise ValueError("Test case does not exist.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_test_case(test_case_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM test_cases WHERE id = ?", (test_case_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_test_case_enabled(test_case_id, enabled):
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean.")
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE test_cases SET enabled = ?, updated_at = ? WHERE id = ?",
                           (int(enabled), datetime.now().isoformat(), test_case_id))
        conn.commit()
        if not cur.rowcount:
            raise ValueError("Test case does not exist.")
    finally:
        conn.close()


def create_test_suite(name, description="", test_case_ids=None, enabled=True):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Test suite name is required.")
    if not isinstance(enabled, bool):
        raise ValueError("Test suite enabled must be true or false.")
    test_case_ids = list(test_case_ids or [])
    _validate_suite_case_ids(test_case_ids)
    now = datetime.now().isoformat()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("INSERT INTO test_suites (name, description, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (name.strip(), description, int(enabled), now, now))
        suite_id = cur.lastrowid
        _insert_suite_case_rows(conn, suite_id, test_case_ids)
        conn.commit()
        return suite_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_suite_case_ids(test_case_ids):
    if any(not isinstance(case_id, int) or isinstance(case_id, bool) or case_id <= 0 for case_id in test_case_ids):
        raise ValueError("Suite references must be positive test case IDs.")
    if len(set(test_case_ids)) != len(test_case_ids):
        raise ValueError("Test suite contains duplicate test case IDs.")
    if test_case_ids:
        conn = get_connection()
        try:
            found = {row[0] for row in conn.execute(
                "SELECT id FROM test_cases WHERE id IN (%s)" % ",".join("?" for _ in test_case_ids),
                test_case_ids,
            )}
        finally:
            conn.close()
        if found != set(test_case_ids):
            raise ValueError("Test suite references a test case that does not exist.")


def _insert_suite_case_rows(conn, suite_id, test_case_ids):
    conn.executemany("INSERT INTO test_suite_cases (suite_id, test_case_id, position) VALUES (?, ?, ?)",
                     [(suite_id, case_id, position) for position, case_id in enumerate(test_case_ids)])


def set_test_suite_cases(suite_id, test_case_ids):
    test_case_ids = list(test_case_ids)
    _validate_suite_case_ids(test_case_ids)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM test_suites WHERE id = ?", (suite_id,)).fetchone():
            raise ValueError("Test suite does not exist.")
        conn.execute("DELETE FROM test_suite_cases WHERE suite_id = ?", (suite_id,))
        _insert_suite_case_rows(conn, suite_id, test_case_ids)
        conn.execute("UPDATE test_suites SET updated_at = ? WHERE id = ?", (datetime.now().isoformat(), suite_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_test_case_to_suite(suite_id, test_case_id):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM test_suites WHERE id = ?", (suite_id,)).fetchone():
            raise ValueError("Test suite does not exist.")
        if not conn.execute("SELECT 1 FROM test_cases WHERE id = ?", (test_case_id,)).fetchone():
            raise ValueError("Test case does not exist.")
        row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM test_suite_cases WHERE suite_id = ?", (suite_id,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO test_suite_cases (suite_id, test_case_id, position) VALUES (?, ?, ?)",
                     (suite_id, test_case_id, row[0]))
        conn.execute("UPDATE test_suites SET updated_at = ? WHERE id = ?", (datetime.now().isoformat(), suite_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_test_case_from_suite(suite_id, test_case_id):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM test_suite_cases WHERE suite_id = ? AND test_case_id = ?", (suite_id, test_case_id))
        conn.execute("UPDATE test_suites SET updated_at = ? WHERE id = ?", (datetime.now().isoformat(), suite_id))
        conn.commit()
    finally:
        conn.close()


def get_test_suite(suite_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM test_suites WHERE id = ?", (suite_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_test_suites():
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM test_suites ORDER BY id")]
    finally:
        conn.close()


def update_test_suite(suite_id, name, description=""):
    """Update suite details without changing its stable ID or run history."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Test suite name is required.")
    if not isinstance(description, str):
        raise ValueError("Test suite description must be text.")
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE test_suites SET name = ?, description = ?, updated_at = ? WHERE id = ?",
            (name.strip(), description.strip(), datetime.now().isoformat(), suite_id),
        )
        conn.commit()
        if not cur.rowcount:
            raise ValueError("Test suite does not exist.")
    finally:
        conn.close()


def set_test_suite_enabled(suite_id, enabled):
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean.")
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE test_suites SET enabled = ?, updated_at = ? WHERE id = ?",
                           (int(enabled), datetime.now().isoformat(), suite_id))
        conn.commit()
        if not cur.rowcount:
            raise ValueError("Test suite does not exist.")
    finally:
        conn.close()


def get_test_cases_for_suite(suite_id, enabled_only=True):
    conn = get_connection()
    try:
        suite = conn.execute("SELECT id FROM test_suites WHERE id = ?", (suite_id,)).fetchone()
        if suite is None:
            raise ValueError("Test suite does not exist.")
        query = """SELECT tc.* FROM test_cases tc
                   JOIN test_suite_cases tsc ON tsc.test_case_id = tc.id
                   WHERE tsc.suite_id = ?"""
        if enabled_only:
            query += " AND tc.enabled = 1"
        query += " ORDER BY tsc.position, tc.id"
        return [dict(row) for row in conn.execute(query, (suite_id,))]
    finally:
        conn.close()


# ---------- TEST RESULTS ----------

def create_evaluation_run(prompt_id, test_case_ids, metadata=None, suite_id=None):
    """Create a unique run record with a suite reference and reproducibility snapshot."""
    if not get_prompt_by_id(prompt_id):
        raise ValueError("Prompt version does not exist.")
    if not isinstance(test_case_ids, list) or any(not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0 for cid in test_case_ids):
        raise ValueError("Evaluation suite must contain valid test case IDs.")
    if len(set(test_case_ids)) != len(test_case_ids):
        raise ValueError("Evaluation suite contains duplicate test case IDs.")
    if test_case_ids:
        conn = get_connection()
        try:
            found = {row[0] for row in conn.execute(
                "SELECT id FROM test_cases WHERE id IN (%s)" % ",".join("?" for _ in test_case_ids),
                test_case_ids,
            )}
        finally:
            conn.close()
        if found != set(test_case_ids):
            raise ValueError("Evaluation suite contains a test case that does not exist.")
    if suite_id is not None:
        suite = get_test_suite(suite_id)
        if suite is None:
            raise ValueError("Test suite does not exist.")
        if not suite["enabled"]:
            raise ValueError("Test suite is disabled and cannot be evaluated.")
        suite_case_ids = {case["id"] for case in get_test_cases_for_suite(suite_id, enabled_only=True)}
        if suite_case_ids != set(test_case_ids):
            raise ValueError("Run test cases do not match the enabled membership of this suite.")
    run_id = str(uuid.uuid4())
    started_at = datetime.now().isoformat()
    metadata = dict(metadata or {})
    metadata.setdefault("started_at", started_at)
    if suite_id is not None:
        metadata.setdefault("suite_snapshot", {key: suite.get(key) for key in ("id", "name", "description")})
    conn = get_connection()
    try:
        conn.execute("""INSERT INTO evaluation_runs
                     (run_id, prompt_id, suite_id, test_case_ids, created_at, started_at, status,
                      metadata_json, total_tests)
                     VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                     (run_id, prompt_id, suite_id, json.dumps(test_case_ids), started_at, started_at,
                      json.dumps(metadata, ensure_ascii=False, sort_keys=True), len(test_case_ids)))
        conn.commit()
    finally:
        conn.close()
    return run_id


def finish_evaluation_run(run_id, status=None):
    """Finalize a run and atomically derive its counts and pass rate from result rows."""
    allowed = {None, "completed", "completed_with_errors", "failed", "partial"}
    if status not in allowed:
        raise ValueError("Invalid final evaluation run status.")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT test_case_ids, status FROM evaluation_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Evaluation run does not exist.")
        if run["status"] != "running":
            conn.commit()
            return get_evaluation_run(run_id)
        test_case_ids = json.loads(run["test_case_ids"])
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM test_results WHERE run_id = ? GROUP BY status", (run_id,))}
        total = len(test_case_ids)
        passed = counts.get("PASS", 0)
        failed = counts.get("FAIL", 0)
        errors = counts.get("ERROR", 0)
        result_count = passed + failed + errors
        if status is None:
            status = "failed" if result_count != total else "completed_with_errors" if errors else "completed"
        elif status in {"completed", "completed_with_errors"}:
            if result_count != total:
                status = "failed"
            elif errors:
                status = "completed_with_errors"
            else:
                status = "completed"
        pass_rate = round((passed / total) * 100, 2) if total else None
        conn.execute("""UPDATE evaluation_runs SET status = ?, completed_at = ?, total_tests = ?,
                       passed_count = ?, failed_count = ?, error_count = ?, pass_rate = ?
                       WHERE run_id = ?""",
                     (status, datetime.now().isoformat(), total, passed, failed, errors, pass_rate, run_id))
        conn.commit()
        return get_evaluation_run(run_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode_evaluation_run(row):
    result = dict(row)
    result["test_case_ids"] = json.loads(result["test_case_ids"])
    result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
    result["summary"] = {
        "total": result.get("total_tests", 0),
        "passed": result.get("passed_count", 0),
        "failed": result.get("failed_count", 0),
        "errors": result.get("error_count", 0),
        "pass_rate": result.get("pass_rate"),
    }
    return result


def get_evaluation_run(run_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return _decode_evaluation_run(row)
    finally:
        conn.close()


def get_evaluation_runs_for_prompt(prompt_id):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM evaluation_runs WHERE prompt_id = ? ORDER BY started_at DESC, rowid DESC",
                            (prompt_id,)).fetchall()
        return [_decode_evaluation_run(row) for row in rows]
    finally:
        conn.close()


def get_dashboard_snapshot(recent_run_limit=10):
    """Read lightweight dashboard counts and recent run summaries in one query session."""
    if not isinstance(recent_run_limit, int) or isinstance(recent_run_limit, bool) or recent_run_limit < 1:
        raise ValueError("Recent run limit must be a positive integer.")
    conn = get_connection()
    try:
        counts = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM prompts) AS prompt_versions,
                (SELECT COUNT(*) FROM test_cases) AS test_cases,
                (SELECT COUNT(*) FROM test_cases WHERE enabled = 1) AS enabled_test_cases,
                (SELECT COUNT(*) FROM test_suites) AS test_suites,
                (SELECT COUNT(*) FROM test_suites WHERE enabled = 1) AS enabled_suites,
                (SELECT COUNT(*) FROM evaluation_runs) AS evaluation_runs,
                (SELECT COUNT(*) FROM evaluation_runs WHERE status <> 'running') AS finalized_runs
        """).fetchone()
        rows = conn.execute(
            "SELECT evaluation_runs.*, prompts.version_number AS prompt_version_number, "
            "prompts.commit_message AS prompt_version_note FROM evaluation_runs "
            "LEFT JOIN prompts ON prompts.id = evaluation_runs.prompt_id "
            "ORDER BY COALESCE(evaluation_runs.started_at, evaluation_runs.created_at) DESC, "
            "evaluation_runs.rowid DESC LIMIT ?",
            (recent_run_limit,),
        ).fetchall()
        prompts = conn.execute(
            "SELECT id, version_number, commit_message, created_at FROM prompts ORDER BY version_number DESC LIMIT 5"
        ).fetchall()
        cases = conn.execute(
            "SELECT id, name, description, created_at, updated_at FROM test_cases "
            "ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 5"
        ).fetchall()
        suites = conn.execute(
            "SELECT id, name, description, enabled, created_at, updated_at FROM test_suites "
            "ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 5"
        ).fetchall()
        return {
            "counts": dict(counts),
            "recent_runs": [_decode_evaluation_run(row) for row in rows],
            "recent_prompts": [dict(row) for row in prompts],
            "recent_test_cases": [dict(row) for row in cases],
            "recent_suites": [dict(row) for row in suites],
        }
    finally:
        conn.close()


def get_evaluation_runs_for_suite(suite_id):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM evaluation_runs WHERE suite_id = ? ORDER BY started_at DESC, rowid DESC",
                            (suite_id,)).fetchall()
        return [_decode_evaluation_run(row) for row in rows]
    finally:
        conn.close()


def get_latest_completed_run(prompt_id=None, suite_id=None, include_errors=True):
    """Return the newest finalized run; never infer or select a regression baseline."""
    statuses = ("completed", "completed_with_errors") if include_errors else ("completed",)
    clauses = ["status IN (%s)" % ",".join("?" for _ in statuses)]
    params = list(statuses)
    if prompt_id is not None:
        clauses.append("prompt_id = ?")
        params.append(prompt_id)
    if suite_id is not None:
        clauses.append("suite_id = ?")
        params.append(suite_id)
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM evaluation_runs WHERE " + " AND ".join(clauses) +
                           " ORDER BY started_at DESC, rowid DESC LIMIT 1", params).fetchone()
        return _decode_evaluation_run(row) if row else None
    finally:
        conn.close()


def get_results_for_run(run_id):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM test_results WHERE run_id = ? ORDER BY test_case_id", (run_id,)).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
            evaluation = result["metadata"].get("evaluation")
            if isinstance(evaluation, dict):
                result["evaluation"] = evaluation
                result["expected"] = evaluation.get("expected")
                result["actual"] = evaluation.get("actual")
                result["evaluation_method"] = evaluation.get("evaluation_method")
            results.append(result)
        return results
    finally:
        conn.close()


def save_test_result(prompt_id, test_case_id, ai_response, passed, score_reason,
                      execution_time, error_message=None, run_id=None, status=None, metadata=None):
    status = status or ("ERROR" if error_message and ai_response is None else "PASS" if passed else "FAIL")
    if status not in {"PASS", "FAIL", "ERROR"}:
        raise ValueError("Result status must be PASS, FAIL, or ERROR.")
    conn = get_connection()
    try:
        if run_id is not None:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT prompt_id, test_case_ids, status FROM evaluation_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Evaluation run does not exist.")
            if run["status"] != "running":
                raise ValueError("Evaluation run is already finalized; results cannot be added.")
            if run["prompt_id"] != prompt_id or test_case_id not in json.loads(run["test_case_ids"]):
                raise ValueError("Result does not belong to this evaluation run and suite.")
        conn.execute(
            """INSERT INTO test_results
               (run_id, prompt_id, test_case_id, ai_response, passed, score_reason,
                execution_time, run_at, error_message, status, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, prompt_id, test_case_id, ai_response, int(passed), score_reason,
             execution_time, datetime.now().isoformat(), error_message, status,
             json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True))
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_results_for_prompt(prompt_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT tr.*, tc.description, tc.input_text, tc.expected_condition, tc.expected_keyword
           FROM test_results tr
           JOIN test_cases tc ON tr.test_case_id = tc.id
           WHERE tr.prompt_id = ?
           ORDER BY tr.run_at DESC""",
        (prompt_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_result_per_test_case(prompt_id):
    """For a given prompt version, return only the MOST RECENT result per test case."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT tr.* FROM test_results tr
           INNER JOIN (
               SELECT test_case_id, MAX(id) AS latest_id
               FROM test_results
               WHERE prompt_id = ?
               GROUP BY test_case_id
           ) latest
           ON tr.id = latest.latest_id
           WHERE tr.prompt_id = ?""",
        (prompt_id, prompt_id)
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]
