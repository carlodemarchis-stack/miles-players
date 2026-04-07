"""Upload existing players.json data to Supabase for a specific user.

Usage:
    1. Make sure the user has already signed in at least once and redeemed the
       invite code (so their row exists in `user_profiles`).
    2. Run:  python3 migrate_to_supabase.py <user-email>
    3. The script reads players.json (legacy schema) and inserts each player
       into the Supabase `players` table for the matching user.

This uses the SERVICE ROLE key so it bypasses RLS. You must provide it via:
    export SUPABASE_SERVICE_ROLE_KEY=<service-role-jwt>
Get it from: Supabase Dashboard → Project Settings → API → service_role key.
DO NOT commit the service role key anywhere.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore

DATA_FILE = Path(__file__).parent / "players.json"
SECRETS_FILE = Path(__file__).parent / ".streamlit" / "secrets.toml"

PLAYER_COLUMNS = {
    "name", "club", "league", "position", "nationality", "birthplace",
    "photo_url", "tm_url", "age", "height", "market_value", "foot", "dob",
    "on_loan_from", "rating", "apps", "goals", "assists", "notes",
}


def load_secrets():
    with open(SECRETS_FILE, "rb") as f:
        return tomllib.load(f)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 migrate_to_supabase.py <user-email>")
        sys.exit(1)
    email = sys.argv[1].strip().lower()

    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not service_key:
        print("ERROR: set SUPABASE_SERVICE_ROLE_KEY env var.")
        sys.exit(1)

    secrets = load_secrets()
    url = secrets["supabase"]["url"]

    from supabase import create_client

    client = create_client(url, service_key)

    # Find the user by email
    users = client.auth.admin.list_users()
    user = None
    for u in users:
        u_email = (u.email if hasattr(u, "email") else u.get("email")) or ""
        if u_email.lower() == email:
            user = u
            break
    if user is None:
        print(f"ERROR: no Supabase auth user found with email {email}")
        print("Have them sign in at least once first.")
        sys.exit(1)
    user_id = user.id if hasattr(user, "id") else user["id"]
    print(f"Found user: {email} -> {user_id}")

    # Load legacy data
    if not DATA_FILE.exists():
        print("No players.json found, nothing to migrate.")
        return
    with open(DATA_FILE) as f:
        raw = json.load(f)

    # Handle both schemas
    if isinstance(raw, list):
        players = raw
    else:
        # {"users": {"miles": {"players": [...], "budget_m": ...}}}
        users_dict = raw.get("users", {})
        if not users_dict:
            print("No players found.")
            return
        # Take first user block (or ask)
        first_key = next(iter(users_dict))
        print(f"Using local user block: {first_key}")
        block = users_dict[first_key]
        players = block.get("players", [])
        budget_m = block.get("budget_m")

        # Ensure profile exists + set budget
        profile = (
            client.table("user_profiles").select("*").eq("user_id", user_id)
            .maybe_single().execute()
        )
        if not (profile and profile.data):
            print("Creating user_profile row...")
            client.table("user_profiles").insert({
                "user_id": user_id,
                "email": email,
                "budget_m": budget_m if budget_m is not None else 1000.0,
            }).execute()
        elif budget_m is not None:
            client.table("user_profiles").update({"budget_m": budget_m}).eq(
                "user_id", user_id
            ).execute()
            print(f"Updated budget to {budget_m}")

    if not players:
        print("No players to migrate.")
        return

    inserted = 0
    for p in players:
        payload = {k: v for k, v in p.items() if k in PLAYER_COLUMNS}
        payload["user_id"] = user_id
        client.table("players").insert(payload).execute()
        inserted += 1
        print(f"  + {p.get('name')}")

    print(f"\nDone. Inserted {inserted} players for {email}.")


if __name__ == "__main__":
    main()
