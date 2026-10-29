"""
app.py
PromptLab entry point — sidebar navigation, authentication gate,
and page routing.
"""

import streamlit as st
from core.db import init_db
from ui import dashboard, prompt_editor, test_cases, run_tests, results, compare, settings, login
from ui.prompt_workspace import consume_workspace_navigation, normalize_workspace_page

st.set_page_config(page_title="PromptLab", page_icon="🧪", layout="wide")

init_db()

if not login.render():
    st.stop()

# ---- Sidebar navigation (only rendered for authenticated users) ----
st.sidebar.title("🧪 PromptLab")
account_type = "Administrator" if st.session_state.get("role") == "admin" else "User"
st.sidebar.caption(f"{account_type}: **{st.session_state.get('username')}**")
st.sidebar.divider()

PAGES = {
    "Dashboard": dashboard,
    "Prompt Workspace": prompt_editor,
    "Test Cases": test_cases,
    "Run Evaluation": run_tests,
    "Results": results,
    "Compare Runs": compare,
    "Settings": settings,
}
PAGE_ALIASES = {
    "Prompt Versions": "Prompt Workspace",
    "Run Tests": "Run Evaluation",
    "Compare Versions": "Compare Runs",
}

# Keep an existing browser session's selected page valid after the labels change.
current_page = st.session_state.get("nav_radio")
if current_page in PAGE_ALIASES:
    st.session_state["nav_radio"] = normalize_workspace_page(current_page, PAGE_ALIASES)

# Page actions defer navigation until before the sidebar widget is created.
pending_page = consume_workspace_navigation(st.session_state, set(PAGES) | set(PAGE_ALIASES))
if pending_page:
    st.session_state["nav_radio"] = normalize_workspace_page(pending_page, PAGE_ALIASES)

selected_page = st.sidebar.radio("Navigate", list(PAGES.keys()), key="nav_radio", label_visibility="collapsed")

st.sidebar.divider()
if st.sidebar.button("Logout", key="sidebar_logout_button"):
    login.logout()
    st.rerun()

PAGES[selected_page].render()
