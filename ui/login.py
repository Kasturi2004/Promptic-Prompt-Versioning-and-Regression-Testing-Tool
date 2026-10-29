"""Authentication landing, login, and one-time administrator setup views."""

import logging

import streamlit as st

from core.auth import (
    admin_exists,
    authenticate_user,
    authentication_state,
    clear_authentication_state,
    create_first_admin,
    create_user,
)

logger = logging.getLogger(__name__)

_LANDING = "landing"
_LOGIN = "login"
_CREATE_ADMIN = "create_admin"


def render() -> bool:
    """Render the appropriate auth view, returning True for a valid session."""
    try:
        state = authentication_state(st.session_state)
    except Exception:
        logger.exception("Unable to determine PromptLab authentication state")
        st.title("PromptLab")
        st.error("PromptLab could not access its local account data. Check the installation and try again.")
        return False

    if state == "authenticated":
        return True

    st.title("PromptLab")
    if state == "login":
        notice = st.session_state.pop("login_notice", None)
        if notice:
            st.success(notice)
        if st.session_state.get("auth_view") == "signup":
            _render_signup_form()
        else:
            # Persisted installation state always wins over setup navigation.
            st.session_state["auth_view"] = _LOGIN
            _render_login_form(fresh_install=False)
        return False

    view = st.session_state.get("auth_view", _LANDING)
    if view == _CREATE_ADMIN:
        _render_first_run_setup()
    elif view == _LOGIN:
        _render_login_form(fresh_install=True)
    else:
        st.session_state["auth_view"] = _LANDING
        _render_landing()
    return False


def _render_landing():
    st.subheader("Welcome to PromptLab")
    st.caption("Sign in to continue to your workspace.")
    st.write("Already initialized this installation? Log in with its administrator account.")
    if st.button("Log In", type="primary", key="landing_login_button"):
        st.session_state["auth_view"] = _LOGIN
        st.rerun()
    st.write("Don't have an administrator account for this installation yet?")
    if st.button("Create Administrator Account", key="landing_create_admin_button"):
        # Recheck persistent state on the action as well as in the backend.
        try:
            if admin_exists():
                st.session_state["auth_view"] = _LOGIN
                st.info("Administrator setup is already complete. Please log in.")
            else:
                st.session_state["auth_view"] = _CREATE_ADMIN
            st.rerun()
        except Exception:
            logger.exception("Unable to verify administrator setup state")
            st.error("PromptLab could not access its local account data. Please try again.")


def _render_first_run_setup():
    """Show the setup form only while the persistent installation is uninitialized."""
    try:
        if admin_exists():
            st.session_state["auth_view"] = _LOGIN
            st.info("Administrator setup is already complete. Please log in.")
            _render_login_form(fresh_install=False)
            return
    except Exception:
        logger.exception("Unable to verify first-run setup state")
        st.error("PromptLab could not access its local account data. Please try again.")
        return

    st.subheader("Create Administrator Account")
    st.caption("Set up the administrator for this PromptLab installation.")
    username = st.text_input("Username / Email", key="setup_username")
    password = st.text_input("Password", type="password", key="setup_password")
    confirm = st.text_input("Confirm Password", type="password", key="setup_confirm")

    if st.button("Create Administrator", type="primary", key="setup_button"):
        if not username.strip():
            st.error("Username cannot be empty.")
        elif len(password) < 6:
            st.error("Password must be at least 6 characters.")
        elif password != confirm:
            st.error("Passwords do not match.")
        else:
            try:
                create_first_admin(username.strip(), password)
                clear_authentication_state(st.session_state)
                st.session_state["auth_view"] = _LOGIN
                st.session_state["login_notice"] = "Administrator account created successfully. Please log in."
                st.rerun()
            except ValueError as exc:
                if str(exc) == "Admin account setup is already complete.":
                    clear_authentication_state(st.session_state)
                    st.session_state["auth_view"] = _LOGIN
                    st.session_state["login_notice"] = "Administrator setup is already complete. Please log in."
                    st.rerun()
                st.error(str(exc))
            except Exception:
                logger.exception("Unable to create the initial PromptLab administrator")
                st.error("PromptLab could not create the administrator account. Please try again.")

    if st.button("← Back to Welcome", key="setup_back_button"):
        st.session_state["auth_view"] = _LANDING
        st.rerun()


def _render_login_form(fresh_install: bool):
    st.subheader("Welcome back" if not fresh_install else "Log in")
    st.caption("Sign in to continue to your workspace.")
    username = st.text_input("Email / Username", key="login_username")
    password = st.text_input("Password", type="password", key="login_password")
    if st.button("Log In", type="primary", key="login_button"):
        try:
            # A login attempt during fresh setup should explain how to initialize
            # this installation instead of looking like invalid credentials.
            if not admin_exists():
                st.error("No administrator account has been created for this installation yet. Create the administrator account to continue.")
            elif (role := authenticate_user(username.strip(), password)) is not None:
                st.session_state["authenticated"] = True
                st.session_state["username"] = username.strip()
                st.session_state["role"] = role
                st.rerun()
            else:
                st.error("Invalid username or password.")
        except Exception:
            logger.exception("PromptLab login operation failed")
            st.error("PromptLab could not verify your account. Please try again.")
    if fresh_install and st.button("← Back to Welcome", key="login_back_button"):
        st.session_state["auth_view"] = _LANDING
        st.rerun()
    elif not fresh_install:
        st.write("Don't have an account?")
        if st.button("Sign Up", key="login_signup_button"):
            st.session_state["auth_view"] = "signup"
            st.rerun()


def _render_signup_form():
    """Create a normal account only; role assignment is fixed in core.auth."""
    st.subheader("Create your PromptLab account")
    username = st.text_input("Username / Email", key="signup_username")
    password = st.text_input("Password", type="password", key="signup_password")
    confirm = st.text_input("Confirm Password", type="password", key="signup_confirm")

    if st.button("Create Account", type="primary", key="signup_button"):
        if not username.strip():
            st.error("Username cannot be empty.")
        elif len(password) < 6:
            st.error("Password must be at least 6 characters.")
        elif password != confirm:
            st.error("Passwords do not match.")
        else:
            try:
                if not admin_exists():
                    st.error("This installation must be initialized before user accounts can be created.")
                else:
                    create_user(username.strip(), password)
                    st.session_state["auth_view"] = _LOGIN
                    st.session_state["login_notice"] = "Account created successfully. Please log in."
                    st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception:
                logger.exception("Unable to create a PromptLab user account")
                st.error("PromptLab could not create your account. Please try again.")

    st.write("Already have an account?")
    if st.button("Log In", key="signup_login_button"):
        st.session_state["auth_view"] = _LOGIN
        st.rerun()


def logout():
    """Clear session authentication markers; persisted setup remains complete."""
    clear_authentication_state(st.session_state)
    st.session_state["auth_view"] = _LOGIN
