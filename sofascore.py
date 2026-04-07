"""Fetch SofaScore average season rating for players."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sofascore.com/",
}

# Top domestic league tournament IDs on SofaScore (prefer these)
_TOP_LEAGUES = {
    23,   # Serie A
    17,   # Premier League
    8,    # LaLiga
    35,   # Bundesliga
    34,   # Ligue 1
    37,   # Eredivisie
    325,  # Liga Portugal
    242,  # Super Lig
    155,  # Super League (Greece)
}


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def search_player(name: str) -> Optional[Dict]:
    """Search SofaScore for a player by name. Returns first match or None."""
    q = urllib.parse.quote(name)
    data = _fetch(
        "https://api.sofascore.com/api/v1/search/all?q={}&page=0".format(q)
    )
    for r in data.get("results", []):
        if r.get("type") == "player":
            ent = r["entity"]
            return {
                "id": ent["id"],
                "name": ent.get("name"),
                "team": ent.get("team", {}).get("name") if ent.get("team") else None,
            }
    return None


def get_season_rating(player_id: int) -> Optional[Dict]:
    """Get the current-season rating for a player from their primary league.

    Returns dict with: rating, tournament, appearances, goals, assists, minutes
    or None if no data.
    """
    seasons_data = _fetch(
        "https://api.sofascore.com/api/v1/player/{}/statistics/seasons".format(
            player_id
        )
    )

    # Collect all (tournament, season) combos for the first (current) season
    candidates = []  # type: List[dict]
    for uts in seasons_data.get("uniqueTournamentSeasons", []):
        tid = uts["uniqueTournament"]["id"]
        tname = uts["uniqueTournament"]["name"]
        for s in uts.get("seasons", [])[:1]:  # only current season
            sid = s["id"]
            candidates.append({
                "tid": tid,
                "tname": tname,
                "sid": sid,
                "is_top_league": tid in _TOP_LEAGUES,
            })

    if not candidates:
        return None

    # Prefer top domestic leagues
    candidates.sort(key=lambda c: (not c["is_top_league"], 0))

    for cand in candidates:
        try:
            stats_data = _fetch(
                "https://api.sofascore.com/api/v1/player/{}"
                "/unique-tournament/{}/season/{}/statistics/overall".format(
                    player_id, cand["tid"], cand["sid"]
                )
            )
            stats = stats_data.get("statistics", {})
            rating = stats.get("rating")
            if rating is None:
                continue
            return {
                "rating": round(float(rating), 2),
                "tournament": cand["tname"],
                "appearances": stats.get("appearances"),
                "goals": stats.get("goals"),
                "assists": stats.get("assists"),
                "minutes": stats.get("minutesPlayed"),
            }
        except Exception:
            continue

    return None


def get_rating_for_name(name: str) -> Optional[Dict]:
    """Search + fetch rating in one call. Returns dict or None."""
    player = search_player(name)
    if not player:
        return None
    result = get_season_rating(player["id"])
    if result:
        result["sofascore_id"] = player["id"]
        result["sofascore_name"] = player["name"]
    return result
