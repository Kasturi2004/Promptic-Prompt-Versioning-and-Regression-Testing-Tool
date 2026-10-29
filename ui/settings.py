"""Account and administrator-only application settings."""

import streamlit as st

from core.ai_client import _MODEL_NAME, is_configured
from core.auth import authenticate, create_user_as_admin, hash_password, is_admin
from core.db import get_connection
from core.settings import load_settings, save_settings


def render():
    st.title("Settings")
    current_username = st.session_state.get("username")
    current_user_is_admin = is_admin(current_username)

    if current_user_is_admin:
        st.subheader("Test Execution")
        settings = load_settings()
        new_max = st.slider(
            "Default max concurrent requests",
            min_value=1, max_value=5,
            value=settings["max_concurrent_requests"],
            help="Used as the default on the Run Evaluation page. Lower = safer against rate limits.",
            key="settings_max_concurrent",
        )
        if st.button("Save Settings", key="settings_save_button"):
            settings["max_concurrent_requests"] = new_max
            try:
                save_settings(settings, actor_username=current_username)
                st.success("Settings saved.")
            except PermissionError:
                st.error("Only the administrator can change application settings.")

        st.divider()
        st.subheader("AI Model")
        st.write(f"**Current model:** `{_MODEL_NAME}`")
        configured_label = "Yes" if is_configured() else "No — check your .env file"
        st.write(f"**API key configured:** {configured_label}")
        st.caption("To change the model, edit `_MODEL_NAME` in core/ai_client.py.")
        st.divider()

    st.subheader("Change Password")
    old_password = st.text_input("Current password", type="password", key="settings_old_pw")
    new_password = st.text_input("New password", type="password", key="settings_new_pw")
    confirm_password = st.text_input("Confirm new password", type="password", key="settings_confirm_pw")

    if st.button("Update Password", key="settings_update_pw_button"):
        if not authenticate(current_username, old_password):
            st.error("Current password is incorrect.")
        elif len(new_password) < 6:
            st.error("New password must be at least 6 characters.")
        elif new_password != confirm_password:
            st.error("New passwords do not match.")
        else:
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
                    (hash_password(new_password), current_username),
                )
                conn.commit()
                st.success("Password updated.")
            finally:
                conn.close()

    if current_user_is_admin:
        st.divider()
        st.subheader("Manage Users")
        st.caption("Create normal user accounts. Only the administrator can manage accounts.")
        new_username = st.text_input("New username", key="settings_new_username")
        new_user_password = st.text_input("New user's password", type="password", key="settings_new_user_pw")
        confirm_new_user_password = st.text_input("Confirm password", type="password", key="settings_confirm_new_user_pw")

        if st.button("Create User", key="settings_create_user_button"):
            if not new_username.strip():
                st.error("Username cannot be empty.")
            elif len(new_user_password) < 6:
                st.error("Password must be at least 6 characters.")
            elif new_user_password != confirm_new_user_password:
                st.error("Passwords do not match.")
            else:
                try:
                    create_user_as_admin(current_username, new_username.strip(), new_user_password)
                    st.success(f"User '{new_username.strip()}' created. They can now log in.")
                except PermissionError:
                    st.error("Only the administrator can manage accounts.")
                except Exception:
                    st.error("That username may already exist, or the account could not be created.")
