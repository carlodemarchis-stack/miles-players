"""Auth flow: Streamlit native OAuth + Supabase for data.

Uses st.login / st.user (built-in since Streamlit 1.42).
Google handles auth, Streamlit handles cookies/sessions.
Supabase is used only for data storage (not auth).
"""

from __future__ import annotations

import streamlit as st
import storage


def _invite_code():
    try:
        return st.secrets["app"].get("invite_code")
    except Exception:
        return None


def _use_local():
    try:
        return bool(st.secrets["app"].get("use_local_json", False))
    except Exception:
        return True


def _clear_session():
    for k in ("sb_user", "user", "players", "app_settings", "owned_map"):
        st.session_state.pop(k, None)
    st.logout()


def require_login():
    """Return current user dict or stop execution after rendering login UI."""
    if _use_local():
        user = {"id": "miles", "email": "miles@local", "display_name": "Miles"}
        st.session_state["sb_user"] = user
        return user

    # Streamlit native auth — handles everything
    if not st.user.is_logged_in:
        st.title("⚽ Football Stars")
        st.caption("Please sign in to continue")
        st.login("google")
        st.caption("First-time users will be asked for an invite code after signing in.")
        st.stop()

    # User is logged in via Google
    email = (st.user.email or "").lower().strip()
    name = st.user.name or ""

    # Look up existing user_id by email, or use email as new ID
    profile = storage.get_profile_by_email(email)
    if profile:
        user_id = profile.get("user_id", email)
    else:
        user_id = email

    user = {
        "id": user_id,
        "email": email,
        "display_name": name,
    }
    st.session_state["sb_user"] = user

    # Ensure profile exists in Supabase (for data storage)
    if not profile:
        # Check invite code
        if not st.session_state.get("invite_redeemed_{}".format(user_id)):
            st.title("⚽ Football Stars")
            st.subheader("Welcome, {}".format(name or email))
            st.caption("You need an invite code to get started.")
            code = st.text_input("Invite code", type="password")
            col1, col2 = st.columns(2)
            if col1.button("✅ Redeem", use_container_width=True, disabled=not code):
                if code.strip() == _invite_code():
                    storage.ensure_profile(user_id, email, name)
                    st.session_state["invite_redeemed_{}".format(user_id)] = True
                    st.success("You're in!")
                    st.rerun()
                else:
                    st.error("Invalid invite code.")
            if col2.button("🚪 Sign out", use_container_width=True):
                _clear_session()
                st.rerun()
            st.stop()

    return user
