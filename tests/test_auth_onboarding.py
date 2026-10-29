"""Authentication onboarding tests against isolated SQLite databases."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import auth, db
from core import settings as app_settings
from ui import login


class _Rerun(BaseException):
    """Stop one fake Streamlit render after a navigation action."""


class _FakeStreamlit:
    def __init__(self, session_state=None, click=None, inputs=None):
        self.session_state = session_state if session_state is not None else {}
        self.click = click
        self.inputs = inputs or {}
        self.buttons = []
        self.fields = []
        self.messages = []

    def title(self, value): self.messages.append(("title", value))
    def subheader(self, value): self.messages.append(("subheader", value))
    def caption(self, value): self.messages.append(("caption", value))
    def write(self, value): self.messages.append(("write", value))
    def info(self, value): self.messages.append(("info", value))
    def error(self, value): self.messages.append(("error", value))
    def success(self, value): self.messages.append(("success", value))

    def text_input(self, label, key, **kwargs):
        self.fields.append(label)
        return self.inputs.get(key, "")

    def button(self, label, **kwargs):
        self.buttons.append(label)
        return label == self.click

    def rerun(self):
        raise _Rerun()


class AuthOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "auth.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def count_users(self):
        conn = db.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()

    def test_fresh_install_enters_setup(self):
        state = {"authenticated": True, "username": "stale"}
        self.assertFalse(auth.admin_exists())
        self.assertEqual(auth.authentication_state(state), "setup")
        self.assertFalse(state["authenticated"])
        self.assertNotIn("username", state)

    def test_first_admin_is_persisted_hashed_and_requires_login(self):
        user_id = auth.create_first_admin("owner", "safe-password")
        self.assertGreater(user_id, 0)
        self.assertTrue(auth.admin_exists())
        self.assertTrue(auth.authenticate("owner", "safe-password"))
        self.assertFalse(auth.authenticate("owner", "wrong-password"))
        conn = db.get_connection()
        try:
            stored_hash = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertNotEqual(stored_hash, "safe-password")
        self.assertTrue(auth.verify_password("safe-password", stored_hash))
        self.assertEqual(auth.authentication_state({}), "login")

    def test_second_setup_is_denied_even_when_called_directly(self):
        auth.create_first_admin("owner", "safe-password")
        with self.assertRaisesRegex(ValueError, "already complete"):
            auth.create_first_admin("intruder", "other-password")
        self.assertEqual(self.count_users(), 1)

    def test_setup_rejects_bad_credentials_before_writing(self):
        for username, password in (("  ", "safe-password"), ("owner", "short")):
            with self.subTest(username=username, password=password), self.assertRaises(ValueError):
                auth.create_first_admin(username, password)
        self.assertEqual(self.count_users(), 0)

    def test_failed_insert_rolls_back_and_leaves_no_user(self):
        conn = db.get_connection()
        try:
            conn.execute("CREATE TRIGGER reject_first_user BEFORE INSERT ON users BEGIN SELECT RAISE(ABORT, 'blocked'); END")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            auth.create_first_admin("owner", "safe-password")
        self.assertFalse(auth.admin_exists())

    def test_existing_admin_requires_login_and_stale_session_is_cleared(self):
        auth.create_first_admin("owner", "safe-password")
        self.assertEqual(auth.authentication_state({}), "login")
        stale = {"authenticated": True, "username": "deleted-user", "nav_radio": "Dashboard"}
        self.assertEqual(auth.authentication_state(stale), "login")
        self.assertFalse(stale["authenticated"])
        self.assertNotIn("username", stale)
        self.assertEqual(stale["nav_radio"], "Dashboard")

    def test_logout_state_clears_session_but_keeps_account(self):
        auth.create_first_admin("owner", "safe-password")
        state = {"authenticated": True, "username": "owner", "nav_radio": "Results"}
        auth.clear_authentication_state(state)
        self.assertEqual(auth.authentication_state(state), "login")
        self.assertEqual(state["nav_radio"], "Results")

    def test_existing_project_data_survives_first_admin_creation(self):
        prompt_id = db.create_prompt_version("Answer {input}", "initial")
        case_id = db.create_test_case("case", "hello", "", "hello", "contains_keyword")
        suite_id = db.create_test_suite("suite", "", [case_id])
        run_id = db.create_evaluation_run(prompt_id, [case_id], suite_id=suite_id)
        db.save_test_result(prompt_id, case_id, "hello", True, "matched", 0.1,
                            run_id=run_id, status="PASS")
        auth.create_first_admin("owner", "safe-password")
        self.assertEqual(db.get_prompt_by_id(prompt_id)["prompt_text"], "Answer {input}")
        self.assertEqual(db.get_test_cases_for_suite(suite_id)[0]["id"], case_id)
        self.assertEqual(db.get_results_for_run(run_id)[0]["ai_response"], "hello")
        self.assertEqual(db.get_evaluation_run(run_id)["prompt_id"], prompt_id)

    def test_fresh_install_landing_offers_login_and_one_time_initialization(self):
        fake = _FakeStreamlit()
        with patch.object(login, "st", fake):
            self.assertFalse(login.render())
        self.assertIn("Log In", fake.buttons)
        self.assertIn("Create Administrator Account", fake.buttons)
        self.assertIn("Welcome to PromptLab", [value for _, value in fake.messages])
        self.assertNotIn("Create Administrator", fake.buttons)
        self.assertNotIn("Username / Email", fake.fields)

    def test_fresh_install_login_attempt_explains_initialization(self):
        fake = _FakeStreamlit({"auth_view": "login"}, click="Log In")
        with patch.object(login, "st", fake):
            login.render()
        errors = [value for kind, value in fake.messages if kind == "error"]
        self.assertTrue(any("No administrator account has been created" in value for value in errors))
        self.assertIn("← Back to Welcome", fake.buttons)

    def test_landing_create_action_opens_admin_initialization_form(self):
        fake = _FakeStreamlit(click="Create Administrator Account")
        with patch.object(login, "st", fake), self.assertRaises(_Rerun):
            login.render()
        self.assertEqual(fake.session_state["auth_view"], "create_admin")
        next_fake = _FakeStreamlit(fake.session_state)
        with patch.object(login, "st", next_fake):
            login.render()
        self.assertIn("Create Administrator Account", [value for _, value in next_fake.messages])
        self.assertIn("Create Administrator", next_fake.buttons)
        self.assertIn("Username / Email", next_fake.fields)

    def test_successful_setup_returns_to_login_without_authenticating(self):
        state = {"auth_view": "create_admin"}
        fake = _FakeStreamlit(
            state,
            click="Create Administrator",
            inputs={"setup_username": "owner", "setup_password": "safe-password",
                    "setup_confirm": "safe-password"},
        )
        with patch.object(login, "st", fake), self.assertRaises(_Rerun):
            login.render()
        self.assertTrue(auth.admin_exists())
        self.assertFalse(state.get("authenticated", False))
        self.assertEqual(state["auth_view"], "login")

        login_fake = _FakeStreamlit(state)
        with patch.object(login, "st", login_fake):
            login.render()
        self.assertIn("Log In", login_fake.buttons)
        self.assertIn("Administrator account created successfully. Please log in.",
                      [value for kind, value in login_fake.messages if kind == "success"])

    def test_existing_installation_forces_login_and_hides_admin_creation(self):
        auth.create_first_admin("owner", "safe-password")
        fake = _FakeStreamlit({"auth_view": "create_admin"})
        with patch.object(login, "st", fake):
            login.render()
        self.assertIn("Log In", fake.buttons)
        self.assertNotIn("Create Administrator Account", fake.buttons)
        self.assertNotIn("Create Administrator", fake.buttons)
        self.assertEqual(fake.session_state["auth_view"], "login")

    def test_logout_and_new_session_after_initialization_show_login_only(self):
        auth.create_first_admin("owner", "safe-password")
        authenticated_state = {"authenticated": True, "username": "owner"}
        fake = _FakeStreamlit(authenticated_state)
        with patch.object(login, "st", fake):
            login.logout()
        self.assertEqual(authenticated_state["auth_view"], "login")
        self.assertFalse(authenticated_state["authenticated"])

        for fresh_session in (authenticated_state, {}):
            page = _FakeStreamlit(fresh_session)
            with patch.object(login, "st", page):
                login.render()
            self.assertIn("Log In", page.buttons)
            self.assertNotIn("Create Administrator Account", page.buttons)

    def test_signup_creates_hashed_normal_user_and_rejects_duplicate(self):
        auth.create_first_admin("owner", "safe-password")
        user_id = auth.create_user("member", "member-password")
        self.assertGreater(user_id, 0)
        self.assertEqual(auth.get_user_role("member"), "user")
        self.assertEqual(auth.authenticate_user("owner", "safe-password"), "admin")
        self.assertEqual(auth.authenticate_user("member", "member-password"), "user")
        self.assertIsNone(auth.authenticate_user("member", "incorrect"))
        conn = db.get_connection()
        try:
            password_hash = conn.execute(
                "SELECT password_hash FROM users WHERE username = 'member'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotEqual(password_hash, "member-password")
        self.assertTrue(auth.verify_password("member-password", password_hash))
        with self.assertRaisesRegex(ValueError, "already in use"):
            auth.create_user("member", "another-password")
        with self.assertRaises(TypeError):
            auth.create_user("attacker", "safe-password", role="admin")
        self.assertEqual(self.count_users(), 2)

    def test_single_admin_is_enforced_by_backend_and_database(self):
        auth.create_first_admin("owner", "safe-password")
        auth.create_user("member", "member-password")
        with self.assertRaisesRegex(ValueError, "already complete"):
            auth.create_first_admin("second-admin", "safe-password")
        with self.assertRaises(PermissionError):
            auth.create_user_as_admin("member", "another", "safe-password")
        with self.assertRaises(PermissionError):
            app_settings.save_settings({"max_concurrent_requests": 1}, actor_username="member")

        conn = db.get_connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at, role) "
                    "VALUES ('direct-admin', 'hash', 'now', 'admin')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE users SET role = 'admin' WHERE username = 'member'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM users WHERE username = 'owner'")
            conn.rollback()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_admin_can_create_only_normal_user_and_update_global_settings(self):
        auth.create_first_admin("owner", "safe-password")
        user_id = auth.create_user_as_admin("owner", "member", "member-password")
        self.assertEqual(auth.get_user_role("member"), "user")
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(app_settings, "SETTINGS_PATH", str(Path(temp) / "settings.json")):
                app_settings.save_settings({"max_concurrent_requests": 4}, actor_username="owner")
                self.assertEqual(app_settings.load_settings()["max_concurrent_requests"], 4)
        self.assertEqual(auth.authenticate_user("owner", "safe-password"), "admin")
        self.assertEqual(auth.authenticate_user("member", "member-password"), "user")
        self.assertGreater(user_id, 0)

    def test_session_role_is_refreshed_from_database_and_logout_clears_it(self):
        auth.create_first_admin("owner", "safe-password")
        auth.create_user("member", "member-password")
        session = {"authenticated": True, "username": "member", "role": "admin"}
        self.assertEqual(auth.authentication_state(session), "authenticated")
        self.assertEqual(session["role"], "user")
        auth.clear_authentication_state(session)
        self.assertNotIn("role", session)
        self.assertEqual(auth.authentication_state(session), "login")

    def test_legacy_single_account_migrates_as_admin_without_losing_data(self):
        legacy_path = str(Path(self.temp.name) / "legacy.db")
        password_hash = auth.hash_password("legacy-password")
        conn = sqlite3.connect(legacy_path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (17, 'legacy-owner', ?, 'then')", (password_hash,))
        conn.execute("CREATE TABLE prompts (id INTEGER PRIMARY KEY, version_number INTEGER NOT NULL, prompt_text TEXT NOT NULL, commit_message TEXT, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO prompts VALUES (8, 1, 'Existing {input}', 'legacy', 'then')")
        conn.commit()
        conn.close()

        with patch.object(db, "DB_PATH", legacy_path):
            db.init_db()
            self.assertEqual(auth.get_user_role("legacy-owner"), "admin")
            self.assertTrue(auth.authenticate("legacy-owner", "legacy-password"))
            prompt = db.get_prompt_by_id(8)
            self.assertEqual(prompt["prompt_text"], "Existing {input}")
            migrated = db.get_connection()
            try:
                row = migrated.execute("SELECT id, password_hash FROM users WHERE username = 'legacy-owner'").fetchone()
                self.assertEqual(row["id"], 17)
                self.assertEqual(row["password_hash"], password_hash)
            finally:
                migrated.close()

    def test_ambiguous_legacy_accounts_are_not_assigned_roles_automatically(self):
        legacy_path = str(Path(self.temp.name) / "ambiguous.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", [(1, "one", "hash1", "t"), (2, "two", "hash2", "t")])
        conn.commit()
        conn.close()
        with patch.object(db, "DB_PATH", legacy_path):
            with self.assertRaisesRegex(RuntimeError, "roles are unknown"):
                db.init_db()
            check = sqlite3.connect(legacy_path)
            try:
                columns = {row[1] for row in check.execute("PRAGMA table_info(users)")}
                self.assertNotIn("role", columns)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            finally:
                check.close()

    def test_signup_ui_creates_user_then_returns_to_login_without_authentication(self):
        auth.create_first_admin("owner", "safe-password")
        state = {}
        login_page = _FakeStreamlit(state, click="Sign Up")
        with patch.object(login, "st", login_page), self.assertRaises(_Rerun):
            login.render()
        self.assertEqual(state["auth_view"], "signup")

        signup_page = _FakeStreamlit(
            state,
            click="Create Account",
            inputs={"signup_username": "member", "signup_password": "member-password",
                    "signup_confirm": "member-password"},
        )
        with patch.object(login, "st", signup_page), self.assertRaises(_Rerun):
            login.render()
        self.assertEqual(auth.get_user_role("member"), "user")
        self.assertFalse(state.get("authenticated", False))
        self.assertEqual(state["auth_view"], "login")
        self.assertNotIn("Choose Role", signup_page.fields)
        self.assertNotIn("Create Administrator", signup_page.buttons)

        returned_login = _FakeStreamlit(state)
        with patch.object(login, "st", returned_login):
            login.render()
        self.assertIn("Account created successfully. Please log in.",
                      [value for kind, value in returned_login.messages if kind == "success"])

    def test_admin_and_user_login_sessions_and_logout_are_distinguished(self):
        auth.create_first_admin("owner", "safe-password")
        auth.create_user("member", "member-password")
        for username, password, role in (
            ("owner", "safe-password", "admin"),
            ("member", "member-password", "user"),
        ):
            state = {}
            page = _FakeStreamlit(state, click="Log In",
                                  inputs={"login_username": username, "login_password": password})
            with patch.object(login, "st", page), self.assertRaises(_Rerun):
                login.render()
            self.assertTrue(state["authenticated"])
            self.assertEqual(state["role"], role)
            self.assertEqual(auth.authentication_state(state), "authenticated")
            with patch.object(login, "st", _FakeStreamlit(state)):
                login.logout()
            self.assertFalse(state["authenticated"])
            self.assertNotIn("role", state)


if __name__ == "__main__":
    unittest.main()
