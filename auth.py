"""Auth flow: Google OAuth via Supabase + persistent session via cookies.

Login once with Google → session persists across reloads for 30 days.
"""

from __future__ import annotations

import base64
import datetime
import json
import time as _time
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
import extra_streamlit_components as stx

import storage

_COOKIE_NAME = "sb_refresh_token"
_COOKIE_EXPIRY_DAYS = 30


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


def _supabase():
    from supabase import create_client
    return create_client(
        st.secrets["supabase"]["url"], st.secrets["supabase"]["anon_key"]
    )


def _cookie_manager():
    """Create cookie manager — must be called exactly ONCE per page render."""
    return stx.CookieManager(key="auth_cookies")


# --------------------------------------------------------------------------- #
# OAuth                                                                       #
# --------------------------------------------------------------------------- #

def _redirect_url():
    try:
        return st.context.url
    except Exception:
        pass
    try:
        return st.secrets["app"].get("redirect_url", "http://localhost:8502")
    except Exception:
        return "http://localhost:8502"


def _handle_oauth_callback(cm):
    """Handle tokens returning from OAuth redirect. Returns True if handled."""
    qp = st.query_params

    # Implicit flow: tokens in query params (from fragment bridge)
    if "access_token" in qp and "refresh_token" in qp:
        access = qp["access_token"]
        refresh = qp["refresh_token"]
        st.session_state["sb_access_token"] = access
        st.session_state["sb_refresh_token"] = refresh
        # Persist refresh token in cookie
        cm.set(
            _COOKIE_NAME,
            refresh,
            expires_at=datetime.datetime.now() + datetime.timedelta(days=_COOKIE_EXPIRY_DAYS),
            key="set_cookie_login",
        )
        for k in ("access_token", "refresh_token", "expires_in", "expires_at",
                  "provider_token", "provider_refresh_token", "token_type"):
            if k in qp:
                del qp[k]
        return True
    return False


def _fragment_to_query_bridge():
    """Move URL hash tokens into query params so Streamlit sees them."""
    components.html(
        """
        <script>
        (function() {
            const hash = window.parent.location.hash;
            if (hash && hash.includes('access_token')) {
                const params = new URLSearchParams(hash.substring(1));
                const search = new URLSearchParams(window.parent.location.search);
                for (const [k, v] of params) { search.set(k, v); }
                const newUrl = window.parent.location.pathname + '?' + search.toString();
                window.parent.history.replaceState(null, '', newUrl);
                window.parent.location.reload();
            }
        })();
        </script>
        """,
        height=0,
    )


def _try_restore_from_cookie(cm):
    """Read refresh token from cookie, exchange for new session. Returns True if ok."""
    token = cm.get(_COOKIE_NAME)
    if not token:
        return False

    try:
        client = _supabase()
        res = client.auth.refresh_session(token)
        session = res.session if hasattr(res, "session") else res.get("session")
        if session:
            access = session.access_token if hasattr(session, "access_token") else session["access_token"]
            new_refresh = session.refresh_token if hasattr(session, "refresh_token") else session["refresh_token"]
            st.session_state["sb_access_token"] = access
            st.session_state["sb_refresh_token"] = new_refresh
            # Update cookie with fresh token
            cm.set(
                _COOKIE_NAME,
                new_refresh,
                expires_at=datetime.datetime.now() + datetime.timedelta(days=_COOKIE_EXPIRY_DAYS),
                key="set_cookie_refresh",
            )
            return True
    except Exception:
        pass
    # Bad token — delete cookie
    cm.delete(_COOKIE_NAME, key="delete_cookie_bad")
    return False


# --------------------------------------------------------------------------- #
# Login UI                                                                    #
# --------------------------------------------------------------------------- #

def _login_screen():
    st.title("⚽ Football Stars")
    st.caption("Please sign in to continue")

    client = _supabase()
    client.auth._flow_type = "implicit"
    res = client.auth.sign_in_with_oauth(
        {
            "provider": "google",
            "options": {"redirect_to": _redirect_url()},
        }
    )
    url = res.url if hasattr(res, "url") else res.get("url")
    st.link_button("🔐 Sign in with Google", url, use_container_width=True)
    st.caption("First-time users will be asked for an invite code after signing in.")
    st.stop()


def _invite_code_screen(user_id, email, display_name):
    st.title("⚽ Football Stars")
    st.subheader("Welcome, {}".format(display_name or email))
    st.caption("You need an invite code to get started.")

    code = st.text_input("Invite code", type="password")
    col1, col2 = st.columns(2)
    if col1.button("✅ Redeem", use_container_width=True, disabled=not code):
        if code.strip() == _invite_code():
            storage.ensure_profile(user_id, email, display_name)
            st.success("You're in! Loading your account...")
            st.rerun()
        else:
            st.error("Invalid invite code. Ask the administrator.")
    if col2.button("🚪 Sign out", use_container_width=True):
        _clear_session(_cookie_manager())
        st.rerun()
    st.stop()


# --------------------------------------------------------------------------- #
# Session helpers                                                             #
# --------------------------------------------------------------------------- #

def _clear_session(cm):
    cm.delete(_COOKIE_NAME, key="delete_cookie_logout")
    for k in ("sb_user", "sb_access_token", "sb_refresh_token"):
        st.session_state.pop(k, None)


def _refresh_token_if_needed():
    """Refresh the access token using the refresh token if expired or about to expire."""
    refresh = st.session_state.get("sb_refresh_token")
    if not refresh:
        return
    # Decode JWT to check expiry (without verification)
    try:
        token = st.session_state.get("sb_access_token", "")
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp", 0)
        if exp - _time.time() > 300:
            return
    except Exception:
        pass  # Can't decode — try refreshing anyway

    try:
        client = _supabase()
        res = client.auth.refresh_session(refresh)
        session = res.session if hasattr(res, "session") else res.get("session")
        if session:
            st.session_state["sb_access_token"] = (
                session.access_token if hasattr(session, "access_token") else session["access_token"]
            )
            st.session_state["sb_refresh_token"] = (
                session.refresh_token if hasattr(session, "refresh_token") else session["refresh_token"]
            )
            st.session_state.pop("sb_user", None)  # Force re-fetch user
    except Exception:
        # Refresh failed — clear session, user will need to re-login
        _clear_session()
        st.rerun()


def _current_user():
    if "sb_user" in st.session_state:
        return st.session_state["sb_user"]

    access = st.session_state.get("sb_access_token")
    if not access:
        return None
    client = _supabase()
    try:
        resp = client.auth.get_user(access)
        user = resp.user if hasattr(resp, "user") else resp.get("user")
        if not user:
            return None
        u = {
            "id": user.id if hasattr(user, "id") else user["id"],
            "email": (user.email if hasattr(user, "email") else user["email"]) or "",
            "display_name": (
                user.user_metadata.get("full_name") or user.user_metadata.get("name")
                if hasattr(user, "user_metadata") and user.user_metadata
                else None
            ),
        }
        st.session_state["sb_user"] = u
        return u
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Main entry point                                                            #
# --------------------------------------------------------------------------- #

def require_login():
    """Return current user dict or stop execution after rendering login UI."""
    if _use_local():
        user = {"id": "miles", "email": "miles@local", "display_name": "Miles"}
        st.session_state["sb_user"] = user
        return user

    # Single cookie manager instance for this render
    cm = _cookie_manager()

    # Handle deferred sign-out (flag set by app.py menu)
    if st.session_state.pop("do_sign_out", False):
        cm.delete(_COOKIE_NAME, key="delete_cookie_logout")
        for k in ("sb_user", "sb_access_token", "sb_refresh_token",
                  "user", "players", "app_settings"):
            st.session_state.pop(k, None)
        st.session_state["_signed_out"] = True
        st.rerun()

    _fragment_to_query_bridge()
    _handle_oauth_callback(cm)

    # If no session in memory, try restoring from cookie
    # (skip if we just signed out — cookie deletion is async)
    if "sb_access_token" not in st.session_state:
        if st.session_state.pop("_signed_out", False):
            pass  # don't restore, cookie is being deleted
        elif _try_restore_from_cookie(cm):
            st.rerun()

    user = _current_user()
    if not user:
        _login_screen()  # stops

    # Refresh the token if it's about to expire or already expired
    _refresh_token_if_needed()

    # Hook the access token onto the Supabase client for RLS
    storage.set_auth(
        st.session_state["sb_access_token"],
        st.session_state.get("sb_refresh_token", ""),
    )

    # Check whitelist
    if not storage.profile_exists(user["id"]):
        _invite_code_screen(user["id"], user["email"], user.get("display_name"))

    return user
