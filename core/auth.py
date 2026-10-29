"""Password hashing and persistent authentication helpers."""

import bcrypt
import sqlite3
from datetime import datetime

from core.db import get_connection


def hash_password(plain_password: str) -> str:
    """Hash a password for storage as a SQLite TEXT value."""
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a password against a stored bcrypt hash, failing closed."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, AttributeError, TypeError):
        return False


def _validate_credentials(username, plain_password):
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Username cannot be empty.")
    if not isinstance(plain_password, str) or len(plain_password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    return username.strip()


def user_exists(username: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        return row is not None
    finally:
        conn.close()


def get_user_role(username: str):
    """Return a user's persisted role, never a client-supplied role value."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        return row["role"] if row else None
    finally:
        conn.close()


def is_admin(username: str) -> bool:
    return get_user_role(username) == "admin"


def admin_exists() -> bool:
    """Return whether the installation's single administrator exists."""
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def any_users_exist() -> bool:
    """Compatibility alias retained for existing callers."""
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def create_first_admin(username: str, plain_password: str) -> int:
    """Atomically create the bootstrap account exactly once.

    The immediate write lock makes the administrator check and insert one
    serialized operation, including when two app processes submit setup concurrently.
    """
    username = _validate_credentials(username, plain_password)
    password_hash = hash_password(plain_password)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone() is not None:
            raise ValueError("Admin account setup is already complete.")
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, 'admin')",
            (username, password_hash, datetime.now().isoformat()),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_user(username: str, plain_password: str) -> int:
    """Create a normal account; this public signup path cannot assign admin."""
    username = _validate_credentials(username, plain_password)
    password_hash = hash_password(plain_password)
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, 'user')",
            (username, password_hash, datetime.now().isoformat()),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("That username is already in use.") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_user_as_admin(admin_username: str, username: str, plain_password: str) -> int:
    """Create a normal account through the admin-only account management path."""
    username = _validate_credentials(username, plain_password)
    password_hash = hash_password(plain_password)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        actor = conn.execute("SELECT role FROM users WHERE username = ?", (admin_username,)).fetchone()
        if actor is None or actor["role"] != "admin":
            raise PermissionError("Only the administrator can manage accounts.")
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, 'user')",
            (username, password_hash, datetime.now().isoformat()),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def authenticate_user(username: str, plain_password: str):
    """Return the persisted role for valid credentials, otherwise None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT password_hash, role FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not verify_password(plain_password, row["password_hash"]):
        return None
    return row["role"]


def authenticate(username: str, plain_password: str) -> bool:
    """Return True only when a persisted user's password matches."""
    return authenticate_user(username, plain_password) is not None


def clear_authentication_state(session_state):
    """Clear authentication markers without disturbing other Streamlit state."""
    session_state["authenticated"] = False
    session_state.pop("username", None)
    session_state.pop("role", None)


def authentication_state(session_state, admin_exists_fn=None, user_exists_fn=None, user_role_fn=None):
    """Resolve setup/login/authenticated state from persistent account data.

    Injected readers keep this policy simple to test without Streamlit.
    """
    admin_exists_fn = admin_exists_fn or admin_exists
    role_reader = user_role_fn or get_user_role
    if not admin_exists_fn():
        clear_authentication_state(session_state)
        return "setup"
    username = session_state.get("username")
    if session_state.get("authenticated") and isinstance(username, str):
        role = role_reader(username)
        if role in {"admin", "user"}:
            # Refresh this marker from SQLite on each app rerun; ignore any
            # role value retained or altered in session state.
            session_state["role"] = role
            return "authenticated"
        if user_exists_fn and user_exists_fn(username):
            session_state["role"] = "user"
            return "authenticated"
    clear_authentication_state(session_state)
    return "login"
