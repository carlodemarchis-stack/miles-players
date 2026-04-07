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


@st.cache_resource
def _supabase_client():
    from supabase import create_client

    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["anon_key"]
    return create_client(url, key)


def set_auth(access_token: str, refresh_token: str):
    """Attach the current user's JWT to the Supabase client so RLS kicks in."""
    client = _supabase_client()
    client.postgrest.auth(access_token)
    try:
        client.auth.set_session(access_token, refresh_token)
    except Exception:
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
    client = _supabase_client()
    res = client.table("players").select("*").eq("user_id", user_id).order("id").execute()
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
    client = _supabase_client()
    res = (
        client.table("transactions")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
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
    """Create a user_profiles row if missing. Returns the profile."""
    if _use_local():
        raw = _local_user_block(user_id)
        return {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "budget_m": raw["users"][user_id].get("budget_m", _DEFAULT_BUDGET_M),
        }
    client = _supabase_client()
    existing = (
        client.table("user_profiles")
        .select("*")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if existing and existing.data:
        return existing.data
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
        "user_id": user_id,
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
        "selected_formation", "formation_overrides",
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
    allowed = {"budget_m", "max_squad_size", "min_players_for_analysis", "analysis_prompt", "formations"}
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
    client.table("transactions").delete().eq("user_id", user_id).execute()
    client.table("players").delete().eq("user_id", user_id).execute()
    client.table("user_profiles").update({"budget_m": _DEFAULT_BUDGET_M}).eq(
        "user_id", user_id
    ).execute()
