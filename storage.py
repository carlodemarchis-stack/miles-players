"""Storage layer: Supabase-backed with local JSON fallback.

Public API:
    list_players(user_id) -> list[dict]
    upsert_player(user_id, player) -> dict  (returns saved row)
    delete_player(user_id, player_id)
    get_budget(user_id) -> float
    set_budget(user_id, budget_m)
    ensure_profile(user_id, email, display_name=None) -> dict
    add_transaction(user_id, txn) -> dict
    list_transactions(user_id) -> list[dict]
    compute_budget(user_id) -> dict
    delete_all_data(user_id) -> None
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

_DATA_FILE = Path(__file__).parent / "players.json"
_DEFAULT_BUDGET_M = 1000.0

# Columns that exist in the Supabase `players` table (used to strip extras)
_PLAYER_COLUMNS = {
    "id", "user_id", "name", "club", "league", "position", "nationality",
    "birthplace", "photo_url", "tm_url", "age", "height", "market_value",
    "foot", "dob", "on_loan_from", "rating", "apps", "goals", "assists",
    "notes", "purchase_price_m", "sofascore_rating", "sofascore_id",
    "verdict", "verdict_reason",
}

_TRANSACTION_COLUMNS = {
    "id", "user_id", "player_id", "player_name", "type",
    "deal_value_m", "market_value_at_time_m", "created_at",
}


# --------------------------------------------------------------------------- #
# Backend selection                                                           #
# --------------------------------------------------------------------------- #

def _use_local() -> bool:
    try:
        return bool(st.secrets["app"].get("use_local_json", False))
    except Exception:
        return True  # no secrets file -> dev mode


def _supabase_client():
    """Create Supabase client. Uses service_role key if available (bypasses RLS)."""
    if "sb_client" not in st.session_state:
        from supabase import create_client
        url = st.secrets["supabase"]["url"]
        # Prefer service_role key (bypasses RLS — we filter by user_id in code)
        key = st.secrets["supabase"].get("service_role_key") or st.secrets["supabase"]["anon_key"]
        st.session_state["sb_client"] = create_client(url, key)
    return st.session_state["sb_client"]


def _reset_client():
    """Clear cached client so a fresh one is created on next call."""
    st.session_state.pop("sb_client", None)


def _safe_execute(query_fn):
    """Execute a Supabase query, retry once on stale connection errors."""
    try:
        return query_fn()
    except KeyError:
        _reset_client()
        return query_fn()
    except Exception as e:
        if "stream" in str(e).lower() or "connection" in str(e).lower():
            _reset_client()
            return query_fn()
        raise


def set_auth(access_token=None, refresh_token=None):
    """No-op — kept for compatibility. Auth is handled by Streamlit native OAuth."""
    pass


# --------------------------------------------------------------------------- #
# Local JSON backend (single-user, dev fallback)                              #
# --------------------------------------------------------------------------- #

def _local_load() -> dict:
    if _DATA_FILE.exists():
        with open(_DATA_FILE) as f:
            return json.load(f)
    return {}


def _local_save(raw: dict):
    with open(_DATA_FILE, "w") as f:
        json.dump(raw, f, indent=2)


def _local_user_block(user_id: str) -> dict:
    raw = _local_load()
    if isinstance(raw, list):
        # legacy list schema -> migrate
        raw = {"users": {"miles": {"budget_m": _DEFAULT_BUDGET_M, "players": raw}}}
    if "users" not in raw:
        raw = {"users": {}}
    raw["users"].setdefault(
        user_id, {"budget_m": _DEFAULT_BUDGET_M, "players": [], "transactions": []}
    )
    raw["users"][user_id].setdefault("budget_m", _DEFAULT_BUDGET_M)
    raw["users"][user_id].setdefault("players", [])
    raw["users"][user_id].setdefault("transactions", [])
    _local_save(raw)
    return raw


def _next_id(items) -> int:
    return max((p.get("id", 0) for p in items), default=0) + 1


# --------------------------------------------------------------------------- #
# Players                                                                     #
# --------------------------------------------------------------------------- #

def list_players(user_id: str) -> List[dict]:
    if _use_local():
        return _local_user_block(user_id)["users"][user_id]["players"]
    def _q():
        client = _supabase_client()
        return client.table("players").select("*").eq("user_id", user_id).order("id").execute()
    res = _safe_execute(_q)
    return res.data or []


def get_player_by_tm_url(tm_url):
    """Look up a player by TM URL from any user's squad. Returns player dict or None."""
    if _use_local() or not tm_url:
        return None
    try:
        def _q():
            client = _supabase_client()
            return (
                client.table("players")
                .select("*")
                .eq("tm_url", tm_url)
                .limit(1)
                .maybe_single()
                .execute()
            )
        res = _safe_execute(_q)
        return res.data if res and res.data else None
    except Exception:
        return None


def list_all_players() -> List[dict]:
    """Admin: list ALL players across all users."""
    if _use_local():
        return []
    def _q():
        client = _supabase_client()
        return client.table("players").select("*").order("id").execute()
    res = _safe_execute(_q)
    return res.data or []


def upsert_player(user_id: str, player: dict) -> dict:
    if _use_local():
        raw = _local_user_block(user_id)
        players = raw["users"][user_id]["players"]
        if player.get("id"):
            for i, p in enumerate(players):
                if p.get("id") == player["id"]:
                    players[i] = {**p, **player}
                    _local_save(raw)
                    return players[i]
        new = {**player, "id": _next_id(players)}
        players.append(new)
        _local_save(raw)
        return new

    # Supabase path
    payload = {k: v for k, v in player.items() if k in _PLAYER_COLUMNS}
    payload["user_id"] = user_id
    client = _supabase_client()
    if payload.get("id"):
        res = client.table("players").update(payload).eq("id", payload["id"]).eq(
            "user_id", user_id
        ).execute()
    else:
        payload.pop("id", None)
        res = client.table("players").insert(payload).execute()
    return res.data[0] if res.data else payload


def delete_player(user_id: str, player_id) -> None:
    if _use_local():
        raw = _local_user_block(user_id)
        raw["users"][user_id]["players"] = [
            p for p in raw["users"][user_id]["players"] if p.get("id") != player_id
        ]
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("players").delete().eq("id", player_id).eq("user_id", user_id).execute()


# --------------------------------------------------------------------------- #
# Transactions                                                                #
# --------------------------------------------------------------------------- #

def add_transaction(user_id: str, txn: dict) -> dict:
    """Add a buy or sell transaction. txn must have: player_name, type, deal_value_m."""
    txn.setdefault("created_at", datetime.datetime.now().isoformat())

    if _use_local():
        raw = _local_user_block(user_id)
        txns = raw["users"][user_id]["transactions"]
        new = {**txn, "id": _next_id(txns)}
        txns.append(new)
        _local_save(raw)
        return new

    payload = {k: v for k, v in txn.items() if k in _TRANSACTION_COLUMNS}
    payload["user_id"] = user_id
    payload.pop("id", None)
    client = _supabase_client()
    res = client.table("transactions").insert(payload).execute()
    return res.data[0] if res.data else payload


def list_transactions(user_id: str) -> List[dict]:
    if _use_local():
        return list(
            reversed(_local_user_block(user_id)["users"][user_id]["transactions"])
        )
    def _q():
        client = _supabase_client()
        return (
            client.table("transactions")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    res = _safe_execute(_q)
    return res.data or []


def compute_budget(user_id: str) -> Dict[str, float]:
    """Return budget breakdown derived from transactions.

    Returns: {"initial": float, "total_buys": float, "total_sells": float,
              "cash": float}
    """
    initial = get_budget(user_id)
    txns = list_transactions(user_id)
    total_buys = sum(
        float(t.get("deal_value_m", 0)) for t in txns if t.get("type") == "buy"
    )
    total_sells = sum(
        float(t.get("deal_value_m", 0)) for t in txns if t.get("type") == "sell"
    )
    return {
        "initial": initial,
        "total_buys": total_buys,
        "total_sells": total_sells,
        "cash": initial - total_buys + total_sells,
    }


# --------------------------------------------------------------------------- #
# Budget                                                                      #
# --------------------------------------------------------------------------- #

def get_budget(user_id: str) -> float:
    if _use_local():
        raw = _local_user_block(user_id)
        return float(raw["users"][user_id].get("budget_m", _DEFAULT_BUDGET_M))
    client = _supabase_client()
    res = (
        client.table("user_profiles")
        .select("budget_m")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if res and res.data:
        return float(res.data.get("budget_m", _DEFAULT_BUDGET_M))
    return _DEFAULT_BUDGET_M


def set_budget(user_id: str, budget_m: float) -> None:
    if _use_local():
        raw = _local_user_block(user_id)
        raw["users"][user_id]["budget_m"] = float(budget_m)
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("user_profiles").update({"budget_m": float(budget_m)}).eq(
        "user_id", user_id
    ).execute()


# --------------------------------------------------------------------------- #
# Profile                                                                     #
# --------------------------------------------------------------------------- #

def ensure_profile(
    user_id: str, email: str, display_name: Optional[str] = None
) -> dict:
    """Create a user_profiles row if missing. Returns the profile.

    Looks up by email first (since user_id may be email, not UUID).
    Creates a new UUID if no existing profile found.
    """
    if _use_local():
        raw = _local_user_block(user_id)
        return {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "budget_m": raw["users"][user_id].get("budget_m", _DEFAULT_BUDGET_M),
        }
    client = _supabase_client()
    # Look up by email (stable key)
    existing = get_profile_by_email(email)
    if existing:
        return existing

    # Generate new UUID for new user
    import uuid as _uuid
    new_user_id = str(_uuid.uuid4())
    # Split display_name into first/last
    first_name = ""
    last_name = ""
    if display_name:
        parts = display_name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    admin_email = "carlodemarchis@gmail.com"
    is_admin_user = (email or "").lower().strip() == admin_email

    row = {
        "user_id": new_user_id,
        "email": email,
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "nickname": "",
        "is_admin": is_admin_user,
        "budget_m": _DEFAULT_BUDGET_M,
    }
    client.table("user_profiles").insert(row).execute()
    return row


def profile_exists(user_id: str) -> bool:
    if _use_local():
        return True
    client = _supabase_client()
    res = (
        client.table("user_profiles")
        .select("user_id")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    return bool(res and res.data)


# --------------------------------------------------------------------------- #
# Profile                                                                     #
# --------------------------------------------------------------------------- #

def get_profile_by_email(email: str) -> dict:
    """Look up profile by email. Returns profile dict or empty dict."""
    if _use_local():
        return {}
    def _q():
        client = _supabase_client()
        return (
            client.table("user_profiles")
            .select("*")
            .eq("email", email.lower().strip())
            .maybe_single()
            .execute()
        )
    try:
        res = _safe_execute(_q)
        return res.data if res and res.data else {}
    except Exception:
        return {}


def get_profile(user_id: str) -> dict:
    """Return full user profile dict."""
    if _use_local():
        raw = _local_user_block(user_id)
        block = raw["users"][user_id]
        return {
            "user_id": user_id,
            "email": block.get("email", ""),
            "display_name": block.get("display_name"),
            "first_name": block.get("first_name", ""),
            "last_name": block.get("last_name", ""),
            "nickname": block.get("nickname", ""),
            "is_admin": block.get("is_admin", True),
            "budget_m": block.get("budget_m", _DEFAULT_BUDGET_M),
        }
    client = _supabase_client()
    res = (
        client.table("user_profiles")
        .select("*")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    return res.data if res and res.data else {}


def update_profile(user_id: str, fields: dict) -> dict:
    """Update profile fields (first_name, last_name, nickname)."""
    allowed = {
        "first_name", "last_name", "nickname", "team_name", "year_of_birth",
        "selected_formation", "formation_overrides", "is_premium", "admin_pin",
        "language", "search_count", "tour_seen", "last_active_at",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return get_profile(user_id)
    if _use_local():
        raw = _local_user_block(user_id)
        raw["users"][user_id].update(payload)
        _local_save(raw)
        return get_profile(user_id)
    client = _supabase_client()
    client.table("user_profiles").update(payload).eq("user_id", user_id).execute()
    return get_profile(user_id)


def list_all_profiles() -> List[dict]:
    """Admin: list all user profiles."""
    if _use_local():
        return []
    try:
        def _q():
            client = _supabase_client()
            return client.rpc("get_all_profiles").execute()
        res = _safe_execute(_q)
        return res.data or []
    except Exception as e:
        st.error("list_all_profiles error: {}".format(e))
        return []


def admin_update_profile(user_id: str, fields: dict) -> dict:
    """Admin: update any user's profile fields."""
    allowed = {"is_admin", "is_premium", "first_name", "last_name", "nickname", "team_name"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return {}
    if _use_local():
        return {}
    client = _supabase_client()
    client.table("user_profiles").update(payload).eq("user_id", user_id).execute()
    return payload


def is_premium(user_id: str) -> bool:
    profile = get_profile(user_id)
    return bool(profile.get("is_premium", False))


def is_admin(user_id: str) -> bool:
    if _use_local():
        return True
    profile = get_profile(user_id)
    return bool(profile.get("is_admin", False))


# --------------------------------------------------------------------------- #
# App settings (admin-controlled)                                             #
# --------------------------------------------------------------------------- #

_DEFAULT_FORMATIONS = [
    {
        "name": "4-3-3",
        "positions": [
            {"slot": "GK", "role": "Goalkeeper", "x": 50, "y": 92},
            {"slot": "RB", "role": "Right-Back", "x": 85, "y": 72},
            {"slot": "CB", "role": "Centre-Back", "x": 62, "y": 75},
            {"slot": "CB", "role": "Centre-Back", "x": 38, "y": 75},
            {"slot": "LB", "role": "Left-Back", "x": 15, "y": 72},
            {"slot": "CM", "role": "Central Midfield", "x": 65, "y": 50},
            {"slot": "CM", "role": "Central Midfield", "x": 50, "y": 55},
            {"slot": "CM", "role": "Central Midfield", "x": 35, "y": 50},
            {"slot": "RW", "role": "Right Winger", "x": 82, "y": 25},
            {"slot": "LW", "role": "Left Winger", "x": 18, "y": 25},
            {"slot": "CF", "role": "Centre-Forward", "x": 50, "y": 15},
        ],
    },
    {
        "name": "4-2-3-1",
        "positions": [
            {"slot": "GK", "role": "Goalkeeper", "x": 50, "y": 92},
            {"slot": "RB", "role": "Right-Back", "x": 85, "y": 72},
            {"slot": "CB", "role": "Centre-Back", "x": 62, "y": 75},
            {"slot": "CB", "role": "Centre-Back", "x": 38, "y": 75},
            {"slot": "LB", "role": "Left-Back", "x": 15, "y": 72},
            {"slot": "DM", "role": "Defensive Midfield", "x": 60, "y": 55},
            {"slot": "DM", "role": "Defensive Midfield", "x": 40, "y": 55},
            {"slot": "RAM", "role": "Right Winger", "x": 78, "y": 35},
            {"slot": "CAM", "role": "Attacking Midfield", "x": 50, "y": 38},
            {"slot": "LAM", "role": "Left Winger", "x": 22, "y": 35},
            {"slot": "CF", "role": "Centre-Forward", "x": 50, "y": 15},
        ],
    },
    {
        "name": "4-4-2",
        "positions": [
            {"slot": "GK", "role": "Goalkeeper", "x": 50, "y": 92},
            {"slot": "RB", "role": "Right-Back", "x": 85, "y": 72},
            {"slot": "CB", "role": "Centre-Back", "x": 62, "y": 75},
            {"slot": "CB", "role": "Centre-Back", "x": 38, "y": 75},
            {"slot": "LB", "role": "Left-Back", "x": 15, "y": 72},
            {"slot": "RM", "role": "Right Winger", "x": 85, "y": 48},
            {"slot": "CM", "role": "Central Midfield", "x": 60, "y": 50},
            {"slot": "CM", "role": "Central Midfield", "x": 40, "y": 50},
            {"slot": "LM", "role": "Left Winger", "x": 15, "y": 48},
            {"slot": "CF", "role": "Centre-Forward", "x": 60, "y": 18},
            {"slot": "CF", "role": "Second Striker", "x": 40, "y": 18},
        ],
    },
    {
        "name": "3-5-2",
        "positions": [
            {"slot": "GK", "role": "Goalkeeper", "x": 50, "y": 92},
            {"slot": "CB", "role": "Centre-Back", "x": 68, "y": 75},
            {"slot": "CB", "role": "Centre-Back", "x": 50, "y": 78},
            {"slot": "CB", "role": "Centre-Back", "x": 32, "y": 75},
            {"slot": "RWB", "role": "Right-Back", "x": 88, "y": 50},
            {"slot": "CM", "role": "Central Midfield", "x": 62, "y": 52},
            {"slot": "CM", "role": "Defensive Midfield", "x": 50, "y": 56},
            {"slot": "CM", "role": "Central Midfield", "x": 38, "y": 52},
            {"slot": "LWB", "role": "Left-Back", "x": 12, "y": 50},
            {"slot": "CF", "role": "Centre-Forward", "x": 60, "y": 18},
            {"slot": "CF", "role": "Second Striker", "x": 40, "y": 18},
        ],
    },
    {
        "name": "3-4-3",
        "positions": [
            {"slot": "GK", "role": "Goalkeeper", "x": 50, "y": 92},
            {"slot": "CB", "role": "Centre-Back", "x": 68, "y": 75},
            {"slot": "CB", "role": "Centre-Back", "x": 50, "y": 78},
            {"slot": "CB", "role": "Centre-Back", "x": 32, "y": 75},
            {"slot": "RWB", "role": "Right-Back", "x": 88, "y": 50},
            {"slot": "CM", "role": "Central Midfield", "x": 60, "y": 52},
            {"slot": "CM", "role": "Central Midfield", "x": 40, "y": 52},
            {"slot": "LWB", "role": "Left-Back", "x": 12, "y": 50},
            {"slot": "RW", "role": "Right Winger", "x": 78, "y": 22},
            {"slot": "LW", "role": "Left Winger", "x": 22, "y": 22},
            {"slot": "CF", "role": "Centre-Forward", "x": 50, "y": 15},
        ],
    },
]

_DEFAULT_APP_SETTINGS = {
    "budget_m": _DEFAULT_BUDGET_M,
    "max_squad_size": 22,
    "min_players_for_analysis": 11,
    "analysis_prompt": "",
    "formations": json.dumps(_DEFAULT_FORMATIONS),
    "max_saved_teams": 20,
}


def get_app_settings() -> dict:
    """Read the single-row app_settings. Falls back to defaults."""
    if _use_local():
        raw = _local_load()
        return raw.get("app_settings", dict(_DEFAULT_APP_SETTINGS))
    client = _supabase_client()
    try:
        res = (
            client.table("app_settings")
            .select("*")
            .eq("id", 1)
            .maybe_single()
            .execute()
        )
        if res and res.data:
            settings = dict(_DEFAULT_APP_SETTINGS)
            settings.update({k: v for k, v in res.data.items() if v is not None})
            return settings
    except Exception:
        pass
    return dict(_DEFAULT_APP_SETTINGS)


def update_app_settings(fields: dict) -> dict:
    """Update app-wide settings (admin only)."""
    allowed = {"budget_m", "max_squad_size", "min_players_for_analysis", "analysis_prompt", "formations", "last_refresh_at", "max_saved_teams"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return get_app_settings()
    if _use_local():
        raw = _local_load()
        raw.setdefault("app_settings", dict(_DEFAULT_APP_SETTINGS))
        raw["app_settings"].update(payload)
        _local_save(raw)
        return raw["app_settings"]
    client = _supabase_client()
    client.table("app_settings").update(payload).eq("id", 1).execute()
    return get_app_settings()


# --------------------------------------------------------------------------- #
# Notes                                                                       #
# --------------------------------------------------------------------------- #

_NOTE_COLUMNS = {"id", "user_id", "title", "content", "created_at", "updated_at"}


def list_notes(user_id):
    if _use_local():
        raw = _local_user_block(user_id)
        return list(reversed(raw["users"][user_id].get("notes_list", [])))
    client = _supabase_client()
    res = (
        client.table("notes")
        .select("*")
        .eq("user_id", user_id)
        .order("updated_at", desc=True)
        .execute()
    )
    return res.data or []


def add_note(user_id, note):
    import datetime
    note.setdefault("created_at", datetime.datetime.now().isoformat())
    note.setdefault("updated_at", note["created_at"])
    if _use_local():
        raw = _local_user_block(user_id)
        notes = raw["users"][user_id].setdefault("notes_list", [])
        new = {**note, "id": _next_id(notes)}
        notes.append(new)
        _local_save(raw)
        return new
    payload = {k: v for k, v in note.items() if k in _NOTE_COLUMNS}
    payload["user_id"] = user_id
    payload.pop("id", None)
    client = _supabase_client()
    res = client.table("notes").insert(payload).execute()
    return res.data[0] if res.data else payload


def update_note(user_id, note_id, fields):
    import datetime
    fields["updated_at"] = datetime.datetime.now().isoformat()
    if _use_local():
        raw = _local_user_block(user_id)
        notes = raw["users"][user_id].setdefault("notes_list", [])
        for i, n in enumerate(notes):
            if n.get("id") == note_id:
                notes[i].update(fields)
                _local_save(raw)
                return notes[i]
        return {}
    payload = {k: v for k, v in fields.items() if k in _NOTE_COLUMNS}
    client = _supabase_client()
    res = (
        client.table("notes")
        .update(payload)
        .eq("id", note_id)
        .eq("user_id", user_id)
        .execute()
    )
    return res.data[0] if res.data else {}


def delete_note(user_id, note_id):
    if _use_local():
        raw = _local_user_block(user_id)
        notes = raw["users"][user_id].setdefault("notes_list", [])
        raw["users"][user_id]["notes_list"] = [
            n for n in notes if n.get("id") != note_id
        ]
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("notes").delete().eq("id", note_id).eq("user_id", user_id).execute()


def get_player_owners(tm_url):
    """Find all users who own a player by tm_url. Returns list of {email, display_name, team_name}."""
    if _use_local() or not tm_url:
        return []
    client = _supabase_client()
    try:
        # Need to bypass RLS — use a DB function or join via service role
        # Simple approach: query players table (RLS only shows own rows)
        # So we use an RPC function instead
        res = client.rpc("get_player_owners", {"p_tm_url": tm_url}).execute()
        return res.data or []
    except Exception:
        return []


def get_all_owned_tm_urls(exclude_user_id=None):
    """Get all tm_urls owned by other users. Returns dict {tm_url: [{name, team_name}]}."""
    if _use_local():
        return {}
    client = _supabase_client()
    try:
        res = client.rpc("get_all_owned_players").execute()
        result = {}
        for row in (res.data or []):
            if exclude_user_id and row.get("user_id") == exclude_user_id:
                continue
            url = row.get("tm_url")
            if url:
                result.setdefault(url, []).append({
                    "display_name": row.get("display_name") or row.get("email", "?"),
                    "team_name": row.get("team_name", ""),
                })
        return result
    except Exception:
        return {}


def get_formations():
    """Return list of formation dicts."""
    settings = get_app_settings()
    raw = settings.get("formations", "")
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except Exception:
            return list(_DEFAULT_FORMATIONS)
    if isinstance(raw, list):
        return raw
    return list(_DEFAULT_FORMATIONS)


# --------------------------------------------------------------------------- #
# Analysis cache                                                              #
# --------------------------------------------------------------------------- #

def get_last_analysis(user_id: str) -> Optional[str]:
    if _use_local():
        raw = _local_user_block(user_id)
        return raw["users"][user_id].get("last_analysis")
    client = _supabase_client()
    res = (
        client.table("user_profiles")
        .select("last_analysis")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if res and res.data:
        return res.data.get("last_analysis")
    return None


def save_last_analysis(user_id: str, text: str) -> None:
    if _use_local():
        raw = _local_user_block(user_id)
        raw["users"][user_id]["last_analysis"] = text
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("user_profiles").update({"last_analysis": text}).eq(
        "user_id", user_id
    ).execute()


# --------------------------------------------------------------------------- #
# Reset (danger zone)                                                         #
# --------------------------------------------------------------------------- #

def list_saved_teams(user_id):
    """List all saved team snapshots for a user, newest first."""
    if _use_local():
        raw = _local_user_block(user_id)
        return list(reversed(raw["users"][user_id].get("saved_teams", [])))
    def _q():
        client = _supabase_client()
        return (
            client.table("saved_teams")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    res = _safe_execute(_q)
    return res.data or []


def save_team(user_id, name, description, players, formation, total_value_m, avg_sofascore):
    """Save a snapshot of the current squad."""
    payload = {
        "user_id": user_id,
        "name": name,
        "description": description,
        "snapshot": players,
        "formation": formation,
        "total_value_m": total_value_m,
        "avg_sofascore": avg_sofascore,
    }
    if _use_local():
        import datetime
        raw = _local_user_block(user_id)
        teams = raw["users"][user_id].setdefault("saved_teams", [])
        new = {**payload, "id": _next_id(teams),
               "created_at": datetime.datetime.now().isoformat()}
        teams.append(new)
        _local_save(raw)
        return new
    client = _supabase_client()
    res = client.table("saved_teams").insert(payload).execute()
    return res.data[0] if res.data else payload


def delete_saved_team(user_id, team_id):
    if _use_local():
        raw = _local_user_block(user_id)
        raw["users"][user_id]["saved_teams"] = [
            t for t in raw["users"][user_id].get("saved_teams", [])
            if t.get("id") != team_id
        ]
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("saved_teams").delete().eq("id", team_id).eq("user_id", user_id).execute()


def rename_saved_team(user_id, team_id, new_name, new_description=""):
    if _use_local():
        raw = _local_user_block(user_id)
        for t in raw["users"][user_id].get("saved_teams", []):
            if t.get("id") == team_id:
                t["name"] = new_name
                t["description"] = new_description
                break
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("saved_teams").update({
        "name": new_name,
        "description": new_description,
    }).eq("id", team_id).eq("user_id", user_id).execute()


# --------------------------------------------------------------------------- #
# TM cache                                                                    #
# --------------------------------------------------------------------------- #

_TM_CACHE_TTL_DAYS = 7


def tm_cache_get(url):
    """Return cached HTML for a TM URL if fresh, else None."""
    if _use_local():
        return None
    try:
        def _q():
            client = _supabase_client()
            return (
                client.table("tm_cache")
                .select("html, cached_at")
                .eq("url", url)
                .maybe_single()
                .execute()
            )
        res = _safe_execute(_q)
        if not res or not res.data:
            return None
        cached_at = res.data.get("cached_at", "")
        if not cached_at:
            return None
        import datetime
        try:
            ts = datetime.datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            if (now - ts).days >= _TM_CACHE_TTL_DAYS:
                return None
        except Exception:
            return None
        return res.data.get("html")
    except Exception:
        return None


def tm_cache_set(url, html):
    """Store HTML in cache (upsert)."""
    if _use_local():
        return
    try:
        import datetime
        payload = {
            "url": url,
            "html": html,
            "cached_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        client = _supabase_client()
        client.table("tm_cache").upsert(payload).execute()
    except Exception:
        pass


def touch_last_active(user_id):
    """Update last_active_at to now."""
    if _use_local():
        return
    try:
        import datetime
        client = _supabase_client()
        client.table("user_profiles").update(
            {"last_active_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        ).eq("user_id", user_id).execute()
    except Exception:
        pass


def increment_search_count(user_id):
    """Increment the per-user search counter by 1."""
    if _use_local():
        raw = _local_user_block(user_id)
        block = raw["users"][user_id]
        block["search_count"] = int(block.get("search_count", 0)) + 1
        _local_save(raw)
        return
    try:
        client = _supabase_client()
        current = (
            client.table("user_profiles")
            .select("search_count")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        cur = 0
        if current and current.data:
            cur = int(current.data.get("search_count") or 0)
        client.table("user_profiles").update(
            {"search_count": cur + 1}
        ).eq("user_id", user_id).execute()
    except Exception:
        pass


def global_stats():
    """Admin: return global stats across all users."""
    if _use_local():
        return {}
    try:
        client = _supabase_client()
        profiles = client.table("user_profiles").select(
            "user_id, email, is_admin, is_premium, created_at, search_count, nickname, first_name, last_name, last_active_at"
        ).execute().data or []
        players = client.table("players").select("id, user_id, market_value, name, league, club, age, position").execute().data or []
        txns = client.table("transactions").select(
            "id, user_id, type, deal_value_m, player_name"
        ).execute().data or []
        saved_teams = client.table("saved_teams").select("id, user_id").execute().data or []

        def _mv(s):
            s = (s or "").lower().strip()
            num = "".join(c for c in s if c.isdigit() or c == ".")
            try:
                v = float(num)
            except ValueError:
                return 0.0
            if "k" in s:
                v /= 1000.0
            return v

        # Users
        total_users = len(profiles)
        admins = sum(1 for p in profiles if p.get("is_admin"))
        premiums = sum(1 for p in profiles if p.get("is_premium"))

        # Squads
        squad_by_user = {}
        squad_value_by_user = {}
        for p in players:
            uid = p["user_id"]
            squad_by_user[uid] = squad_by_user.get(uid, 0) + 1
            squad_value_by_user[uid] = squad_value_by_user.get(uid, 0) + _mv(p.get("market_value"))

        total_players = len(players)
        avg_squad = total_players / total_users if total_users else 0
        most_val_user = max(squad_value_by_user.items(), key=lambda x: x[1], default=(None, 0))

        # Transactions
        buys = [t for t in txns if t.get("type") == "buy"]
        sells = [t for t in txns if t.get("type") == "sell"]
        total_buys_val = sum(float(t.get("deal_value_m") or 0) for t in buys)
        total_sells_val = sum(float(t.get("deal_value_m") or 0) for t in sells)

        biggest_buy = max(buys, key=lambda t: float(t.get("deal_value_m") or 0), default=None)
        biggest_sell = max(sells, key=lambda t: float(t.get("deal_value_m") or 0), default=None)

        # Most-bought player
        from collections import Counter
        player_counts = Counter(t.get("player_name", "?") for t in buys)
        top_bought = player_counts.most_common(5)

        # Saved teams
        teams_by_user = {}
        for t in saved_teams:
            uid = t["user_id"]
            teams_by_user[uid] = teams_by_user.get(uid, 0) + 1

        # Map user_id to display name (nickname > first+last > email)
        def _name(p):
            nick = p.get("nickname", "")
            fn = p.get("first_name", "")
            ln = p.get("last_name", "")
            full = "{} {}".format(fn, ln).strip()
            return nick or full or p.get("email", "?")

        uid_to_email = {p["user_id"]: _name(p) for p in profiles}

        # Searches
        total_searches = sum(int(p.get("search_count") or 0) for p in profiles)
        top_searchers = sorted(
            [(_name(p), int(p.get("search_count") or 0)) for p in profiles],
            key=lambda x: -x[1],
        )[:5]

        return {
            "total_users": total_users,
            "admins": admins,
            "premiums": premiums,
            "total_players": total_players,
            "avg_squad": avg_squad,
            "most_val_user_email": uid_to_email.get(most_val_user[0], "?"),
            "most_val_user_value": most_val_user[1],
            "total_txns": len(txns),
            "total_buys": len(buys),
            "total_sells": len(sells),
            "total_buys_val": total_buys_val,
            "total_sells_val": total_sells_val,
            "biggest_buy": biggest_buy,
            "biggest_sell": biggest_sell,
            "top_bought": top_bought,
            "total_saved_teams": len(saved_teams),
            "avg_saved_per_user": len(saved_teams) / total_users if total_users else 0,
            "total_searches": total_searches,
            "top_searchers": top_searchers,
            # Leagues, clubs, ages
            "leagues": Counter(p.get("league", "Unknown") or "Unknown" for p in players).most_common(15),
            "clubs": Counter(p.get("club", "Unknown") or "Unknown" for p in players).most_common(15),
            "ages": [p.get("age") for p in players if p.get("age")],
            "positions": Counter(p.get("position", "Unknown") or "Unknown" for p in players if p.get("position")).most_common(15),
            "roles": Counter(
                {"Goalkeeper": "GK", "Centre-Back": "DEF", "Right-Back": "DEF", "Left-Back": "DEF",
                 "Defensive Midfield": "MID", "Central Midfield": "MID", "Attacking Midfield": "MID",
                 "Right Winger": "ATT", "Left Winger": "ATT", "Second Striker": "ATT", "Centre-Forward": "ATT",
                }.get(p.get("position", ""), "Other")
                for p in players if p.get("position")
            ).most_common(),
            # Activity
            "user_activity": [
                (_name(p), p.get("last_active_at", ""), int(p.get("search_count") or 0))
                for p in profiles
            ],
        }
    except Exception as e:
        return {"error": str(e)}


def tm_cache_stats():
    """Return cache stats: count, total size KB, oldest age days."""
    if _use_local():
        return {"count": 0, "size_kb": 0, "oldest_days": 0}
    try:
        client = _supabase_client()
        res = client.table("tm_cache").select("html, cached_at").execute()
        rows = res.data or []
        count = len(rows)
        total_bytes = sum(len(r.get("html", "") or "") for r in rows)
        oldest_days = 0
        if rows:
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
            ages = []
            for r in rows:
                ts_str = r.get("cached_at", "")
                if ts_str:
                    try:
                        ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ages.append((now - ts).days)
                    except Exception:
                        pass
            if ages:
                oldest_days = max(ages)
        return {
            "count": count,
            "size_kb": round(total_bytes / 1024, 1),
            "oldest_days": oldest_days,
        }
    except Exception:
        return {"count": 0, "size_kb": 0, "oldest_days": 0}


def tm_cache_invalidate(urls=None):
    """Clear cache for given URLs, or all if None."""
    if _use_local():
        return
    try:
        client = _supabase_client()
        if urls:
            for url in urls:
                client.table("tm_cache").delete().eq("url", url).execute()
        else:
            client.table("tm_cache").delete().neq("url", "").execute()
    except Exception:
        pass


def delete_all_data(user_id: str) -> None:
    """Delete all players + transactions and reset budget to default."""
    if _use_local():
        raw = _local_user_block(user_id)
        raw["users"][user_id]["players"] = []
        raw["users"][user_id]["transactions"] = []
        raw["users"][user_id]["budget_m"] = _DEFAULT_BUDGET_M
        _local_save(raw)
        return
    client = _supabase_client()
    client.table("saved_teams").delete().eq("user_id", user_id).execute()
    client.table("transactions").delete().eq("user_id", user_id).execute()
    client.table("players").delete().eq("user_id", user_id).execute()
    client.table("user_profiles").update({"budget_m": _DEFAULT_BUDGET_M}).eq(
        "user_id", user_id
    ).execute()
