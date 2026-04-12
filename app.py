import io
import json
import time
from pathlib import Path

import streamlit as st

import storage
from auth import require_login
from flags import country_to_flag
from sofascore import get_rating_for_name
import transfermarkt as _tm
from transfermarkt import scrape_player, search_player

# Wire TM cache hooks
_tm.set_cache_hooks(
    lambda url: storage.tm_cache_get(url),
    lambda url, html: storage.tm_cache_set(url, html),
)

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULT_BUDGET_M = 1000.0  # fallback if app_settings not loaded
DEFAULT_MAX_SQUAD_SIZE = 22


def _app_settings():
    """Get app settings (cached in session_state per run)."""
    if "app_settings" not in st.session_state:
        st.session_state.app_settings = storage.get_app_settings()
    return st.session_state.app_settings


def _max_squad():
    return int(_app_settings().get("max_squad_size", DEFAULT_MAX_SQUAD_SIZE))


def _analysis_prompt_override():
    """Return custom analysis prompt from settings, or empty string for default."""
    return (_app_settings().get("analysis_prompt") or "").strip()


DEFAULT_ANALYSIS_PROMPT = (
    "You are a football scout and squad analyst for a fantasy football game. "
    "The user is {user_name}, a {user_age}-year-old who loves football. "
    "Be enthusiastic but analytical. Adapt your language to the user's age. "
    "The max squad size is {max_squad}. "
    "Analyze the squad and provide: "
    "1) Overall assessment (2-3 sentences) "
    "2) Strengths (each bullet point on its own line) "
    "3) Weaknesses/gaps (each bullet point on its own line, consider position coverage for a {max_squad}-player squad) "
    "4) Transfer suggestions: who to buy and why (consider budget and remaining slots) "
    "5) A fun squad rating out of 10. "
    "FORMATTING: Use markdown. Each bullet point MUST be on its own separate line using '- ' prefix. "
    "Never put multiple bullets on the same line. Use blank lines between sections for readability. "
    "Keep it concise and fun. Use football terminology but explain it simply.{lang}"
)


def _min_players_for_analysis():
    return int(_app_settings().get("min_players_for_analysis", 11))


def _ownership_badge(tm_url, owned_map):
    """Return ownership text if other users own this player, else empty string."""
    if not tm_url or not owned_map:
        return ""
    others = owned_map.get(tm_url, [])
    if not others:
        return ""
    names = [o.get("team_name") or o.get("display_name", "?") for o in others]
    return "⚡ Also: {}".format(", ".join(names))


def _display_name(user, profile=None):
    """Display name priority: nickname > first+last > Google display_name > email."""
    if profile:
        if profile.get("nickname"):
            return profile["nickname"]
        fn = profile.get("first_name", "")
        ln = profile.get("last_name", "")
        if fn or ln:
            return "{} {}".format(fn, ln).strip()
    return user.get("display_name") or user.get("email", "")

POSITIONS = [
    "Goalkeeper",
    "Centre-Back",
    "Right-Back",
    "Left-Back",
    "Defensive Midfield",
    "Central Midfield",
    "Attacking Midfield",
    "Right Winger",
    "Left Winger",
    "Second Striker",
    "Centre-Forward",
]
POSITION_ORDER = {pos: i for i, pos in enumerate(POSITIONS)}
ROLE_PREFIX = {
    "Goalkeeper": "GK",
    "Centre-Back": "DF", "Right-Back": "DF", "Left-Back": "DF",
    "Defensive Midfield": "MF", "Central Midfield": "MF",
    "Attacking Midfield": "MF",
    "Right Winger": "FW", "Left Winger": "FW",
    "Second Striker": "FW", "Centre-Forward": "FW",
}
SHORT_POS = {
    "Goalkeeper": "GK",
    "Centre-Back": "CB",
    "Right-Back": "RB",
    "Left-Back": "LB",
    "Defensive Midfield": "DM",
    "Central Midfield": "CM",
    "Attacking Midfield": "AM",
    "Right Winger": "RW",
    "Left Winger": "LW",
    "Second Striker": "SS",
    "Centre-Forward": "CF",
}


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _current_user_id():
    return st.session_state["user"]["id"]


def load_players():
    return storage.list_players(_current_user_id())


def save_player(player):
    return storage.upsert_player(_current_user_id(), player)


def delete_player(player_id):
    storage.delete_player(_current_user_id(), player_id)


def load_settings():
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def _mv_num(p):
    """Market value in millions. Parses 'm' / 'k' suffix."""
    mv = (p.get("market_value", "") or "").lower().strip()
    num = "".join(c for c in mv if c.isdigit() or c == ".")
    try:
        value = float(num)
    except ValueError:
        return 0.0
    if "k" in mv:
        value = value / 1000.0
    return value


def _fmt_m(v):
    if abs(v) >= 1000:
        return "€{:.2f}b".format(v / 1000)
    return "€{:.1f}m".format(v)


def _fetch_sofascore(name):
    """Fetch SofaScore rating. Returns dict or empty dict on failure."""
    try:
        result = get_rating_for_name(name)
        if result and result.get("rating"):
            return {
                "sofascore_rating": result["rating"],
                "sofascore_id": result.get("sofascore_id"),
            }
    except Exception:
        pass
    return {}


def _surname_key(full_name):
    if not full_name:
        return ""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0].lower()
    surname = parts[-1].lower()
    rest = " ".join(parts[:-1]).lower()
    return "{} {}".format(surname, rest)


_VERDICT_SORT_ORDER = {
    "🔒 Lock Him In": 0,
    "💪 Keep & Build Around": 1,
    "🤔 Hold For Now": 2,
    "⚠️ Consider Selling": 3,
    "🚨 Sell ASAP": 4,
}


def _sort_players(players, col, desc):
    def key(p):
        if col == "market_value":
            v = _mv_num(p)
        elif col == "name":
            v = _surname_key(p.get("name", ""))
        elif col == "position":
            v = POSITION_ORDER.get(p.get("position", ""), 999)
        elif col == "verdict":
            v = _VERDICT_SORT_ORDER.get(p.get("verdict", ""), 999)
        else:
            v = p.get(col)
        if v is None or v == "":
            return (1, 0)
        if isinstance(v, str):
            return (0, v)
        return (0, v)

    return sorted(players, key=key, reverse=desc)


# --------------------------------------------------------------------------- #
# Migrations                                                                  #
# --------------------------------------------------------------------------- #

def _migrate_ratings_if_needed(players, settings):
    if settings.get("rating_scale_v2"):
        return
    changed = []
    for p in players:
        r = p.get("rating")
        if isinstance(r, (int, float)) and 0 < r <= 10:
            p["rating"] = int(r) * 10
            changed.append(p)
    settings["rating_scale_v2"] = True
    save_settings(settings)
    for p in changed:
        save_player(p)


def _migrate_purchase_prices(players, settings):
    """Backfill purchase_price_m and create synthetic buy transactions."""
    if settings.get("purchase_price_migration_done"):
        return
    uid = _current_user_id()
    for p in players:
        if p.get("purchase_price_m") is None:
            mv = _mv_num(p)
            p["purchase_price_m"] = mv
            save_player(p)
            storage.add_transaction(uid, {
                "player_id": p.get("id"),
                "player_name": p.get("name", "Unknown"),
                "type": "buy",
                "deal_value_m": mv,
                "market_value_at_time_m": mv,
            })
    settings["purchase_price_migration_done"] = True
    save_settings(settings)


# --------------------------------------------------------------------------- #
# State                                                                       #
# --------------------------------------------------------------------------- #

def init_state():
    if "players" not in st.session_state:
        # Track activity once per session
        try:
            storage.touch_last_active(_current_user_id())
        except Exception:
            pass
        players = load_players()
        settings = load_settings()
        _migrate_ratings_if_needed(players, settings)
        _migrate_purchase_prices(players, settings)
        st.session_state.players = players
    if "editing_id" not in st.session_state:
        st.session_state.editing_id = None
    if "prefill" not in st.session_state:
        st.session_state.prefill = {}
    if "search_results" not in st.session_state:
        st.session_state.search_results = []
    if "form_version" not in st.session_state:
        st.session_state.form_version = 0
    if "detail_id" not in st.session_state:
        st.session_state.detail_id = None
    if "last_searched" not in st.session_state:
        st.session_state.last_searched = ""
    if "table_sort_col" not in st.session_state:
        settings = load_settings()
        st.session_state.table_sort_col = settings.get("table_sort_col", "rating")
    if "table_sort_desc" not in st.session_state:
        settings = load_settings()
        st.session_state.table_sort_desc = settings.get("table_sort_desc", True)


# --------------------------------------------------------------------------- #
# Buy flow                                                                    #
# --------------------------------------------------------------------------- #

def buy_player_form():
    """Buy form: TM data read-only, user sets deal price + rating + notes."""
    data = st.session_state.get("prefill") or {}
    form_key = "buy_form_v{}".format(st.session_state.form_version)

    # Preview
    c1, c2 = st.columns([1, 3])
    with c1:
        if data.get("photo_url"):
            st.image(data["photo_url"], use_container_width=True)
    with c2:
        flag = country_to_flag(data.get("nationality", ""))
        st.markdown("### {} {}".format(flag, data.get("name", "")))
        line_bits = []
        if data.get("club"):
            line_bits.append("**{}**".format(data["club"]))
        if data.get("league"):
            line_bits.append(data["league"])
        if data.get("position"):
            line_bits.append(data["position"])
        if line_bits:
            st.caption(" · ".join(line_bits))
        meta = []
        if data.get("age"):
            meta.append("🎂 {}".format(data["age"]))
        if data.get("height"):
            meta.append("📏 {}".format(data["height"]))
        if data.get("market_value"):
            meta.append("💰 {}".format(data["market_value"]))
        if meta:
            st.write(" · ".join(meta))
        stats_line = "📊 {} apps · ⚽ {} G · 🅰️ {} A".format(
            data.get("apps", 0), data.get("goals", 0), data.get("assists", 0)
        )
        if data.get("sofascore_rating"):
            stats_line += " · 📈 SofaScore: {}".format(data["sofascore_rating"])
        st.caption(stats_line)
        extras = []
        if data.get("foot"):
            extras.append("🦶 {}".format(data["foot"]))
        if data.get("dob"):
            extras.append("📅 {}".format(data["dob"]))
        if data.get("birthplace"):
            extras.append("📍 {}".format(data["birthplace"]))
        if data.get("on_loan_from"):
            extras.append("🔁 on loan from **{}**".format(data["on_loan_from"]))
        if extras:
            st.markdown(" · ".join(extras))

    # Budget + squad size check
    budget_info = storage.compute_budget(_current_user_id())
    market_val = _mv_num(data)
    cash = budget_info["cash"]
    squad_full = len(st.session_state.players) >= _max_squad()
    if squad_full:
        st.error("Squad is full ({}/{})! Sell a player first.".format(
            len(st.session_state.players), _max_squad()
        ))

    # Duplicate check: already owned?
    tm_url = data.get("tm_url", "")
    already_owned = False
    if tm_url:
        for p in st.session_state.players:
            if p.get("tm_url") == tm_url:
                already_owned = True
                break
    if already_owned:
        st.error("You already own **{}**! Can't buy twice.".format(data.get("name", "this player")))

    deal_price = market_val
    with st.form(form_key, clear_on_submit=True):
        st.markdown("**Deal price: {}**".format(_fmt_m(deal_price)))
        rating = 70  # default, not shown in UI
        notes = st.text_area("Notes", value="")

        after_cash = cash - deal_price
        if deal_price > cash:
            st.error("Not enough budget! Cash: {} · Need: {}".format(
                _fmt_m(cash), _fmt_m(deal_price)
            ))

        st.caption("Cash: {} → {}".format(_fmt_m(cash), _fmt_m(after_cash)))

        col_a, col_b = st.columns([1, 1])
        submitted = col_a.form_submit_button(
            "🛒 Buy for {}".format(_fmt_m(deal_price)),
            use_container_width=True,
            disabled=(deal_price > cash or squad_full or already_owned),
        )
        cancelled = col_b.form_submit_button("Cancel", use_container_width=True)

        if cancelled:
            st.session_state.prefill = {}
            st.session_state.form_version += 1
            st.rerun()

        if submitted and deal_price <= cash and not squad_full and not already_owned:
            if not data.get("name"):
                st.error("Missing player name.")
                return
            record = {
                **data,
                "rating": int(rating),
                "notes": notes.strip(),
                "purchase_price_m": deal_price,
            }
            record.pop("id", None)
            saved = save_player(record)
            storage.add_transaction(_current_user_id(), {
                "player_id": saved.get("id"),
                "player_name": data["name"],
                "type": "buy",
                "deal_value_m": deal_price,
                "market_value_at_time_m": market_val,
            })
            st.session_state.players.append(saved)
            st.session_state.pop("owned_map", None)  # refresh ownership cache
            st.session_state.pop("squad_map_png", None)  # invalidate cached image
            st.session_state.prefill = {}
            st.session_state.form_version += 1
            with st.spinner("✅ Bought {}! Updating analysis...".format(data["name"])):
                run_post_transaction_analysis("buy", data["name"], deal_price)
            st.rerun()


# --------------------------------------------------------------------------- #
# Edit form (rating + notes only)                                             #
# --------------------------------------------------------------------------- #

def player_form(existing=None):
    assert existing is not None, "player_form is edit-only"
    form_key = "player_form_v{}".format(st.session_state.form_version)

    c1, c2 = st.columns([1, 3])
    with c1:
        if existing.get("photo_url"):
            st.image(existing["photo_url"], use_container_width=True)
    with c2:
        flag = country_to_flag(existing.get("nationality", ""))
        st.markdown("### {} {}".format(flag, existing.get("name", "")))
        bits = []
        if existing.get("club"):
            bits.append("**{}**".format(existing["club"]))
        if existing.get("league"):
            bits.append(existing["league"])
        if existing.get("position"):
            bits.append(existing["position"])
        if bits:
            st.caption(" · ".join(bits))
        st.caption("To update TM data, use 🔄 Refresh in the details.")

    with st.form(form_key, clear_on_submit=False):
        rating = int(existing.get("rating") or 70)  # preserved, not editable
        notes = st.text_area("Notes", value=existing.get("notes", ""))
        col_a, col_b = st.columns([1, 1])
        submitted = col_a.form_submit_button("💾 Update", use_container_width=True)
        cancelled = col_b.form_submit_button("Cancel", use_container_width=True)

        if cancelled:
            st.session_state.editing_id = None
            st.rerun()

        if submitted:
            updated = {**existing, "rating": int(rating), "notes": notes.strip()}
            saved = save_player(updated)
            for i, p in enumerate(st.session_state.players):
                if p["id"] == existing["id"]:
                    st.session_state.players[i] = saved
                    break
            st.session_state.editing_id = None
            st.session_state.form_version += 1
            st.success("Updated!")
            st.rerun()


# --------------------------------------------------------------------------- #
# Sell dialog                                                                 #
# --------------------------------------------------------------------------- #

@st.dialog("Sell Player", width="large")
def sell_player_dialog(p):
    flag = country_to_flag(p.get("nationality", ""))
    c1, c2 = st.columns([1, 3])
    with c1:
        if p.get("photo_url"):
            st.image(p["photo_url"], width=100)
    with c2:
        st.markdown("#### {} {}".format(flag, p.get("name", "")))
        if p.get("club"):
            st.caption(p["club"])

    market_val = _mv_num(p)
    purchase = float(p.get("purchase_price_m") or market_val)

    st.markdown("**Bought for:** {}  ·  **Current value:** {}".format(
        _fmt_m(purchase), _fmt_m(market_val)
    ))

    sell_price = market_val
    st.markdown("**Sell price: {}**".format(_fmt_m(sell_price)))

    gain = sell_price - purchase
    if gain >= 0:
        st.success("Gain: +{}".format(_fmt_m(gain)))
    else:
        st.error("Loss: {}".format(_fmt_m(gain)))

    col_a, col_b = st.columns([1, 1])
    if col_a.button(
        "💰 Confirm Sale for {}".format(_fmt_m(sell_price)),
        use_container_width=True,
    ):
        storage.add_transaction(_current_user_id(), {
            "player_id": p.get("id"),
            "player_name": p.get("name", "Unknown"),
            "type": "sell",
            "deal_value_m": sell_price,
            "market_value_at_time_m": market_val,
        })
        delete_player(p["id"])
        st.session_state.players = [
            x for x in st.session_state.players if x["id"] != p["id"]
        ]
        st.session_state.pop("owned_map", None)
        st.session_state.pop("squad_map_png", None)  # invalidate cached image
        with st.spinner("✅ Sold {}! Updating analysis...".format(p["name"])):
            run_post_transaction_analysis("sell", p["name"], sell_price)
        st.rerun()

    if col_b.button("Cancel", use_container_width=True):
        st.rerun()


# --------------------------------------------------------------------------- #
# Search bar                                                                  #
# --------------------------------------------------------------------------- #

def transfermarkt_search_bar():
    # Clear search text if flagged (can't modify after widget renders)
    if st.session_state.pop("clear_search", False):
        st.session_state["tm_search"] = ""
    name = st.text_input(
        "🔎 Search Transfermarkt to buy a player",
        key="tm_search",
        placeholder="e.g. Lamine Yamal, Haaland, Palestra...",
    )
    # Search on any change (typing triggers rerun in Streamlit)
    if name and name != st.session_state.last_searched:
        st.session_state.last_searched = name
        if len(name) >= 3:
            try:
                with st.spinner("Searching..."):
                    st.session_state.search_results = search_player(name)
                storage.increment_search_count(_current_user_id())
                if not st.session_state.search_results:
                    st.warning("No players found.")
            except Exception as e:
                st.error("Search failed: {}".format(e))
    elif not name:
        st.session_state.last_searched = ""
        st.session_state.search_results = []

    if st.session_state.search_results:
        seen = set()
        unique = []
        for r in st.session_state.search_results:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            unique.append(r)

        st.caption("Pick a player to buy:")
        # Header
        hc = st.columns([2.5, 2, 1, 0.8, 1.2, 1.5, 0.4])
        hc[0].markdown("<small><b>Name</b></small>", unsafe_allow_html=True)
        hc[1].markdown("<small><b>Club</b></small>", unsafe_allow_html=True)
        hc[2].markdown("<small><b>Pos</b></small>", unsafe_allow_html=True)
        hc[3].markdown("<small><b>Age</b></small>", unsafe_allow_html=True)
        hc[4].markdown("<small><b>Value</b></small>", unsafe_allow_html=True)
        hc[5].markdown("<small><b>Owners</b></small>", unsafe_allow_html=True)
        hc[6].markdown("<small><b>TM</b></small>", unsafe_allow_html=True)

        # Sort: players with value first, then without
        unique.sort(key=lambda r: (0 if r.get("value") else 1, r.get("name", "")))

        for i, r in enumerate(unique[:8]):
            club = r.get("club", "")
            value = r.get("value", "").strip()
            is_retired = club.lower() in ("retired", "career break", "without club", "")
            has_no_value = not value or value == "-" or value == "€0"
            rc = st.columns([2.5, 2, 1, 0.8, 1.2, 1.5, 0.4])
            rc[1].write(club)
            rc[2].write(r.get("position", ""))
            rc[3].write(r.get("age", ""))
            rc[4].write(r.get("value", ""))
            badge = _ownership_badge(r.get("url"), st.session_state.get("owned_map", {}))
            rc[5].caption(badge if badge else "—")
            rc[6].markdown(
                "<a href='{}' target='_blank'>🔗</a>".format(r["url"]),
                unsafe_allow_html=True,
            )
            if is_retired or has_no_value:
                label = r["name"]
                if is_retired:
                    label += " (retired)"
                elif has_no_value:
                    label += " (no value)"
                rc[0].caption("~~{}~~".format(label))
                continue
            if rc[0].button(r["name"], key="pick_{}".format(i), use_container_width=True):
                try:
                    with st.spinner("Loading player..."):
                        # Check if player already exists in our DB (any user)
                        existing = storage.get_player_by_tm_url(r["url"])
                        if existing:
                            # Serve from DB — no TM call needed
                            data = dict(existing)
                            data.pop("id", None)
                            data.pop("user_id", None)
                            data.pop("purchase_price_m", None)
                            data.pop("verdict", None)
                            data.pop("verdict_reason", None)
                            data.pop("rating", None)
                            data.pop("notes", None)
                            data.pop("created_at", None)
                            data.pop("updated_at", None)
                        else:
                            # New player — scrape from TM
                            data = scrape_player(r["url"])
                            ss = _fetch_sofascore(data.get("name", r["name"]))
                            data.update(ss)
                    st.session_state.prefill = data
                    st.session_state.form_version += 1
                    st.session_state.search_results = []
                    st.session_state.last_searched = ""
                    st.session_state["clear_search"] = True
                    st.rerun()
                except Exception as e:
                    st.error("Couldn't load: {}".format(e))

    if st.session_state.get("prefill"):
        if st.button("🗑️ Clear", use_container_width=True):
            st.session_state.prefill = {}
            st.session_state.form_version += 1
            st.rerun()


# --------------------------------------------------------------------------- #
# Table + Cards                                                               #
# --------------------------------------------------------------------------- #

TABLE_WIDTHS = [0.4, 0.3, 2.0, 1.4, 1.0, 1.4, 0.5, 1.0, 0.5, 0.4, 0.4, 0.5, 0.5, 0.3, 0.3]
TABLE_COLS = [
    ("", None, False),
    ("", None, False),
    ("Name", "name", False),
    ("Club", "club", False),
    ("Lge", "league", False),
    ("Pos", "position", False),
    ("Age", "age", True),
    ("Value", "market_value", True),
    ("📈", "sofascore_rating", True),
    ("G", "goals", True),
    ("A", "assists", True),
    ("App", "apps", True),
    ("⚖️", "verdict", False),
    ("", None, False),
    ("", None, False),
]


def player_table(players):
    col = st.session_state.table_sort_col
    desc = st.session_state.table_sort_desc
    owned_map = st.session_state.get("owned_map", {})

    header = st.columns(TABLE_WIDTHS)
    for i, (lab, sort_key, _numeric) in enumerate(TABLE_COLS):
        if sort_key is None:
            header[i].markdown(
                "<small><b>{}</b></small>".format(lab), unsafe_allow_html=True
            )
        else:
            arrow = ""
            if col == sort_key:
                arrow = "↓" if desc else "↑"
            with header[i]:
                if st.button(
                    "{}{}".format(lab, arrow),
                    key="hdr_{}".format(sort_key),
                    use_container_width=True,
                ):
                    if col == sort_key:
                        st.session_state.table_sort_desc = not desc
                    else:
                        st.session_state.table_sort_col = sort_key
                        _, _, numeric = TABLE_COLS[i]
                        st.session_state.table_sort_desc = numeric
                    settings = load_settings()
                    settings["table_sort_col"] = st.session_state.table_sort_col
                    settings["table_sort_desc"] = st.session_state.table_sort_desc
                    save_settings(settings)
                    st.rerun()
    st.markdown(
        "<hr style='margin:0.25rem 0 0.5rem 0'>", unsafe_allow_html=True
    )

    for p in players:
        c = st.columns(TABLE_WIDTHS)
        with c[0]:
            if p.get("photo_url"):
                st.image(p["photo_url"], width=36)
        c[1].markdown(
            "<div style='font-size:1.3em;padding-top:6px'>{}</div>".format(
                country_to_flag(p.get("nationality", ""))
            ),
            unsafe_allow_html=True,
        )
        with c[2]:
            if st.button(
                p.get("name", ""),
                key="tbl_name_{}".format(p["id"]),
                use_container_width=True,
                help="Click for details",
            ):
                st.session_state.detail_id = p["id"]
                st.rerun()
        c[3].write(p.get("club", ""))
        c[4].write(p.get("league", "") or "")
        pos = p.get("position", "")
        prefix = ROLE_PREFIX.get(pos, "")
        c[5].write("{} {}".format(prefix, pos).strip() if pos else "")
        _r = "<div style='text-align:right'>{}</div>"
        c[6].markdown(_r.format(p.get("age", "") or ""), unsafe_allow_html=True)
        c[7].markdown(_r.format(p.get("market_value", "") or ""), unsafe_allow_html=True)
        ss_val = p.get("sofascore_rating", "")
        if ss_val:
            ss_f = float(ss_val)
            ss_color = "#4CAF50" if ss_f >= 7.5 else "#FFC107" if ss_f >= 7.0 else "#F44336"
            c[8].markdown(
                "<div style='text-align:right;color:{}'>{}</div>".format(ss_color, ss_val),
                unsafe_allow_html=True,
            )
        else:
            c[8].write("")
        c[9].markdown(_r.format(p.get("goals", 0)), unsafe_allow_html=True)
        c[10].markdown(_r.format(p.get("assists", 0)), unsafe_allow_html=True)
        c[11].markdown(_r.format(p.get("apps", 0)), unsafe_allow_html=True)
        verdict = p.get("verdict", "")
        if verdict:
            emoji = verdict.split(" ")[0]
            c[12].markdown(
                "<span title='{}' style='font-size:1.2em'>{}</span>".format(verdict, emoji),
                unsafe_allow_html=True,
            )
        else:
            c[12].write("")
        tm_url = p.get("tm_url", "")
        others = owned_map.get(tm_url, []) if tm_url else []
        # Col 13: TM link
        if tm_url:
            c[13].markdown(
                "<a href='{}' target='_blank' style='text-decoration:none'>🔗</a>".format(tm_url),
                unsafe_allow_html=True,
            )
        else:
            c[13].write("")
        # Col 14: ownership indicator
        if others:
            c[14].write("⚡")


def player_card(p):
    with st.container(border=True):
        c1, c2 = st.columns([1, 3])
        with c1:
            if p.get("photo_url"):
                st.image(p["photo_url"], use_container_width=True)
            else:
                st.markdown("🧍")
        with c2:
            flag = country_to_flag(p.get("nationality", ""))
            st.markdown("### {} {}".format(flag, p["name"]).strip())
            bits = []
            if p.get("club"):
                club_bit = "**{}**".format(p["club"])
                if p.get("league"):
                    club_bit += " _({})_".format(p["league"])
                bits.append(club_bit)
            if p.get("position"):
                bits.append(p["position"])
            if p.get("nationality"):
                bits.append(p["nationality"])
            if bits:
                st.caption(" · ".join(bits))

            meta_line = []
            if p.get("age"):
                meta_line.append("🎂 {}".format(p["age"]))
            if p.get("height"):
                meta_line.append("📏 {}".format(p["height"]))
            if p.get("market_value"):
                meta_line.append("💰 {}".format(p["market_value"]))
            if meta_line:
                st.write(" · ".join(meta_line))

            st.write(
                "⚽ {} G · 🅰️ {} A · 👕 {} apps".format(
                    p.get("goals", 0), p.get("assists", 0), p.get("apps", 0)
                )
            )

            if p.get("notes"):
                with st.expander("Notes"):
                    st.write(p["notes"])

            b1, _ = st.columns([1, 4])
            if b1.button("🔍 Details", key="det_{}".format(p["id"])):
                st.session_state.detail_id = p["id"]
                st.rerun()
            if p.get("tm_url"):
                st.markdown(
                    "<a href='{}' target='_blank' style='font-size:0.85em'>🔗 Transfermarkt</a>".format(
                        p["tm_url"]
                    ),
                    unsafe_allow_html=True,
                )


# --------------------------------------------------------------------------- #
# Detail dialog                                                               #
# --------------------------------------------------------------------------- #

@st.dialog("Player details", width="large")
def player_detail_dialog(p):
    flag = country_to_flag(p.get("nationality", ""))
    c1, c2 = st.columns([1, 3])
    with c1:
        if p.get("photo_url"):
            st.image(p["photo_url"], use_container_width=True)
    with c2:
        st.markdown("#### {} {}".format(flag, p.get("name", "")))
        club_line = []
        if p.get("club"):
            club_line.append("**{}**".format(p["club"]))
        if p.get("league"):
            club_line.append(p["league"])
        if p.get("position"):
            club_line.append(p["position"])
        if club_line:
            st.caption(" · ".join(club_line))

        line2 = []
        if p.get("age"):
            line2.append("🎂 {}".format(p["age"]))
        if p.get("height"):
            line2.append("📏 {}".format(p["height"]))
        if p.get("foot"):
            line2.append("🦶 {}".format(p["foot"]))
        if p.get("market_value"):
            line2.append("💰 {}".format(p["market_value"]))
        if p.get("nationality"):
            line2.append("{} {}".format(flag, p["nationality"]))
        if line2:
            st.caption(" · ".join(line2))

        line3 = []
        if p.get("dob"):
            line3.append("📅 {}".format(p["dob"]))
        if p.get("birthplace"):
            line3.append("📍 {}".format(p["birthplace"]))
        if p.get("on_loan_from"):
            line3.append("🔁 loan from **{}**".format(p["on_loan_from"]))
        if p.get("tm_url"):
            line3.append("[🔗 Transfermarkt]({})".format(p["tm_url"]))
        if p.get("sofascore_id"):
            line3.append("[📈 SofaScore](https://www.sofascore.com/player/{})".format(p["sofascore_id"]))
        if line3:
            st.caption(" · ".join(line3))

        # Stats + purchase info
        purchase = float(p.get("purchase_price_m") or _mv_num(p))
        current = _mv_num(p)
        gain = current - purchase
        gain_str = "+{}".format(_fmt_m(gain)) if gain >= 0 else _fmt_m(gain)
        stats_md = "👕 {} apps  ·  ⚽ {} G  ·  🅰️ {} A".format(
            p.get("apps", 0), p.get("goals", 0), p.get("assists", 0)
        )
        if p.get("sofascore_rating"):
            stats_md += "  ·  📈 SofaScore: **{}**".format(p["sofascore_rating"])
        st.markdown(stats_md)
        st.caption(
            "Bought: {}  ·  Now: {}  ·  {}".format(
                _fmt_m(purchase), _fmt_m(current), gain_str
            )
        )

        if p.get("verdict"):
            reason = p.get("verdict_reason", "")
            if reason:
                st.markdown("**{}**  \n_{}_ ".format(p["verdict"], reason))
            else:
                st.markdown("**{}**".format(p["verdict"]))

        if p.get("notes"):
            st.caption("📝 {}".format(p["notes"]))

        badge = _ownership_badge(p.get("tm_url"), st.session_state.get("owned_map", {}))
        if badge:
            st.caption(badge)

    st.divider()
    is_adm = storage.is_admin(_current_user_id())
    a1, a2, a3, a4, _ = st.columns([1.2, 1, 1, 1, 1])
    if is_adm and a1.button(
        "🔄 Refresh",
        key="dlg_refresh_{}".format(p["id"]),
        use_container_width=True,
        disabled=not p.get("tm_url"),
    ):
        try:
            with st.spinner("Refreshing..."):
                # Invalidate cache for this player
                stats_u = p["tm_url"].replace("/profil/spieler/", "/leistungsdaten/spieler/")
                storage.tm_cache_invalidate([p["tm_url"], stats_u])
                fresh = scrape_player(p["tm_url"])
                ss = _fetch_sofascore(fresh.get("name", p.get("name", "")))
                fresh.update(ss)
            preserved = {
                "id": p["id"],
                "rating": p.get("rating"),
                "notes": p.get("notes", ""),
                "purchase_price_m": p.get("purchase_price_m"),
            }
            merged = {**p, **fresh, **preserved}
            saved = save_player(merged)
            for i, x in enumerate(st.session_state.players):
                if x["id"] == p["id"]:
                    st.session_state.players[i] = saved
                    break
            st.success("Refreshed!")
            st.rerun()
        except Exception as e:
            st.error("Refresh failed: {}".format(e))
    if a2.button("✏️ Edit", key="dlg_edit_{}".format(p["id"]), use_container_width=True):
        st.session_state.editing_id = p["id"]
        st.rerun()
    if a3.button("💰 Sell", key="dlg_sell_{}".format(p["id"]), use_container_width=True):
        st.session_state["selling_id"] = p["id"]
        st.rerun()
    if a4.button(
        "🗑️ Delete", key="dlg_del_{}".format(p["id"]),
        use_container_width=True, type="secondary",
    ):
        delete_player(p["id"])
        st.session_state.players = [
            x for x in st.session_state.players if x["id"] != p["id"]
        ]
        st.session_state.pop("squad_map_png", None)
        st.session_state.pop("owned_map", None)
        st.rerun()


# --------------------------------------------------------------------------- #
# Refresh all                                                                 #
# --------------------------------------------------------------------------- #

@st.dialog("Refresh All Players", width="large")
def refresh_all_players():
    """Admin only: refresh ALL players across ALL users from TM + SofaScore."""
    all_players = storage.list_all_players()
    tm_players = [p for p in all_players if p.get("tm_url")]
    if not tm_players:
        st.warning("No players with Transfermarkt URLs.")
        return

    # Invalidate TM cache for all player URLs + their stats pages
    tm_urls = [p["tm_url"] for p in tm_players]
    stats_urls = [
        u.replace("/profil/spieler/", "/leistungsdaten/spieler/") for u in tm_urls
    ]
    storage.tm_cache_invalidate(tm_urls + stats_urls)

    # Dedupe by tm_url to avoid scraping the same player twice
    seen_urls = {}
    unique_players = []
    for p in tm_players:
        url = p["tm_url"]
        if url not in seen_urls:
            seen_urls[url] = []
            unique_players.append(p)
        seen_urls[url].append(p)

    st.markdown("**{} unique players** to refresh ({} total across all users)".format(
        len(unique_players), len(tm_players)
    ))

    log = st.container(height=400)
    progress = st.progress(0)
    status = st.empty()

    ok = 0
    fail = 0
    failed_names = []
    for idx, p in enumerate(unique_players):
        pct = (idx + 1) / len(unique_players)
        progress.progress(pct)
        owners = len(seen_urls[p["tm_url"]])
        status.caption("({}/{}) Refreshing **{}**...".format(idx + 1, len(unique_players), p["name"]))

        try:
            fresh = scrape_player(p["tm_url"])
            ss = _fetch_sofascore(fresh.get("name", p.get("name", "")))
            fresh.update(ss)

            # Update ALL players with this tm_url (across all users)
            for dup in seen_urls[p["tm_url"]]:
                preserved = {
                    "id": dup["id"],
                    "user_id": dup["user_id"],
                    "rating": dup.get("rating"),
                    "notes": dup.get("notes", ""),
                    "purchase_price_m": dup.get("purchase_price_m"),
                    "verdict": dup.get("verdict"),
                    "verdict_reason": dup.get("verdict_reason"),
                }
                merged = {**dup, **fresh, **preserved}
                storage.upsert_player(dup["user_id"], merged)

            ok += 1
            log.markdown("✅ **{}** — {} · SofaScore {} · {} owner{}".format(
                p["name"], fresh.get("market_value", "?"),
                fresh.get("sofascore_rating", "n/a"),
                owners, "s" if owners > 1 else "",
            ))
        except Exception as e:
            fail += 1
            failed_names.append(p["name"])
            log.markdown("❌ **{}** — {}".format(p["name"], str(e)[:60]))

        if idx < len(unique_players) - 1:
            time.sleep(2.5)

    progress.progress(1.0)
    status.empty()

    # Summary
    st.divider()
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    storage.update_app_settings({"last_refresh_at": now})
    st.session_state.pop("app_settings", None)
    st.success("✅ Done! {} refreshed, {} failed.".format(ok, fail))
    if failed_names:
        st.warning("Failed: {}".format(", ".join(failed_names)))

    # Reload current user's players
    st.session_state.players = load_players()
    st.session_state.pop("squad_map_png", None)
    if st.button("Close", use_container_width=True):
        st.rerun()


# --------------------------------------------------------------------------- #
# Transactions tab                                                            #
# --------------------------------------------------------------------------- #

def transactions_tab():
    txns = storage.list_transactions(_current_user_id())
    if not txns:
        st.info("No transactions yet. Buy your first player!")
        return

    # Summary
    buys = [t for t in txns if t.get("type") == "buy"]
    sells = [t for t in txns if t.get("type") == "sell"]
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Transactions", len(txns))
    s2.metric("Buys", len(buys))
    s3.metric("Sells", len(sells))
    net = sum(float(t.get("deal_value_m", 0)) for t in sells) - sum(
        float(t.get("deal_value_m", 0)) for t in buys
    )
    s4.metric("Net", _fmt_m(net))

    st.divider()

    # Table
    for t in txns:
        is_buy = t.get("type") == "buy"
        icon = "🛒" if is_buy else "💸"
        color = "#F44336" if is_buy else "#4CAF50"
        ts = t.get("created_at", "")
        if isinstance(ts, str) and len(ts) > 16:
            ts = ts[:16].replace("T", " ")
        c1, c2, c3, c4 = st.columns([1.5, 3, 1.5, 1.5])
        c1.caption(ts)
        c2.markdown(
            "{} **{}** — <span style='color:{}'>{}</span>".format(
                icon, t.get("player_name", "?"), color, t.get("type", "?").upper()
            ),
            unsafe_allow_html=True,
        )
        c3.markdown(
            "<b style='color:{}'>{}</b>".format(
                color, _fmt_m(float(t.get("deal_value_m", 0)))
            ),
            unsafe_allow_html=True,
        )
        mv = t.get("market_value_at_time_m")
        c4.caption("MV: {}".format(_fmt_m(float(mv))) if mv else "")


# --------------------------------------------------------------------------- #
# Squad analysis (Claude)                                                     #
# --------------------------------------------------------------------------- #

def _build_tactics_summary(players):
    """Build a text summary of the user's chosen formation for Claude."""
    saved_formation, saved_overrides = _get_user_formation_data()
    formations = storage.get_formations()
    if not saved_formation or not formations:
        return ""
    formation = next((f for f in formations if f["name"] == saved_formation), None)
    if not formation:
        return ""

    overrides = saved_overrides.get(saved_formation, {})
    assignments = _auto_assign_formation(formation, players)
    player_by_id = {str(p.get("id")): p for p in players}
    for i, (slot, auto_p) in enumerate(assignments):
        oid = overrides.get(str(i))
        if oid and str(oid) in player_by_id:
            assignments[i][1] = player_by_id[str(oid)]

    lines = ["PREFERRED FORMATION: {}".format(saved_formation)]
    for slot, player in assignments:
        compat = _slot_compatibility(slot["role"], player.get("position")) if player else "empty"
        name = player.get("name", "EMPTY") if player else "EMPTY"
        lines.append("  {} -> {} [{}]".format(slot["slot"], name, compat))

    assigned_ids = {str(p.get("id")) for _, p in assignments if p}
    bench = [p for p in players if str(p.get("id")) not in assigned_ids]
    if bench:
        lines.append("  BENCH: {}".format(", ".join(p.get("name", "?") for p in bench)))
    return "\n".join(lines)


def _build_squad_summary(players, budget_info):
    """Build a text summary of the squad for Claude."""
    lines = []
    lines.append("SQUAD ROSTER ({}/{} players)".format(len(players), _max_squad()))
    lines.append("Budget: {} initial | {} invested | {} cash remaining".format(
        _fmt_m(budget_info["initial"]),
        _fmt_m(budget_info["total_buys"]),
        _fmt_m(budget_info["cash"]),
    ))
    portfolio = sum(_mv_num(p) for p in players)
    net = budget_info["total_buys"] - budget_info["total_sells"]
    lines.append("Portfolio value: {} | Gain/Loss: {}".format(
        _fmt_m(portfolio), _fmt_m(portfolio - net)
    ))
    lines.append("Max squad size: {} | Slots remaining: {}".format(
        _max_squad(), _max_squad() - len(players)
    ))

    # Include tactics
    tactics = _build_tactics_summary(players)
    if tactics:
        lines.append("")
        lines.append(tactics)

    lines.append("")
    for p in players:
        role = ROLE_PREFIX.get(p.get("position", ""), "??")
        ss = p.get("sofascore_rating", "n/a")
        pp = p.get("purchase_price_m")
        pp_str = _fmt_m(float(pp)) if pp else "?"
        lines.append(
            "- {name} | {role} {pos} | {club} ({league}) | Age {age} | "
            "Value {mv} | Bought {pp} | SofaScore {ss} | Rating {r}/100 | "
            "{apps} apps, {g}G {a}A".format(
                name=p.get("name", "?"),
                role=role,
                pos=p.get("position", "?"),
                club=p.get("club", "?"),
                league=p.get("league", "?"),
                age=p.get("age", "?"),
                mv=p.get("market_value", "?"),
                pp=pp_str,
                ss=ss,
                r=p.get("rating", "?"),
                apps=p.get("apps", 0),
                g=p.get("goals", 0),
                a=p.get("assists", 0),
            )
        )
    return "\n".join(lines)


def _run_analysis(players=None, user_q="", lang=None, context_msg=""):
    """Run Claude analysis on the squad. Returns analysis text or None."""
    try:
        api_key = st.secrets["app"]["anthropic_api_key"]
    except Exception:
        return None

    if players is None:
        players = st.session_state.players
    if not players:
        return None

    if lang is None:
        lang = _get_analysis_lang()

    budget_info = storage.compute_budget(_current_user_id())
    squad_text = _build_squad_summary(players, budget_info)

    lang_instruction = ""
    if lang == "Italiano":
        lang_instruction = " Respond entirely in Italian."

    is_question = bool(user_q) or bool(context_msg)

    custom_prompt = _analysis_prompt_override()

    profile = storage.get_profile(_current_user_id())
    user_name = profile.get("first_name") or profile.get("nickname") or "the user"
    yob = profile.get("year_of_birth")
    import datetime as _dt
    user_age = _dt.date.today().year - int(yob) if yob else "unknown"

    # Resolve the analysis criteria (custom prompt or default) for context in all modes
    criteria_prompt = custom_prompt if custom_prompt else DEFAULT_ANALYSIS_PROMPT
    criteria_prompt = criteria_prompt.replace("{max_squad}", str(_max_squad()))
    criteria_prompt = criteria_prompt.replace("{lang}", "")
    criteria_prompt = criteria_prompt.replace("{user_name}", str(user_name))
    criteria_prompt = criteria_prompt.replace("{user_age}", str(user_age))

    if is_question:
        # Focused answer mode: answer the specific question using all available data
        system_prompt = (
            "You are a football scout and squad analyst for a fantasy football game. "
            "The user is {user_name}, a {user_age}-year-old who loves football. "
            "You have full access to their squad data, budget, ratings, and statistics. "
            "The max squad size is {max_squad}. "
            "\n\nYour evaluation philosophy and criteria:\n{criteria}\n\n"
            "IMPORTANT: The squad data provided below is the LATEST and most accurate information. "
            "Player clubs, leagues, values, and stats in the data reflect real-time transfers and current season. "
            "Always trust the provided data over your training knowledge for player info. "
            "When suggesting players to buy, you may use your football knowledge but note that "
            "clubs and values may have changed — suggest based on player quality and role needs.\n\n"
            "Answer the user's question directly and concisely, using the squad data to support your answer. "
            "Consider: current squad composition, missing positions, budget available, "
            "player ratings (both user's rating and SofaScore), age balance, and transfer market values. "
            "Be specific — name actual players from the squad when relevant. "
            "Adapt language to the user's age. Keep it short and actionable.{lang}"
        )
        system_prompt = system_prompt.replace("{criteria}", criteria_prompt)
        system_prompt = system_prompt.replace("{max_squad}", str(_max_squad()))
        system_prompt = system_prompt.replace("{lang}", lang_instruction)
        system_prompt = system_prompt.replace("{user_name}", str(user_name))
        system_prompt = system_prompt.replace("{user_age}", str(user_age))
    else:
        # Full assessment mode
        prompt_template = custom_prompt if custom_prompt else DEFAULT_ANALYSIS_PROMPT
        # Safe replacement — handles custom prompts with stray {braces}
        system_prompt = prompt_template
        system_prompt = system_prompt.replace("{max_squad}", str(_max_squad()))
        system_prompt = system_prompt.replace("{lang}", lang_instruction)
        system_prompt = system_prompt.replace("{user_name}", str(user_name))
        system_prompt = system_prompt.replace("{user_age}", str(user_age))

    user_msg = (
        "IMPORTANT: This data is live and up-to-date (current clubs, leagues, "
        "values, and stats reflect the latest transfers and season data).\n\n"
        "Here is my current squad:\n\n{}\n".format(squad_text)
    )
    if context_msg:
        user_msg += "\n{}\n".format(context_msg)
    if user_q:
        user_msg += "\n{}\n".format(user_q)

    with st.expander("🔍 Prompt sent to Claude", expanded=False):
        st.text(system_prompt)
        st.divider()
        st.text(user_msg)

    try:
        import anthropic

        # Sonnet for full assessments, Haiku for questions/post-txn
        model = "claude-haiku-4-5-20251001" if is_question else "claude-sonnet-4-20250514"

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        analysis = response.content[0].text
        # Save only full assessments (no question, no context)
        if not is_question:
            storage.save_last_analysis(_current_user_id(), analysis)
            _generate_and_save_verdicts()
        return analysis
    except Exception as e:
        st.error("Analysis error: {}".format(e))
        return None


VERDICT_TIERS = [
    "🔒 Lock Him In",
    "💪 Keep & Build Around",
    "🤔 Hold For Now",
    "⚠️ Consider Selling",
    "🚨 Sell ASAP",
]


def _generate_and_save_verdicts():
    """Second call: ask Sonnet for structured JSON verdicts per player.
    Uses the same analysis prompt as context so verdicts align with the assessment criteria."""
    players = st.session_state.get("players", [])
    if not players:
        return

    try:
        api_key = st.secrets["app"]["anthropic_api_key"]
    except Exception:
        return

    budget_info = storage.compute_budget(_current_user_id())
    squad_text = _build_squad_summary(players, budget_info)

    lang = _get_analysis_lang()
    lang_instruction = " Respond in Italian." if lang == "Italiano" else ""

    # Get the analysis prompt (custom or default) for context — safe replace
    custom_prompt = _analysis_prompt_override()
    analysis_context = custom_prompt if custom_prompt else DEFAULT_ANALYSIS_PROMPT
    analysis_context = analysis_context.replace("{max_squad}", str(_max_squad()))
    analysis_context = analysis_context.replace("{lang}", "")
    analysis_context = analysis_context.replace("{user_name}", "the user")
    analysis_context = analysis_context.replace("{user_age}", "unknown")

    system_prompt = (
        "You are a football squad analyst. You evaluate players using these criteria:\n\n"
        "{analysis_context}\n\n"
        "Based on these criteria, assign EACH player in the squad "
        "exactly ONE of these verdict tiers:\n"
        "- 🔒 Lock Him In — essential to the squad, don't sell\n"
        "- 💪 Keep & Build Around — strong contributor, sell only for amazing offer\n"
        "- 🤔 Hold For Now — decent but monitor closely\n"
        "- ⚠️ Consider Selling — underperforming or overpriced, look for upgrade\n"
        "- 🚨 Sell ASAP — free up budget for better options\n\n"
        "Consider: squad composition needs, position coverage, age balance, "
        "SofaScore rating vs league averages, market value trend (bought vs current), "
        "goals/assists contribution, and budget constraints.\n\n"
        "Respond ONLY with valid JSON — an array of objects with keys: "
        "\"name\" (exact player name from the data), "
        "\"verdict\" (one of the 5 tiers above), and "
        "\"reason\" (one concise sentence explaining why, referencing specific stats or squad needs). "
        "No markdown, no explanation, just the JSON array.{lang}"
    )
    system_prompt = system_prompt.replace("{analysis_context}", analysis_context)
    system_prompt = system_prompt.replace("{lang}", lang_instruction)

    user_msg = (
        "IMPORTANT: This data is live and up-to-date (current clubs, leagues, "
        "values, and stats reflect the latest transfers and season data).\n\n"
        "Full squad data:\n\n{}".format(squad_text)
    )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if present
        if "```" in text:
            # Extract content between first ``` and last ```
            parts_raw = text.split("```")
            for part in parts_raw:
                cleaned = part.strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:].strip()
                if cleaned.startswith("["):
                    text = cleaned
                    break
        text = text.strip()
        # Fix common JSON issues: trailing commas
        import re
        text = re.sub(r",\s*([}\]])", r"\1", text)
        # If JSON is truncated (cut off mid-array), try to close it
        if text.count("[") > text.count("]"):
            # Find last complete object
            last_brace = text.rfind("}")
            if last_brace > 0:
                text = text[:last_brace + 1] + "]"
        verdicts = json.loads(text)

        # Build lookup by name (case-insensitive)
        verdict_map = {}
        for v in verdicts:
            verdict_map[v["name"].lower()] = {
                "verdict": v.get("verdict", ""),
                "reason": v.get("reason", ""),
            }

        # Apply to players
        for p in players:
            name = p.get("name", "")
            entry = verdict_map.get(name.lower())
            if entry and entry["verdict"] in VERDICT_TIERS:
                changed = (
                    p.get("verdict") != entry["verdict"]
                    or p.get("verdict_reason") != entry["reason"]
                )
                if changed:
                    p["verdict"] = entry["verdict"]
                    p["verdict_reason"] = entry["reason"]
                    save_player(p)
    except Exception as e:
        # Log to file for debugging
        import traceback
        try:
            with open("/Users/carlodemarchis/Downloads/miles-players/verdict_debug.log", "w") as f:
                f.write("Error: {}\n\n".format(e))
                f.write("Raw text:\n{}\n".format(text if 'text' in dir() else "no response"))
                traceback.print_exc(file=f)
        except Exception:
            pass


def _get_analysis_lang():
    """Return the user's preferred language from profile (fallback to English)."""
    try:
        profile = storage.get_profile(_current_user_id())
        return profile.get("language") or "English"
    except Exception:
        return "English"


def _save_analysis_lang(lang):
    try:
        storage.update_profile(_current_user_id(), {"language": lang})
    except Exception:
        pass


def run_post_transaction_analysis(txn_type, player_name, deal_price=0):
    """After buy/sell: quick Haiku reaction only. Full analysis is manual."""
    players = st.session_state.get("players", [])
    lang = _get_analysis_lang()
    lang_instruction = " Respond entirely in Italian." if lang == "Italiano" else ""

    try:
        api_key = st.secrets["app"]["anthropic_api_key"]
    except Exception:
        return

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    budget_info = storage.compute_budget(_current_user_id())
    squad_text = _build_squad_summary(players, budget_info)

    action = "bought {} for {}".format(player_name, _fmt_m(deal_price)) if txn_type == "buy" else "sold {} for {}".format(player_name, _fmt_m(deal_price))

    # Quick Haiku comment only
    try:
        quick_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "You are a football scout. The user just {action}. "
                "Give a quick 2-3 sentence reaction: was it a good move? Why? "
                "Be enthusiastic and specific. Reference the squad impact.{lang}"
            ).replace("{action}", action).replace("{lang}", lang_instruction),
            messages=[{"role": "user", "content": "Squad:\n{}".format(squad_text)}],
        )
        st.session_state["post_txn_analysis"] = quick_resp.content[0].text
    except Exception:
        pass


def squad_analysis_tab():
    players = st.session_state.players
    if not players:
        st.info("Add some players first to ask questions.")
        return

    # Init chat history
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    # Build system prompt once
    lang = _get_analysis_lang()
    lang_instruction = " Respond entirely in Italian." if lang == "Italiano" else ""
    custom_prompt = _analysis_prompt_override()

    profile = storage.get_profile(_current_user_id())
    user_name = profile.get("first_name") or profile.get("nickname") or "the user"
    yob = profile.get("year_of_birth")
    import datetime as _dt
    user_age = _dt.date.today().year - int(yob) if yob else "unknown"

    criteria_prompt = custom_prompt if custom_prompt else DEFAULT_ANALYSIS_PROMPT
    criteria_prompt = criteria_prompt.replace("{max_squad}", str(_max_squad()))
    criteria_prompt = criteria_prompt.replace("{lang}", "")
    criteria_prompt = criteria_prompt.replace("{user_name}", str(user_name))
    criteria_prompt = criteria_prompt.replace("{user_age}", str(user_age))

    budget_info = storage.compute_budget(_current_user_id())
    squad_text = _build_squad_summary(players, budget_info)

    chat_system = (
        "You are a football scout and squad analyst for a fantasy football game. "
        "The user is {user_name}, a {user_age}-year-old who loves football. "
        "You have full access to their squad data, budget, ratings, and statistics. "
        "The max squad size is {max_squad}. "
        "\n\nYour evaluation philosophy and criteria:\n{criteria}\n\n"
        "IMPORTANT: The squad data provided is live and up-to-date (current clubs, "
        "leagues, values, and stats reflect the latest transfers and season data). "
        "Always trust the provided data over your training knowledge for player info.\n\n"
        "Be conversational. Answer questions concisely using squad data. "
        "Be specific — name actual players when relevant. "
        "Adapt language to the user's age. "
        "You can suggest follow-ups and offer to dig deeper.\n\n"
        "CONSISTENCY: Below is the latest full squad analysis report and player verdicts. "
        "Your answers MUST be consistent with this report. If you disagree with something, "
        "explain why new information changes the assessment, don't silently contradict it.\n\n"
        "{last_report}\n\n"
        "{player_verdicts}\n\n"
        "{lang}\n\n"
        "Current squad data:\n{squad_data}"
    )
    # Build player verdicts summary
    verdict_lines = []
    for p in players:
        v = p.get("verdict")
        if v:
            reason = p.get("verdict_reason", "")
            line = "{} — {}".format(p.get("name", "?"), v)
            if reason:
                line += " ({})".format(reason)
            verdict_lines.append(line)
    verdicts_text = (
        "PLAYER VERDICTS FROM LATEST REPORT:\n" + "\n".join(verdict_lines)
        if verdict_lines else ""
    )

    # Get last full report
    last_report = storage.get_last_analysis(_current_user_id()) or ""
    if last_report:
        last_report = "LATEST FULL ANALYSIS REPORT:\n" + last_report

    chat_system = chat_system.replace("{user_name}", str(user_name))
    chat_system = chat_system.replace("{user_age}", str(user_age))
    chat_system = chat_system.replace("{max_squad}", str(_max_squad()))
    chat_system = chat_system.replace("{criteria}", criteria_prompt)
    chat_system = chat_system.replace("{last_report}", last_report)
    chat_system = chat_system.replace("{player_verdicts}", verdicts_text)
    chat_system = chat_system.replace("{lang}", lang_instruction)
    chat_system = chat_system.replace("{squad_data}", squad_text)

    # Chat container with scrollable history + fixed input at bottom
    chat_container = st.container(height=500)
    with chat_container:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    user_input = st.chat_input("Ask about your squad...")
    if user_input:
        st.session_state.chat_messages.append({"role": "user", "content": user_input})

        # Build messages for API (full conversation history)
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.chat_messages
        ]

        # Call Claude
        try:
            api_key = st.secrets["app"]["anthropic_api_key"]
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                system=chat_system,
                messages=api_messages,
            )
            reply = response.content[0].text
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": reply}
            )
        except Exception as e:
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": "Error: {}".format(e)}
            )
        st.rerun()




# --------------------------------------------------------------------------- #
# Tactics tab                                                                 #
# --------------------------------------------------------------------------- #

def _slot_compatibility(slot_role, player_position):
    """Return compatibility: 'exact', 'similar', 'mismatch'."""
    if not player_position:
        return "mismatch"
    if player_position == slot_role:
        return "exact"
    if ROLE_PREFIX.get(player_position, "X") == ROLE_PREFIX.get(slot_role, "Y"):
        return "similar"
    return "mismatch"


_COMPAT_COLORS = {"exact": "#2d8a4e", "similar": "#d4a017", "mismatch": "#cc3333"}
_COMPAT_ICONS = {"exact": "🟢", "similar": "🟡", "mismatch": "🔴"}


def _auto_assign_formation(formation, players):
    """Auto-assign players to formation slots. Returns list of (slot, player_or_None)."""
    available = list(players)
    assignments = []
    # First pass: exact matches
    for slot in formation["positions"]:
        best = None
        best_score = -1
        for p in available:
            pos = p.get("position", "")
            if pos == slot["role"]:
                ss = float(p.get("sofascore_rating") or 0)
                if ss > best_score:
                    best = p
                    best_score = ss
        if best:
            available.remove(best)
        assignments.append([slot, best])
    # Second pass: fill empty slots with similar role
    for i, (slot, player) in enumerate(assignments):
        if player is not None:
            continue
        best = None
        best_score = -1
        for p in available:
            pos = p.get("position", "")
            if ROLE_PREFIX.get(pos, "X") == ROLE_PREFIX.get(slot["role"], "Y"):
                ss = float(p.get("sofascore_rating") or 0)
                if ss > best_score:
                    best = p
                    best_score = ss
        if best:
            available.remove(best)
            assignments[i][1] = best
    return assignments


def _render_pitch(formation, assignments):
    """Render a football pitch with player positions — same style as squad map."""
    parts = []
    # Dark pitch with stripes
    parts.append(
        '<div style="position:relative;width:100%;max-width:700px;'
        'aspect-ratio:68/84;'
        'background:repeating-linear-gradient(to bottom,'
        '#1a6b35 0%,#1a6b35 8.33%,#1f7a3d 8.33%,#1f7a3d 16.66%);'
        'border:3px solid rgba(255,255,255,0.6);border-radius:8px;overflow:hidden;'
        'box-shadow:inset 0 0 40px rgba(0,0,0,0.3),0 4px 12px rgba(0,0,0,0.3);">'
    )
    # Pitch markings
    parts.append('<div style="position:absolute;top:50%;left:5%;right:5%;height:2px;background:rgba(255,255,255,0.7);"></div>')
    parts.append('<div style="position:absolute;top:50%;left:50%;width:70px;height:70px;border:2px solid rgba(255,255,255,0.7);border-radius:50%;transform:translate(-50%,-50%);"></div>')
    parts.append('<div style="position:absolute;top:50%;left:50%;width:6px;height:6px;background:rgba(255,255,255,0.7);border-radius:50%;transform:translate(-50%,-50%);"></div>')
    parts.append('<div style="position:absolute;top:0;left:22%;right:22%;height:14%;border:2px solid rgba(255,255,255,0.7);border-top:none;"></div>')
    parts.append('<div style="position:absolute;top:0;left:34%;right:34%;height:7%;border:2px solid rgba(255,255,255,0.7);border-top:none;"></div>')
    parts.append('<div style="position:absolute;bottom:0;left:22%;right:22%;height:14%;border:2px solid rgba(255,255,255,0.7);border-bottom:none;"></div>')
    parts.append('<div style="position:absolute;bottom:0;left:34%;right:34%;height:7%;border:2px solid rgba(255,255,255,0.7);border-bottom:none;"></div>')

    for slot, player in assignments:
        x = slot["x"]
        y = slot["y"]
        surname = player.get("name", "?").split()[-1] if player else "—"
        ss = str(player.get("sofascore_rating", "")) if player else ""
        verdict = player.get("verdict", "") if player else ""
        verdict_emoji = verdict.split(" ")[0] if verdict else ""
        compat = _slot_compatibility(slot["role"], player.get("position")) if player else "mismatch"
        compat_border = _COMPAT_COLORS[compat]
        opacity = "1" if player else "0.6"
        photo = player.get("photo_url", "") if player else ""
        age = str(player.get("age", "")) if player else ""
        mv = player.get("market_value", "") if player else ""
        extra = verdict_emoji if verdict_emoji else ""

        if photo:
            img_html = (
                '<img src="{photo}" style="width:42px;height:42px;border-radius:50%;'
                'border:2px solid {bc};object-fit:cover;display:block;margin:0 auto;"/>'
            ).format(photo=photo, bc=compat_border)
        else:
            img_html = (
                '<div style="width:42px;height:42px;background:#555;border-radius:50%;'
                'border:2px solid {bc};display:flex;align-items:center;justify-content:center;'
                'font-size:11px;color:white;font-weight:bold;margin:0 auto;">?</div>'
            ).format(bc=compat_border)

        parts.append(
            '<div style="position:absolute;left:{x}%;top:{y}%;transform:translate(-50%,-50%);opacity:{op};">'
            '<div style="background:rgba(0,0,0,0.75);border-radius:8px;padding:3px 6px 4px;'
            'text-align:center;min-width:55px;box-shadow:0 1px 4px rgba(0,0,0,0.5);">'
            '<div style="font-size:9px;color:#aaa;font-weight:700;letter-spacing:0.5px;">{sn}</div>'
            '{img}'
            '<div style="font-size:10px;color:white;font-weight:700;margin-top:2px;white-space:nowrap;">{nm}</div>'
            '<div style="font-size:8px;color:#ccc;white-space:nowrap;">'
            '{age}{sep1}{ss} {ve}</div>'
            '<div style="font-size:8px;color:#999;">{mv}</div>'
            '</div></div>'.format(
                x=x, y=y, op=opacity, img=img_html,
                sn=slot["slot"], nm=surname,
                age=age, sep1=" · " if age and ss else "",
                ss=ss, ve=extra, mv=mv,
            )
        )

    parts.append('</div>')
    return "".join(parts)


def _get_user_formation_data():
    """Load user's saved formation choice + overrides from profile."""
    profile = storage.get_profile(_current_user_id())
    selected = profile.get("selected_formation", "")
    overrides_raw = profile.get("formation_overrides", "")
    overrides = {}
    if isinstance(overrides_raw, str) and overrides_raw:
        try:
            overrides = json.loads(overrides_raw)
        except Exception:
            pass
    elif isinstance(overrides_raw, dict):
        overrides = overrides_raw
    return selected, overrides


def _save_user_formation_data(formation_name, overrides):
    """Save user's formation choice + manual overrides."""
    storage.update_profile(_current_user_id(), {
        "selected_formation": formation_name,
        "formation_overrides": json.dumps(overrides),
    })


def _slot_change_callback(selected_formation):
    """Called when a slot selectbox changes — swap players and save."""
    saved_formation, saved_overrides = _get_user_formation_data()
    overrides = dict(saved_overrides.get(selected_formation, {}))

    # Collect all current selections from widgets
    # Keys like "tac_{formation}_{slot_idx}" store option_id (player_id string or None)
    changed = False
    for key in list(st.session_state.keys()):
        prefix = "tac_{}_".format(selected_formation)
        if key.startswith(prefix):
            try:
                slot_idx = int(key[len(prefix):])
            except ValueError:
                continue
            new_id = st.session_state[key]
            old_id = overrides.get(str(slot_idx))
            if new_id != old_id:
                overrides[str(slot_idx)] = new_id
                changed = True

    if changed:
        new_overrides = dict(saved_overrides)
        new_overrides[selected_formation] = overrides
        _save_user_formation_data(selected_formation, new_overrides)


def tactics_tab(players):
    formations = storage.get_formations()
    if not formations:
        st.info("No formations configured. Ask an admin to set them up.")
        return

    saved_formation, saved_overrides = _get_user_formation_data()

    # Formation selector
    names = [f["name"] for f in formations]
    default_idx = 0
    if saved_formation in names:
        default_idx = names.index(saved_formation)
    selected = st.radio(
        "Formation", names, index=default_idx,
        horizontal=True, label_visibility="collapsed", key="tac_formation_select",
    )

    # Save formation selection if changed
    if selected != saved_formation:
        _save_user_formation_data(selected, saved_overrides)
        # Clear stale slot widget state from previous formation
        for k in list(st.session_state.keys()):
            if k.startswith("tac_") and not k.startswith("tac_formation_select"):
                del st.session_state[k]
        st.rerun()

    formation = next(f for f in formations if f["name"] == selected)

    # Build final assignments: start with auto, then apply overrides
    overrides = saved_overrides.get(selected, {})
    player_by_id = {str(p.get("id")): p for p in players}

    # Start with auto assignments
    assignments = _auto_assign_formation(formation, players)

    # Apply saved overrides (move players to explicit slots)
    if overrides:
        # First, remove all override'd players from their auto spots
        override_ids = {str(v) for v in overrides.values() if v}
        for i in range(len(assignments)):
            p = assignments[i][1]
            if p and str(p.get("id")) in override_ids:
                assignments[i][1] = None
        # Now place each override in its slot
        for slot_idx_str, pid in overrides.items():
            try:
                slot_idx = int(slot_idx_str)
            except ValueError:
                continue
            if 0 <= slot_idx < len(assignments):
                if pid and str(pid) in player_by_id:
                    assignments[slot_idx][1] = player_by_id[str(pid)]
                else:
                    assignments[slot_idx][1] = None

    # Starting XI value
    xi_players = [p for _, p in assignments if p]
    xi_value = sum(_mv_num(p) for p in xi_players)
    xi_ss = [float(p.get("sofascore_rating") or 0) for p in xi_players if p.get("sofascore_rating")]
    avg_ss = sum(xi_ss) / len(xi_ss) if xi_ss else 0
    st.markdown("**Starting XI** — {} players · {} · Avg SofaScore {:.2f}".format(
        len(xi_players), _fmt_m(xi_value), avg_ss
    ))
    st.caption("🟢 Exact position · 🟡 Similar role · 🔴 Out of position")

    # Two columns: pitch on left, lineup editor on right
    assigned_ids = {str(p.get("id")) for _, p in assignments if p}
    bench = [p for p in players if str(p.get("id")) not in assigned_ids]

    col_pitch, col_edit = st.columns([2, 1])

    with col_pitch:
        pitch = _render_pitch(formation, assignments)
        st.markdown(
            pitch.replace("margin:0 auto;", "margin:0;"),
            unsafe_allow_html=True,
        )

    with col_edit:
        # Disable text input on selectboxes in this column (click-to-select only)
        st.markdown(
            """
            <style>
            div[class*="st-key-tac_"] input[role="combobox"] {
                pointer-events: none !important;
                caret-color: transparent !important;
            }
            div[class*="st-key-tac_"] [data-baseweb="select"] > div {
                cursor: pointer !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Build options sorted by position (GK → DEF → MID → ATT)
        sorted_players = sorted(
            players,
            key=lambda p: (
                POSITION_ORDER.get(p.get("position", ""), 999),
                p.get("name", "").lower(),
            ),
        )
        option_ids = [None] + [str(p.get("id")) for p in sorted_players]
        option_labels = ["— Empty —"] + [
            "{} {} · {}".format(
                SHORT_POS.get(p.get("position", ""), "?"),
                p.get("name", "?"),
                p.get("club", "?"),
            )
            for p in sorted_players
        ]
        id_to_label = dict(zip(option_ids, option_labels))

        # Iterate in pitch order (top to bottom = attack to GK)
        indexed = list(enumerate(assignments))
        indexed.sort(key=lambda x: x[1][0].get("y", 0))

        for i, (slot, player) in indexed:
            current_id = str(player.get("id")) if player else None
            widget_key = "tac_{}_{}".format(selected, i)

            # Seed widget state to match current assignment (before widget renders)
            # Streamlit uses session state value if key already exists
            st.session_state[widget_key] = current_id

            compat_icon = "⚪"
            if player:
                compat_icon = _COMPAT_ICONS[_slot_compatibility(slot["role"], player.get("position"))]

            st.selectbox(
                "{} **{}** {}".format(compat_icon, slot["slot"], slot["role"]),
                options=option_ids,
                format_func=lambda x, lbl=id_to_label: lbl.get(x, "— Empty —"),
                key=widget_key,
                on_change=_slot_change_callback,
                args=(selected,),
                label_visibility="visible",
                accept_new_options=False,
            )

    # Bench + legend
    if bench:
        bench_value = sum(_mv_num(p) for p in bench)
        st.caption("🪑 **Bench** ({}): {}".format(
            _fmt_m(bench_value),
            " · ".join("{} ({})".format(p.get("name", "?"), SHORT_POS.get(p.get("position", ""), "?")) for p in bench)
        ))



# --------------------------------------------------------------------------- #
# Squad Map tab                                                               #
# --------------------------------------------------------------------------- #

# Natural x/y positions on pitch for each role (can have multiple players)
_POSITION_COORDS = {
    "Goalkeeper":         {"y": 92, "x_base": 50},
    "Centre-Back":        {"y": 75, "x_base": 50},
    "Right-Back":         {"y": 70, "x_base": 85},
    "Left-Back":          {"y": 70, "x_base": 15},
    "Defensive Midfield": {"y": 58, "x_base": 50},
    "Central Midfield":   {"y": 48, "x_base": 50},
    "Attacking Midfield": {"y": 38, "x_base": 50},
    "Right Winger":       {"y": 25, "x_base": 82},
    "Left Winger":        {"y": 25, "x_base": 18},
    "Second Striker":     {"y": 18, "x_base": 50},
    "Centre-Forward":     {"y": 12, "x_base": 50},
}


def _spread_players(players_in_position, x_base, spread=14):
    """Given N players at the same position, spread them horizontally."""
    n = len(players_in_position)
    if n == 1:
        return [(x_base, players_in_position[0])]
    coords = []
    total_width = spread * (n - 1)
    start_x = x_base - total_width / 2
    for i, p in enumerate(players_in_position):
        x = start_x + i * spread
        x = max(5, min(95, x))
        coords.append((x, p))
    return coords


def _max_saved_teams():
    return int(_app_settings().get("max_saved_teams", 20))


@st.dialog("Save this team", width="large")
def save_team_dialog(players):
    st.caption("Freeze a snapshot of your current 22/22 squad.")
    name = st.text_input("Team name *", placeholder="e.g. January Champions", key="save_team_name")
    desc = st.text_area("Description (optional)", key="save_team_desc")

    # Preview stats
    total_value = sum(_mv_num(p) for p in players)
    ss_list = [float(p.get("sofascore_rating") or 0) for p in players if p.get("sofascore_rating")]
    avg_ss = sum(ss_list) / len(ss_list) if ss_list else 0
    st.caption("Value: {} · Avg SofaScore: {:.2f}".format(_fmt_m(total_value), avg_ss))

    c1, c2 = st.columns(2)
    if c1.button("💾 Save", use_container_width=True, disabled=not name.strip()):
        existing = storage.list_saved_teams(_current_user_id())
        if len(existing) >= _max_saved_teams():
            st.error("You've reached the max of {} saved teams. Delete one first.".format(_max_saved_teams()))
            return
        saved_form, _ = _get_user_formation_data()
        storage.save_team(
            _current_user_id(),
            name.strip(),
            desc.strip(),
            players,
            saved_form or "",
            total_value,
            round(avg_ss, 2),
        )
        st.success("Team saved!")
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Team details", width="large")
def saved_team_detail_dialog(team):
    name = team.get("name", "?")
    desc = team.get("description", "")
    created = team.get("created_at", "")
    if isinstance(created, str) and len(created) > 16:
        created = created[:16].replace("T", " ")
    snapshot = team.get("snapshot") or []
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            snapshot = []

    st.markdown("### 🏆 {}".format(name))
    if desc:
        st.caption(desc)
    st.caption("📅 {} · ⚔️ {}".format(created, team.get("formation") or "-"))

    # Summary stats (same style as Map tab)
    total_value = float(team.get("total_value_m") or 0)
    avg_ss_val = float(team.get("avg_sofascore") or 0)
    age_list = [p["age"] for p in snapshot if p.get("age")]
    avg_age = sum(age_list) / len(age_list) if age_list else 0
    st.markdown("**{}** players · {} · Avg SofaScore {:.2f} · Avg Age {:.1f}".format(
        len(snapshot), _fmt_m(total_value), avg_ss_val, avg_age
    ))

    _DEPT_MAP_TEAMS = {
        "Goalkeeper": "GK",
        "Centre-Back": "DEF", "Right-Back": "DEF", "Left-Back": "DEF",
        "Defensive Midfield": "MID", "Central Midfield": "MID", "Attacking Midfield": "MID",
        "Right Winger": "ATT", "Left Winger": "ATT", "Second Striker": "ATT", "Centre-Forward": "ATT",
    }
    dept_values = {"GK": 0.0, "DEF": 0.0, "MID": 0.0, "ATT": 0.0}
    dept_counts = {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0}
    dept_ages = {"GK": [], "DEF": [], "MID": [], "ATT": []}
    for p in snapshot:
        d = _DEPT_MAP_TEAMS.get(p.get("position", ""), "")
        if d:
            dept_values[d] += _mv_num(p)
            dept_counts[d] += 1
            if p.get("age"):
                dept_ages[d].append(p["age"])

    def _dept_str(icon, label, dept):
        aa = sum(dept_ages[dept]) / len(dept_ages[dept]) if dept_ages[dept] else 0
        age_str = " ~{:.0f}y".format(aa) if aa else ""
        return "{} {}: {} ({}){}".format(icon, label, _fmt_m(dept_values[dept]), dept_counts[dept], age_str)

    st.markdown("{} · {} · {} · {}".format(
        _dept_str("🧤", "GK", "GK"),
        _dept_str("🛡️", "DEF", "DEF"),
        _dept_str("⚙️", "MID", "MID"),
        _dept_str("⚔️", "ATT", "ATT"),
    ))

    st.divider()

    # Render pitch using the same layout as squad map
    by_position = {}
    for p in snapshot:
        pos = p.get("position", "")
        if pos in _POSITION_COORDS:
            by_position.setdefault(pos, []).append(p)

    all_placed = []
    for pos, group in by_position.items():
        coords = _POSITION_COORDS[pos]
        spread = _spread_players(group, coords["x_base"])
        for x, p in spread:
            all_placed.append((x, coords["y"], p))

    parts = []
    parts.append(
        '<div style="position:relative;width:100%;max-width:700px;'
        'aspect-ratio:68/84;'
        'background:repeating-linear-gradient(to bottom,'
        '#1a6b35 0%,#1a6b35 8.33%,#1f7a3d 8.33%,#1f7a3d 16.66%);'
        'border:3px solid rgba(255,255,255,0.6);border-radius:8px;overflow:hidden;'
        'box-shadow:inset 0 0 40px rgba(0,0,0,0.3),0 4px 12px rgba(0,0,0,0.3);">'
    )
    parts.append('<div style="position:absolute;top:50%;left:5%;right:5%;height:2px;background:rgba(255,255,255,0.7);"></div>')
    parts.append('<div style="position:absolute;top:50%;left:50%;width:70px;height:70px;border:2px solid rgba(255,255,255,0.7);border-radius:50%;transform:translate(-50%,-50%);"></div>')
    parts.append('<div style="position:absolute;top:50%;left:50%;width:6px;height:6px;background:rgba(255,255,255,0.7);border-radius:50%;transform:translate(-50%,-50%);"></div>')
    parts.append('<div style="position:absolute;top:0;left:22%;right:22%;height:14%;border:2px solid rgba(255,255,255,0.7);border-top:none;"></div>')
    parts.append('<div style="position:absolute;top:0;left:34%;right:34%;height:7%;border:2px solid rgba(255,255,255,0.7);border-top:none;"></div>')
    parts.append('<div style="position:absolute;bottom:0;left:22%;right:22%;height:14%;border:2px solid rgba(255,255,255,0.7);border-bottom:none;"></div>')
    parts.append('<div style="position:absolute;bottom:0;left:34%;right:34%;height:7%;border:2px solid rgba(255,255,255,0.7);border-bottom:none;"></div>')

    for x, y, p in all_placed:
        surname = p.get("name", "?").split()[-1]
        ss_r = str(p.get("sofascore_rating", "")) if p.get("sofascore_rating") else ""
        photo = p.get("photo_url", "")
        mv_str = p.get("market_value", "")
        age_str = str(p.get("age", "")) if p.get("age") else ""
        role = SHORT_POS.get(p.get("position", ""), ROLE_PREFIX.get(p.get("position", ""), ""))

        if photo:
            img_html = (
                '<img src="{photo}" style="width:42px;height:42px;border-radius:50%;'
                'border:2px solid white;object-fit:cover;display:block;margin:0 auto;"/>'
            ).format(photo=photo)
        else:
            img_html = (
                '<div style="width:42px;height:42px;background:#555;border-radius:50%;'
                'border:2px solid white;display:flex;align-items:center;justify-content:center;'
                'font-size:11px;color:white;font-weight:bold;margin:0 auto;">?</div>'
            )

        parts.append(
            '<div style="position:absolute;left:{x}%;top:{y}%;transform:translate(-50%,-50%);">'
            '<div style="background:rgba(0,0,0,0.75);border-radius:8px;padding:3px 6px 4px;'
            'text-align:center;min-width:55px;box-shadow:0 1px 4px rgba(0,0,0,0.5);">'
            '<div style="font-size:9px;color:#aaa;font-weight:700;letter-spacing:0.5px;">{role}</div>'
            '{img}'
            '<div style="font-size:10px;color:white;font-weight:700;margin-top:2px;white-space:nowrap;">{nm}</div>'
            '<div style="font-size:8px;color:#ccc;white-space:nowrap;">'
            '{age}{sep}{ss}</div>'
            '<div style="font-size:8px;color:#999;">{mv}</div>'
            '</div></div>'.format(
                x=x, y=y, img=img_html, role=role,
                nm=surname, age=age_str, sep=" · " if age_str and ss_r else "",
                ss=ss_r, mv=mv_str,
            )
        )
    parts.append('</div>')
    st.markdown("".join(parts), unsafe_allow_html=True)

    st.divider()
    b1, b2, b3 = st.columns(3)
    if b1.button("✏️ Rename", use_container_width=True, key="rename_team_btn"):
        st.session_state["rename_team_id"] = team.get("id")
        st.session_state["rename_team_name"] = name
        st.session_state["rename_team_desc"] = desc
        st.rerun()
    if b2.button("🗑️ Delete", use_container_width=True, type="secondary", key="delete_team_btn"):
        storage.delete_saved_team(_current_user_id(), team.get("id"))
        st.success("Deleted!")
        st.rerun()
    if b3.button("Close", use_container_width=True, key="close_team_btn"):
        st.rerun()


@st.dialog("Rename team")
def rename_team_dialog():
    team_id = st.session_state.get("rename_team_id")
    new_name = st.text_input(
        "Name", value=st.session_state.get("rename_team_name", ""), key="rt_name"
    )
    new_desc = st.text_area(
        "Description", value=st.session_state.get("rename_team_desc", ""), key="rt_desc"
    )
    c1, c2 = st.columns(2)
    if c1.button("💾 Save", use_container_width=True, disabled=not new_name.strip()):
        storage.rename_saved_team(
            _current_user_id(), team_id, new_name.strip(), new_desc.strip()
        )
        for k in ("rename_team_id", "rename_team_name", "rename_team_desc"):
            st.session_state.pop(k, None)
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        for k in ("rename_team_id", "rename_team_name", "rename_team_desc"):
            st.session_state.pop(k, None)
        st.rerun()


def saved_teams_tab(players):
    uid = _current_user_id()
    teams = storage.list_saved_teams(uid)
    max_size = _max_squad()
    full = len(players) >= max_size

    # Header with save button
    h1, h2 = st.columns([4, 1])
    h1.markdown("**{} saved team{}** (max {})".format(
        len(teams), "s" if len(teams) != 1 else "", _max_saved_teams()
    ))
    if full:
        if h2.button("💾 Save current", use_container_width=True, key="save_cur_team"):
            st.session_state["show_save_team_dialog"] = True
            st.rerun()
    else:
        h2.caption("Save unlocks at {}/{}".format(len(players), max_size))

    if not teams:
        st.info("No saved teams yet. Fill your squad to {}/{} and hit 💾 Save.".format(max_size, max_size))
        return

    st.divider()

    # Row per team: name/date/value + 5 top player photos + view button
    for t in teams:
        snapshot = t.get("snapshot") or []
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except Exception:
                snapshot = []
        created = t.get("created_at", "")
        if isinstance(created, str) and len(created) > 10:
            created = created[:10]

        # Top 5 players by market value
        top5 = sorted(snapshot, key=lambda p: _mv_num(p), reverse=True)[:5]

        tid = t.get("id")
        png_key = "team_png_{}".format(tid)

        with st.container(border=True):
            c_info, c_photos, c_png, c_btn = st.columns([3, 4, 0.8, 0.8])
            with c_info:
                st.markdown("**🏆 {}**".format(t.get("name", "?")))
                st.caption("📅 {} · ⚔️ {} · 👥 {}".format(
                    created, t.get("formation") or "-", len(snapshot)
                ))
                st.caption("💰 {} · 📈 {:.2f}".format(
                    _fmt_m(float(t.get("total_value_m") or 0)),
                    float(t.get("avg_sofascore") or 0),
                ))
            with c_photos:
                if top5:
                    pcols = st.columns(5)
                    for j, p in enumerate(top5):
                        with pcols[j]:
                            if p.get("photo_url"):
                                st.image(p["photo_url"], width=44)
                            surname = p.get("name", "?").split()[-1]
                            st.caption("**{}**".format(surname))
                            st.caption(p.get("market_value", ""))
            with c_png:
                st.write("")
                if st.session_state.get(png_key):
                    st.download_button(
                        "💾",
                        data=st.session_state[png_key],
                        file_name="{}.png".format(t.get("name", "team").replace(" ", "_")),
                        mime="image/png",
                        use_container_width=True,
                        key="dl_{}".format(tid),
                        help="Download PNG",
                    )
                else:
                    if st.button("🖼️", key="gen_{}".format(tid),
                                 use_container_width=True, help="Generate PNG"):
                        with st.spinner("Generating..."):
                            try:
                                from pitch_image import render_squad_map_image
                                stats = "{} players · {} · Avg SofaScore {:.2f}".format(
                                    len(snapshot),
                                    _fmt_m(float(t.get("total_value_m") or 0)),
                                    float(t.get("avg_sofascore") or 0),
                                )
                                subtitle = "📅 {} · ⚔️ {}".format(
                                    created, t.get("formation") or "-"
                                )
                                img = render_squad_map_image(
                                    t.get("name", "Team"),
                                    snapshot,
                                    stats_line=stats,
                                    subtitle=subtitle,
                                )
                                buf = io.BytesIO()
                                img.save(buf, format="PNG", optimize=True)
                                st.session_state[png_key] = buf.getvalue()
                                st.rerun()
                            except Exception as e:
                                st.error("Failed: {}".format(e))
            with c_btn:
                st.write("")
                if st.button("👀", key="view_team_{}".format(tid),
                             use_container_width=True, help="View details"):
                    st.session_state["view_team_id"] = tid
                    st.rerun()


def squad_map_tab(players):
    if not players:
        st.info("No players yet. Buy some to see your squad map.")
        return

    total_value = sum(_mv_num(p) for p in players)
    ss_list = [float(p.get("sofascore_rating") or 0) for p in players if p.get("sofascore_rating")]
    avg_ss = sum(ss_list) / len(ss_list) if ss_list else 0
    st.markdown("**Squad Map** — {} players · {} · Avg SofaScore {:.2f}".format(
        len(players), _fmt_m(total_value), avg_ss
    ))

    # Department values
    _DEPT_MAP = {
        "Goalkeeper": "GK",
        "Centre-Back": "DEF", "Right-Back": "DEF", "Left-Back": "DEF",
        "Defensive Midfield": "MID", "Central Midfield": "MID", "Attacking Midfield": "MID",
        "Right Winger": "ATT", "Left Winger": "ATT", "Second Striker": "ATT", "Centre-Forward": "ATT",
    }
    dept_values = {"GK": 0.0, "DEF": 0.0, "MID": 0.0, "ATT": 0.0}
    dept_counts = {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0}
    dept_ages = {"GK": [], "DEF": [], "MID": [], "ATT": []}
    for p in players:
        dept = _DEPT_MAP.get(p.get("position", ""), "")
        if dept:
            dept_values[dept] += _mv_num(p)
            dept_counts[dept] += 1
            if p.get("age"):
                dept_ages[dept].append(p["age"])

    def _dept_str(icon, name, dept):
        avg_age = sum(dept_ages[dept]) / len(dept_ages[dept]) if dept_ages[dept] else 0
        age_str = " ~{:.0f}y".format(avg_age) if avg_age else ""
        return "{} {}: {} ({}){}" .format(icon, name, _fmt_m(dept_values[dept]), dept_counts[dept], age_str)

    dept_line = "{} · {} · {} · {}".format(
        _dept_str("🧤", "GK", "GK"),
        _dept_str("🛡️", "DEF", "DEF"),
        _dept_str("⚙️", "MID", "MID"),
        _dept_str("⚔️", "ATT", "ATT"),
    )
    st.markdown(dept_line)

    # Generate & auto-download image button — constrained to pitch width
    profile = storage.get_profile(_current_user_id())
    team_name = profile.get("team_name") or "My Football Stars"
    cached_png = st.session_state.get("squad_map_png")

    # Constrain the button width to match the pitch (max-width 700px, left-aligned)
    btn_col, _btn_spacer = st.columns([2, 1])
    with btn_col:
        if cached_png:
            st.download_button(
                "💾 Download image",
                data=cached_png,
                file_name="{}_squad_map.png".format(team_name.replace(" ", "_")),
                mime="image/png",
                use_container_width=True,
                key="dl_squad_png_btn",
            )
        else:
            if st.button("🖼️ Generate & download image", use_container_width=True, key="gen_squad_img"):
                with st.spinner("Generating image..."):
                    try:
                        from pitch_image import render_squad_map_image
                        stats = "{} players · {} · Avg SofaScore {:.2f}".format(
                            len(players), _fmt_m(total_value), avg_ss
                        )
                        plain_dept = dept_line.replace("🧤 ", "").replace("🛡️ ", "").replace("⚙️ ", "").replace("⚔️ ", "")
                        img = render_squad_map_image(
                            team_name, players,
                            stats_line=plain_dept,
                            subtitle=stats,
                        )
                        buf = io.BytesIO()
                        img.save(buf, format="PNG", optimize=True)
                        st.session_state["squad_map_png"] = buf.getvalue()
                        st.session_state["squad_map_png_autotrigger"] = True
                        st.rerun()
                    except Exception as e:
                        st.error("Image generation failed: {}".format(e))

    # Auto-click the download button once after generation
    if cached_png and st.session_state.pop("squad_map_png_autotrigger", False):
        import streamlit.components.v1 as _components
        _components.html(
            """
            <script>
            setTimeout(function() {
                const btns = window.parent.document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.innerText && b.innerText.includes('Download image')) {
                        b.click();
                        break;
                    }
                }
            }, 200);
            </script>
            """,
            height=0,
        )

    # Group players by position
    by_position = {}
    unknown = []
    for p in players:
        pos = p.get("position", "")
        if pos in _POSITION_COORDS:
            by_position.setdefault(pos, []).append(p)
        else:
            unknown.append(p)

    # Build all (x, y, player) tuples
    all_placed = []
    for pos, group in by_position.items():
        coords = _POSITION_COORDS[pos]
        spread = _spread_players(group, coords["x_base"])
        for x, p in spread:
            all_placed.append((x, coords["y"], p))

    # Render pitch (reuse same style as tactics)
    parts = []
    parts.append(
        '<div style="position:relative;width:100%;max-width:700px;'
        'aspect-ratio:68/84;'
        'background:repeating-linear-gradient(to bottom,'
        '#1a6b35 0%,#1a6b35 8.33%,#1f7a3d 8.33%,#1f7a3d 16.66%);'
        'border:3px solid rgba(255,255,255,0.6);border-radius:8px;overflow:hidden;'
        'box-shadow:inset 0 0 40px rgba(0,0,0,0.3),0 4px 12px rgba(0,0,0,0.3);">'
    )
    # Pitch markings
    parts.append('<div style="position:absolute;top:50%;left:5%;right:5%;height:2px;background:rgba(255,255,255,0.7);"></div>')
    parts.append('<div style="position:absolute;top:50%;left:50%;width:70px;height:70px;border:2px solid rgba(255,255,255,0.7);border-radius:50%;transform:translate(-50%,-50%);"></div>')
    parts.append('<div style="position:absolute;top:50%;left:50%;width:6px;height:6px;background:rgba(255,255,255,0.7);border-radius:50%;transform:translate(-50%,-50%);"></div>')
    parts.append('<div style="position:absolute;top:0;left:22%;right:22%;height:14%;border:2px solid rgba(255,255,255,0.7);border-top:none;"></div>')
    parts.append('<div style="position:absolute;top:0;left:34%;right:34%;height:7%;border:2px solid rgba(255,255,255,0.7);border-top:none;"></div>')
    parts.append('<div style="position:absolute;bottom:0;left:22%;right:22%;height:14%;border:2px solid rgba(255,255,255,0.7);border-bottom:none;"></div>')
    parts.append('<div style="position:absolute;bottom:0;left:34%;right:34%;height:7%;border:2px solid rgba(255,255,255,0.7);border-bottom:none;"></div>')

    for x, y, p in all_placed:
        surname = p.get("name", "?").split()[-1]
        ss = str(p.get("sofascore_rating", "")) if p.get("sofascore_rating") else ""
        verdict = p.get("verdict", "")
        verdict_emoji = verdict.split(" ")[0] if verdict else ""
        photo = p.get("photo_url", "")
        mv = p.get("market_value", "")
        age = str(p.get("age", "")) if p.get("age") else ""
        role = SHORT_POS.get(p.get("position", ""), ROLE_PREFIX.get(p.get("position", ""), ""))
        extra = verdict_emoji if verdict_emoji else ""

        if photo:
            img_html = (
                '<img src="{photo}" style="width:42px;height:42px;border-radius:50%;'
                'border:2px solid white;object-fit:cover;display:block;margin:0 auto;"/>'
            ).format(photo=photo)
        else:
            img_html = (
                '<div style="width:42px;height:42px;background:#555;border-radius:50%;'
                'border:2px solid white;display:flex;align-items:center;justify-content:center;'
                'font-size:11px;color:white;font-weight:bold;margin:0 auto;">?</div>'
            )

        parts.append(
            '<div style="position:absolute;left:{x}%;top:{y}%;transform:translate(-50%,-50%);">'
            '<div style="background:rgba(0,0,0,0.75);border-radius:8px;padding:3px 6px 4px;'
            'text-align:center;min-width:55px;box-shadow:0 1px 4px rgba(0,0,0,0.5);">'
            '<div style="font-size:9px;color:#aaa;font-weight:700;letter-spacing:0.5px;">{role}</div>'
            '{img}'
            '<div style="font-size:10px;color:white;font-weight:700;margin-top:2px;white-space:nowrap;">{nm}</div>'
            '<div style="font-size:8px;color:#ccc;white-space:nowrap;">'
            '{age}{sep}{ss} {ve}</div>'
            '<div style="font-size:8px;color:#999;">{mv}</div>'
            '</div></div>'.format(
                x=x, y=y, img=img_html, role=role,
                nm=surname, age=age, sep=" · " if age and ss else "",
                ss=ss, ve=extra, mv=mv,
            )
        )

    parts.append('</div>')
    st.markdown("".join(parts), unsafe_allow_html=True)

    if unknown:
        st.caption("⚠️ Unknown position: {}".format(
            ", ".join("{} ({})".format(p.get("name", "?"), p.get("position", "?")) for p in unknown)
        ))


# --------------------------------------------------------------------------- #
# Ask ChatGPT tab                                                             #
# --------------------------------------------------------------------------- #

def chatgpt_tab(players):
    if not players:
        st.info("Add some players first.")
        return

    import urllib.parse

    st.caption("Ask ChatGPT about your squad — opens in a new tab with your team data pre-filled.")

    budget_info = storage.compute_budget(_current_user_id())

    profile = storage.get_profile(_current_user_id())
    user_name = profile.get("first_name") or profile.get("nickname") or "the user"
    lang = _get_analysis_lang()
    lang_note = " Reply in Italian." if lang == "Italiano" else ""

    # Compact squad for URL (keep under 2000 chars)
    lines = []
    for p in players:
        pos = SHORT_POS.get(p.get("position", ""), "?")
        name = p.get("name", "?")
        age = p.get("age", "?")
        mv = p.get("market_value", "?")
        ss = p.get("sofascore_rating", "")
        lines.append("{} {} {}y {} ss{}".format(pos, name, age, mv, ss))
    squad_compact = "; ".join(lines)
    cash = _fmt_m(budget_info["cash"])

    context = (
        "Football scout help.{lang} "
        "I have {cash} cash, {n}/22 players: {squad}. "
    ).format(lang=lang_note, cash=cash, n=len(players), squad=squad_compact)

    # Handle preset click → store in session and rerun
    presets = [
        "Analyze my squad strengths and weaknesses",
        "Suggest 3 players I should buy next",
        "Which players should I sell and why?",
        "Rate my squad out of 10 with explanation",
        "Suggest the best formation for my squad",
    ]

    # If a preset was clicked last run, pre-fill the input
    if st.session_state.get("gpt_preset_selected"):
        st.session_state["gpt_question"] = st.session_state.pop("gpt_preset_selected")

    user_q = st.text_input(
        "Your question",
        placeholder="e.g. Who should I buy next? How can I improve my midfield?",
        key="gpt_question",
    )

    if user_q:
        # Question is set — show the link, hide presets
        full_prompt = context + "\n\n" + user_q
        encoded = urllib.parse.quote(full_prompt, safe="")
        url = "https://chatgpt.com/?q={}".format(encoded)

        if len(url) > 4000:
            short_squad = "; ".join(lines[:15]) + "..."
            short_context = (
                "Football scout help.{lang} "
                "I have {cash} cash, {n}/22 players: {squad}. "
            ).format(lang=lang_note, cash=cash, n=len(players), squad=short_squad)
            full_prompt = short_context + "\n\n" + user_q
            encoded = urllib.parse.quote(full_prompt, safe="")
            url = "https://chatgpt.com/?q={}".format(encoded)

        st.link_button(
            "🚀 Open in ChatGPT",
            url,
            use_container_width=True,
        )
        st.caption("Opens ChatGPT with your squad data and question pre-filled.")
    else:
        # No question yet — show presets
        st.caption("Pick a question or type your own:")
        for i, preset in enumerate(presets):
            if st.button(preset, key="gpt_preset_{}".format(i), use_container_width=True):
                st.session_state["gpt_preset_selected"] = preset
                st.rerun()


# --------------------------------------------------------------------------- #
# Notes tab                                                                   #
# --------------------------------------------------------------------------- #

def notes_tab():
    uid = _current_user_id()

    # Add new note
    with st.form("new_note", clear_on_submit=True):
        title = st.text_input("Title")
        content = st.text_area("Note", height=100)
        if st.form_submit_button("➕ Add note", use_container_width=True):
            if title.strip() or content.strip():
                storage.add_note(uid, {
                    "title": title.strip(),
                    "content": content.strip(),
                })
                st.rerun()

    st.divider()

    # List notes
    notes = storage.list_notes(uid)
    if not notes:
        st.caption("No notes yet.")
        return

    for n in notes:
        nid = n.get("id")
        ts = n.get("updated_at", "")
        if isinstance(ts, str) and len(ts) > 16:
            ts = ts[:16].replace("T", " ")

        with st.container(border=True):
            # Header: title + date + actions
            h1, h2, h3 = st.columns([5, 1, 1])
            h1.markdown("**{}**".format(n.get("title", "Untitled")))
            h2.caption(ts)
            editing_key = "editing_note_{}".format(nid)

            if not st.session_state.get(editing_key):
                # View mode
                st.markdown(n.get("content", ""))
                c1, c2, _ = st.columns([1, 1, 6])
                if c1.button("✏️", key="edit_n_{}".format(nid)):
                    st.session_state[editing_key] = True
                    st.rerun()
                if c2.button("🗑️", key="del_n_{}".format(nid)):
                    storage.delete_note(uid, nid)
                    st.rerun()
            else:
                # Edit mode
                new_title = st.text_input(
                    "Title", value=n.get("title", ""), key="et_{}".format(nid)
                )
                new_content = st.text_area(
                    "Note", value=n.get("content", ""), key="ec_{}".format(nid), height=100
                )
                c1, c2, _ = st.columns([1, 1, 6])
                if c1.button("💾", key="save_n_{}".format(nid)):
                    storage.update_note(uid, nid, {
                        "title": new_title.strip(),
                        "content": new_content.strip(),
                    })
                    st.session_state.pop(editing_key, None)
                    st.rerun()
                if c2.button("✖️", key="cancel_n_{}".format(nid)):
                    st.session_state.pop(editing_key, None)
                    st.rerun()


# --------------------------------------------------------------------------- #
# Profile & Settings dialogs                                                  #
# --------------------------------------------------------------------------- #

@st.dialog("Edit Profile")
def profile_dialog(profile):
    pf_team = st.text_input("Team name", value=profile.get("team_name", "My Football Stars"))
    pf_fn = st.text_input("First name", value=profile.get("first_name", ""))
    pf_ln = st.text_input("Last name", value=profile.get("last_name", ""))
    pf_nick = st.text_input("Nickname", value=profile.get("nickname", ""))
    pf_yob = st.number_input(
        "Year of birth", value=int(profile.get("year_of_birth") or 2015),
        min_value=1950, max_value=2025, step=1,
    )
    c1, c2 = st.columns(2)
    if c1.button("💾 Save", use_container_width=True):
        storage.update_profile(_current_user_id(), {
            "team_name": pf_team.strip(),
            "first_name": pf_fn.strip(),
            "last_name": pf_ln.strip(),
            "nickname": pf_nick.strip(),
            "year_of_birth": int(pf_yob),
        })
        st.success("Profile saved!")
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("App Settings", width="large")
def settings_dialog():
    app_s = _app_settings()
    new_budget = st.number_input(
        "Initial budget (€M)", value=float(app_s.get("budget_m", 1000)),
        min_value=0.0, step=100.0,
    )
    c1, c2 = st.columns(2)
    new_max = c1.number_input(
        "Max squad size", value=int(app_s.get("max_squad_size", 22)),
        min_value=1, max_value=50,
    )
    new_min_analysis = c2.number_input(
        "Min players for analysis", value=int(app_s.get("min_players_for_analysis", 11)),
        min_value=1, max_value=50,
    )
    new_max_saved = st.number_input(
        "Max saved teams per user",
        value=int(app_s.get("max_saved_teams", 20)),
        min_value=1, max_value=200,
    )
    current_prompt = app_s.get("analysis_prompt", "") or DEFAULT_ANALYSIS_PROMPT
    new_prompt = st.text_area(
        "Analysis prompt (placeholders: {user_name}, {user_age}, {max_squad}, {lang})",
        value=current_prompt,
        height=300,
    )

    st.divider()
    st.markdown("**Formations**")
    current_formations = storage.get_formations()
    formations_json = json.dumps(current_formations, indent=2)
    new_formations_json = st.text_area(
        "Formations JSON (advanced — edit carefully)",
        value=formations_json,
        height=200,
    )

    # TM cache stats
    st.divider()
    cache = storage.tm_cache_stats()
    cs1, cs2 = st.columns([4, 1])
    cs1.caption("📦 TM cache: **{}** entries · **{} KB** · oldest **{}d** ago".format(
        cache["count"], cache["size_kb"], cache["oldest_days"]
    ))
    if cs2.button("🗑️ Clear cache", use_container_width=True, key="clear_tm_cache"):
        storage.tm_cache_invalidate()
        st.success("Cache cleared!")
        st.rerun()

    bc1, bc2 = st.columns(2)
    if bc1.button("💾 Save settings", use_container_width=True):
        # Validate formations JSON
        try:
            parsed_formations = json.loads(new_formations_json)
            assert isinstance(parsed_formations, list)
            for f in parsed_formations:
                assert "name" in f and "positions" in f
        except Exception:
            st.error("Invalid formations JSON. Each formation needs 'name' and 'positions'.")
            st.stop()

        storage.update_app_settings({
            "budget_m": new_budget,
            "max_squad_size": int(new_max),
            "min_players_for_analysis": int(new_min_analysis),
            "max_saved_teams": int(new_max_saved),
            "analysis_prompt": new_prompt.strip(),
            "formations": json.dumps(parsed_formations),
        })
        st.session_state.pop("app_settings", None)
        st.success("Settings saved!")
        st.rerun()
    if bc2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Change Admin PIN")
def change_pin_dialog():
    admin_uid = st.session_state.get("real_user_id") or _current_user_id()
    profile = storage.get_profile(admin_uid)
    current_pin = profile.get("admin_pin", "")

    if current_pin:
        st.caption("Enter your current PIN, then set a new one.")
        old_pin = st.text_input("Current PIN", type="password", max_chars=4, key="old_pin")
    else:
        st.caption("No PIN set yet. Set a new 4-digit PIN.")
        old_pin = ""

    new_pin = st.text_input("New PIN (4 digits)", type="password", max_chars=4, key="new_pin")

    c1, c2 = st.columns(2)
    valid_new = new_pin and new_pin.isdigit() and len(new_pin) == 4
    can_save = valid_new and (not current_pin or old_pin == current_pin)
    if c1.button("💾 Save", use_container_width=True, disabled=not can_save):
        if current_pin and old_pin != current_pin:
            st.error("Current PIN is wrong")
            return
        storage.update_profile(admin_uid, {"admin_pin": new_pin})
        st.success("PIN updated!")
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Set Admin PIN (once)")
def impersonate_pin_setup_dialog():
    pending = st.session_state.get("pending_impersonate", {})
    if not pending:
        return
    st.markdown("About to impersonate **{}**".format(pending.get("name", "?")))
    st.caption("Set a 4-digit admin PIN. This is saved to your profile — you only set it once.")
    pin = st.text_input("PIN (4 digits)", type="password", max_chars=4, key="imp_pin_set")
    c1, c2 = st.columns(2)
    if c1.button("✅ Save & start impersonating", use_container_width=True, disabled=not (pin and pin.isdigit() and len(pin) == 4)):
        # Persist PIN in admin profile
        admin_uid = _current_user_id()
        storage.update_profile(admin_uid, {"admin_pin": pin})
        # Save real identity + PIN in session
        st.session_state["real_user_id"] = admin_uid
        st.session_state["real_user_email"] = st.session_state["user"].get("email", "")
        st.session_state["impersonate_pin"] = pin
        # Swap user
        st.session_state["user"] = {
            "id": pending["uid"],
            "email": pending["email"],
            "display_name": pending["name"],
        }
        for k in ("players", "app_settings", "owned_map"):
            st.session_state.pop(k, None)
        st.session_state.pop("pending_impersonate", None)
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.session_state.pop("pending_impersonate", None)
        st.rerun()


@st.dialog("Exit PIN required")
def impersonate_pin_exit_dialog():
    st.markdown("Enter the 4-digit PIN to exit impersonation.")
    st.caption("Forgot the PIN? Close this browser tab to reset.")
    pin = st.text_input("PIN", type="password", max_chars=4, key="imp_pin_exit")
    c1, c2 = st.columns(2)
    if c1.button("✅ Exit", use_container_width=True, disabled=not pin):
        if pin == st.session_state.get("impersonate_pin"):
            # Revert
            real_uid = st.session_state.get("real_user_id")
            real_email = st.session_state.get("real_user_email", "")
            real_profile = storage.get_profile(real_uid)
            st.session_state["user"] = {
                "id": real_uid,
                "email": real_email,
                "display_name": _display_name(
                    {"email": real_email}, real_profile
                ),
            }
            for k in ("real_user_id", "real_user_email", "impersonate_pin",
                      "players", "app_settings", "owned_map",
                      "exit_impersonate_request"):
                st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error("Wrong PIN")
    if c2.button("Cancel", use_container_width=True):
        st.session_state.pop("exit_impersonate_request", None)
        st.rerun()


# --------------------------------------------------------------------------- #
# Guided tour                                                                 #
# --------------------------------------------------------------------------- #

_TOUR_STEPS = [
    {
        "title": "Welcome! 👋",
        "text": (
            "This is your **Fantasy Football Manager**!\n\n"
            "You have **€1 billion** to build a squad of **22 real players**.\n\n"
            "Buy players at their real market value, set tactics, "
            "and compete with friends. Let's get started!"
        ),
        "icon": "⚽",
    },
    {
        "title": "🔎 Search & Buy",
        "text": (
            "Use the **search bar at the top** to find players on Transfermarkt.\n\n"
            "Type at least 3 letters — results appear automatically.\n\n"
            "Click a player name to buy them at market value."
        ),
        "icon": "🔎",
    },
    {
        "title": "⚽ Your Squad",
        "text": (
            "The **Squad tab** shows all your players in a sortable table.\n\n"
            "Click any player name to see their **full details**, "
            "sell them, or refresh their data."
        ),
        "icon": "⚽",
    },
    {
        "title": "🗺️ Squad Map",
        "text": (
            "The **Map tab** shows all your players on a football pitch, "
            "positioned by their natural role.\n\n"
            "You can also **generate a PNG image** of your squad to share!"
        ),
        "icon": "🗺️",
    },
    {
        "title": "⚔️ Tactics",
        "text": (
            "Choose from **5 formations** (4-3-3, 4-2-3-1, etc.) and "
            "assign players to each slot.\n\n"
            "Green = exact match, Yellow = similar role, Red = out of position."
        ),
        "icon": "⚔️",
    },
    {
        "title": "📊 Budget & Transactions",
        "text": (
            "Your budget updates with every buy and sell.\n\n"
            "The **Transactions tab** tracks every deal. "
            "Watch your **portfolio value** and **gain/loss** over time."
        ),
        "icon": "📊",
    },
    {
        "title": "📋 AI Analysis",
        "text": (
            "Once you have **11+ players**, the **Analysis tab** gives you "
            "a full squad report powered by Claude AI.\n\n"
            "Each player gets a **verdict**: 🔒 Lock Him In → 🚨 Sell ASAP.\n\n"
            "Use **Ask Claude** to chat about your squad."
        ),
        "icon": "🤖",
    },
    {
        "title": "🏆 Save Your Team",
        "text": (
            "When your squad hits **22/22**, you can **save a snapshot**.\n\n"
            "Saved teams appear in the **Teams tab** with a pitch view "
            "and downloadable PNG image."
        ),
        "icon": "🏆",
    },
    {
        "title": "You're ready! 🚀",
        "text": (
            "Start by **searching for a player** you love.\n\n"
            "Build your dream squad, set your tactics, "
            "and see how your team compares!\n\n"
            "You can replay this tour anytime from the **❓** in the header."
        ),
        "icon": "🎉",
    },
]


def _show_tour():
    """Render the current tour step."""
    step = st.session_state.get("tour_step", 0)
    if step < 0 or step >= len(_TOUR_STEPS):
        st.session_state.pop("tour_active", None)
        st.session_state.pop("tour_step", None)
        return

    s = _TOUR_STEPS[step]
    with st.container(border=True):
        st.markdown("### {} {}".format(s["icon"], s["title"]))
        st.markdown(s["text"])
        st.caption("Step {} of {}".format(step + 1, len(_TOUR_STEPS)))

        c1, c2, c3 = st.columns([1, 1, 1])
        if step > 0:
            if c1.button("◀ Back", use_container_width=True, key="tour_back"):
                st.session_state["tour_step"] = step - 1
                st.rerun()

        if step < len(_TOUR_STEPS) - 1:
            if c2.button("Next ▶", use_container_width=True, key="tour_next"):
                st.session_state["tour_step"] = step + 1
                st.rerun()
        else:
            if c2.button("✅ Got it!", use_container_width=True, key="tour_done"):
                st.session_state.pop("tour_active", None)
                st.session_state.pop("tour_step", None)
                # Mark as seen in profile
                try:
                    storage.update_profile(_current_user_id(), {"tour_seen": "1"})
                except Exception:
                    pass
                st.rerun()

        if c3.button("Skip tour", use_container_width=True, key="tour_skip"):
            st.session_state.pop("tour_active", None)
            st.session_state.pop("tour_step", None)
            try:
                storage.update_profile(_current_user_id(), {"tour_seen": "1"})
            except Exception:
                pass
            st.rerun()


def global_stats_tab():
    stats = storage.global_stats()
    if not stats or "error" in stats:
        st.error("Could not load stats: {}".format(stats.get("error", "unknown")))
        return

    import pandas as pd

    # Users + Squads on one line
    u1, u2, u3, u4, u5 = st.columns(5)
    u1.metric("👥 Users", stats["total_users"])
    u2.metric("⚽ Players", stats["total_players"])
    u3.metric("📊 Avg Squad", "{:.1f}".format(stats["avg_squad"]))
    u4.metric("💰 Top Value", _fmt_m(stats["most_val_user_value"]))
    u5.metric("🏆 Top", stats["most_val_user_email"])

    st.divider()

    # Leagues & clubs pie charts
    import altair as alt

    lc1, lc2 = st.columns(2)
    with lc1:
        st.markdown("### 🏟️ Leagues")
        leagues = stats.get("leagues", [])
        if leagues:
            df = pd.DataFrame(leagues, columns=["League", "Count"])
            st.altair_chart(
                alt.Chart(df).mark_bar().encode(
                    x=alt.X("Count:Q", scale=alt.Scale(domain=[0, int(df["Count"].max()) + 1]), title="Players"),
                    y=alt.Y("League:N", sort="-x", title=None),
                ),
                use_container_width=True,
            )
    with lc2:
        st.markdown("### 🏢 Clubs")
        clubs = stats.get("clubs", [])
        if clubs:
            df = pd.DataFrame(clubs[:10], columns=["Club", "Count"])
            st.altair_chart(
                alt.Chart(df).mark_bar().encode(
                    x=alt.X("Count:Q", scale=alt.Scale(domain=[0, int(df["Count"].max()) + 1]), title="Players"),
                    y=alt.Y("Club:N", sort="-x", title=None),
                ),
                use_container_width=True,
            )

    # Age, Role, Position distributions
    ages = stats.get("ages", [])
    roles = stats.get("roles", [])
    positions = stats.get("positions", [])

    if ages or roles or positions:
        st.divider()
        ac1, ac2, ac3 = st.columns(3)

        with ac1:
            st.markdown("### 🎂 Age")
            if ages:
                df_age = pd.DataFrame({"Age": ages})
                st.altair_chart(
                    alt.Chart(df_age).mark_bar(color="#FFD700").encode(
                        x=alt.X("Age:Q", bin=alt.Bin(step=2), title="Age"),
                        y=alt.Y("count()", title="Players"),
                    ),
                    use_container_width=True,
                )

        with ac2:
            st.markdown("### 🏷️ Role")
            if roles:
                role_order = ["GK", "DEF", "MID", "ATT"]
                role_colors = {"GK": "#FFC107", "DEF": "#4CAF50", "MID": "#2196F3", "ATT": "#F44336"}
                df_role = pd.DataFrame(roles, columns=["Role", "Count"])
                st.altair_chart(
                    alt.Chart(df_role).mark_arc(innerRadius=40).encode(
                        theta=alt.Theta("Count:Q"),
                        color=alt.Color("Role:N",
                            scale=alt.Scale(
                                domain=list(role_colors.keys()),
                                range=list(role_colors.values()),
                            ),
                            sort=role_order,
                        ),
                        tooltip=["Role", "Count"],
                    ),
                    use_container_width=True,
                )

        with ac3:
            st.markdown("### 📍 Position")
            if positions:
                # Map each position to its role color
                pos_to_role = {
                    "Goalkeeper": "GK", "Centre-Back": "DEF", "Right-Back": "DEF", "Left-Back": "DEF",
                    "Defensive Midfield": "MID", "Central Midfield": "MID", "Attacking Midfield": "MID",
                    "Right Winger": "ATT", "Left Winger": "ATT", "Second Striker": "ATT", "Centre-Forward": "ATT",
                }
                role_colors_map = {"GK": "#FFC107", "DEF": "#4CAF50", "MID": "#2196F3", "ATT": "#F44336", "Other": "#888"}
                df_pos = pd.DataFrame(positions, columns=["Position", "Count"])
                df_pos["Role"] = df_pos["Position"].map(lambda p: pos_to_role.get(p, "Other"))
                df_pos["Color"] = df_pos["Role"].map(lambda r: role_colors_map.get(r, "#888"))

                all_positions = df_pos["Position"].tolist()
                all_colors = df_pos["Color"].tolist()

                st.altair_chart(
                    alt.Chart(df_pos).mark_bar().encode(
                        x=alt.X("Count:Q", scale=alt.Scale(domain=[0, int(df_pos["Count"].max()) + 1]), title="Players"),
                        y=alt.Y("Position:N", sort="-x", title=None),
                        color=alt.Color("Position:N",
                            scale=alt.Scale(domain=all_positions, range=all_colors),
                            legend=None,
                        ),
                    ),
                    use_container_width=True,
                )

    st.divider()

    # Transactions
    st.markdown("### 📊 Transactions")
    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Total", stats["total_txns"])
    t2.metric("Buys", stats["total_buys"])
    t3.metric("Sells", stats["total_sells"])
    t4.metric("Net volume", _fmt_m(stats["total_buys_val"] + stats["total_sells_val"]))

    tv1, tv2 = st.columns(2)
    tv1.caption("💸 Bought: {}".format(_fmt_m(stats["total_buys_val"])))
    tv2.caption("💰 Sold: {}".format(_fmt_m(stats["total_sells_val"])))

    if stats.get("biggest_buy"):
        b = stats["biggest_buy"]
        st.caption("🔝 Biggest buy: **{}** — {}".format(
            b.get("player_name", "?"), _fmt_m(float(b.get("deal_value_m") or 0))
        ))
    if stats.get("biggest_sell"):
        b = stats["biggest_sell"]
        st.caption("🔝 Biggest sell: **{}** — {}".format(
            b.get("player_name", "?"), _fmt_m(float(b.get("deal_value_m") or 0))
        ))
    if stats.get("top_bought"):
        st.caption("🏆 **Most bought**: " + " · ".join(
            "{} ({})".format(n, c) for n, c in stats["top_bought"]
        ))

    st.divider()

    # Saved teams + Searches
    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown("### 🏆 Saved teams")
        st.metric("Total", stats["total_saved_teams"])
        st.caption("Avg per user: {:.1f}".format(stats["avg_saved_per_user"]))
    with sc2:
        st.markdown("### 🔎 Searches")
        st.metric("Total searches", stats.get("total_searches", 0))
        avg_s = stats.get("total_searches", 0) / stats["total_users"] if stats["total_users"] else 0
        st.caption("Avg per user: {:.1f}".format(avg_s))
        if stats.get("top_searchers"):
            st.caption("Top: " + " · ".join(
                "{} ({})".format(e, c) for e, c in stats["top_searchers"] if c > 0
            ))

    # User activity
    if stats.get("user_activity"):
        st.divider()
        st.markdown("### 👤 User Activity")
        for name, last_at, searches in stats["user_activity"]:
            ts = ""
            if last_at and isinstance(last_at, str) and len(last_at) > 16:
                ts = last_at[:16].replace("T", " ")
            st.caption("**{}** — last active: {} · {} searches".format(
                name, ts or "never", searches
            ))


@st.dialog("Manage Users", width="large")
def manage_users_dialog():
    # Admin-only (defense in depth)
    real_uid = st.session_state.get("real_user_id") or _current_user_id()
    if not storage.is_admin(real_uid):
        st.error("Admin access required.")
        return

    profiles = storage.list_all_profiles()
    if not profiles:
        st.info("No users found or function not available.")
        return

    # Pagination
    page_size = 10
    total_pages = max(1, (len(profiles) + page_size - 1) // page_size)
    page = st.session_state.get("mu_page", 0)
    if page >= total_pages:
        page = total_pages - 1
    start = page * page_size
    page_profiles = profiles[start:start + page_size]

    st.caption("{} users · Page {}/{}".format(len(profiles), page + 1, total_pages))
    if total_pages > 1:
        pc1, pc2, pc3 = st.columns([1, 2, 1])
        if pc1.button("◀ Prev", disabled=(page == 0), use_container_width=True, key="mu_prev"):
            st.session_state["mu_page"] = page - 1
        if pc3.button("Next ▶", disabled=(page >= total_pages - 1), use_container_width=True, key="mu_next"):
            st.session_state["mu_page"] = page + 1

    for p in page_profiles:
        uid = p.get("user_id", "")
        email = p.get("email", "?")
        # Name priority: nickname > first+last > email
        nick = p.get("nickname", "")
        fn = p.get("first_name", "")
        ln = p.get("last_name", "")
        full = "{} {}".format(fn, ln).strip()
        name = nick or full or email
        team = p.get("team_name", "")
        is_adm = p.get("is_admin", False)
        is_prem = p.get("is_premium", False)

        last_at = p.get("last_active_at", "")
        if isinstance(last_at, str) and len(last_at) > 16:
            last_at = last_at[:16].replace("T", " ")
        elif not last_at:
            last_at = "never"

        c1, c2, c3, c4, c5, c6 = st.columns([3.5, 0.8, 0.8, 0.5, 0.5, 0.5])
        c1.markdown(
            "**{}** · _{}_ · <small><code>{}</code></small> · <small>🕐 {}</small>".format(
                name, team or "no team", email, last_at
            ),
            unsafe_allow_html=True,
        )
        new_admin = c2.checkbox(
            "Admin", value=bool(is_adm), key="adm_{}".format(uid)
        )
        new_premium = c3.checkbox(
            "Prem", value=bool(is_prem), key="prem_{}".format(uid)
        )
        if c4.button("💾", key="save_u_{}".format(uid), help="Save changes"):
            storage.admin_update_profile(uid, {
                "is_admin": new_admin,
                "is_premium": new_premium,
            })
            st.success("Updated {}".format(name))
            st.rerun()

        # Edit profile button — toggle single editing user
        if c5.button("✏️", key="btn_edit_u_{}".format(uid), help="Edit profile"):
            if st.session_state.get("mu_editing_uid") == uid:
                st.session_state.pop("mu_editing_uid", None)
            else:
                st.session_state["mu_editing_uid"] = uid

        # Impersonate button
        current_real = st.session_state.get("real_user_id", _current_user_id())
        if uid != current_real:
            if c6.button("👤", key="imp_{}".format(uid), help="Impersonate " + name):
                # Check if admin already has a PIN
                real_profile = storage.get_profile(current_real)
                saved_pin = real_profile.get("admin_pin", "")
                if saved_pin:
                    # Start impersonation directly with saved PIN
                    st.session_state["real_user_id"] = current_real
                    st.session_state["real_user_email"] = st.session_state["user"].get("email", "")
                    st.session_state["impersonate_pin"] = saved_pin
                    st.session_state["user"] = {
                        "id": uid,
                        "email": email,
                        "display_name": name,
                    }
                    for k in ("players", "app_settings", "owned_map"):
                        st.session_state.pop(k, None)
                    st.rerun()
                else:
                    # First time — ask to set PIN
                    st.session_state["pending_impersonate"] = {
                        "uid": uid,
                        "email": email,
                        "name": name,
                    }
                    st.rerun()

    # Inline edit form for the selected user (rendered outside the loop, no rerun needed)
    edit_uid = st.session_state.get("mu_editing_uid")
    if edit_uid:
        ep = next((x for x in profiles if x.get("user_id") == edit_uid), None)
        if ep:
            st.divider()
            st.markdown("**Editing: {}**".format(
                ep.get("nickname") or "{} {}".format(ep.get("first_name", ""), ep.get("last_name", "")).strip() or ep.get("email", "?")
            ))
            ec1, ec2 = st.columns(2)
            new_fn = ec1.text_input("First name", value=ep.get("first_name", ""), key="efn")
            new_ln = ec2.text_input("Last name", value=ep.get("last_name", ""), key="eln")
            ec3, ec4 = st.columns(2)
            new_nick = ec3.text_input("Nickname", value=ep.get("nickname", ""), key="enk")
            new_team = ec4.text_input("Team name", value=ep.get("team_name", ""), key="etm")
            sc1, sc2 = st.columns(2)
            if sc1.button("💾 Save profile", use_container_width=True, key="save_edit_prof"):
                storage.admin_update_profile(edit_uid, {
                    "first_name": new_fn.strip(),
                    "last_name": new_ln.strip(),
                    "nickname": new_nick.strip(),
                    "team_name": new_team.strip(),
                })
                st.session_state.pop("mu_editing_uid", None)
                st.success("Updated!")
                st.rerun()
            if sc2.button("Cancel", use_container_width=True, key="cancel_edit_prof"):
                st.session_state.pop("mu_editing_uid", None)
                st.rerun()


@st.dialog("Danger Zone")
def danger_zone_dialog():
    st.warning(
        "This will permanently delete ALL players, transactions, "
        "and reset your budget. This cannot be undone."
    )
    confirm = st.text_input("Type RESET to confirm")
    c1, c2 = st.columns(2)
    if c1.button(
        "🔥 Reset Everything", type="primary",
        disabled=(confirm != "RESET"), use_container_width=True,
    ):
        storage.delete_all_data(_current_user_id())
        for k in ("players", "editing_id", "prefill", "search_results",
                   "detail_id", "form_version", "app_settings"):
            st.session_state.pop(k, None)
        settings = load_settings()
        settings.pop("purchase_price_migration_done", None)
        save_settings(settings)
        st.success("All data reset!")
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    st.set_page_config(
        page_title="Football Stars", page_icon="⚽", layout="wide"
    )

    # Auth gate
    user = require_login()
    # If impersonating, keep the impersonated user in session (don't overwrite)
    if not st.session_state.get("real_user_id"):
        st.session_state["user"] = user
    else:
        # We're impersonating — use the impersonated user, not the real one
        user = st.session_state["user"]

    init_state()
    settings = load_settings()

    # Global UI styles
    st.markdown(
        """
        <style>
        /* === ACCENT COLOR: Gold (#FFD700) throughout === */
        :root {
            --accent: #FFD700;
            --accent-dim: #B8960F;
            --link: #5BA8F5;
            --link-hover: #8CC8FF;
            --green: #4CAF50;
            --yellow: #FFC107;
            --red: #F44336;
        }

        /* Tab bar: background strip + bigger text */
        [data-baseweb="tab-list"] {
            background: rgba(255,255,255,0.04) !important;
            border-radius: 8px !important;
            padding: 4px 8px !important;
            border-bottom: 2px solid rgba(255,255,255,0.08) !important;
        }
        button[data-baseweb="tab"] {
            font-size: 0.95rem !important;
            font-weight: 500 !important;
            padding: 8px 16px !important;
        }
        /* Active tab: gold underline + brighter text */
        button[data-baseweb="tab"][aria-selected="true"] {
            border-bottom-color: var(--accent) !important;
            color: var(--accent) !important;
            font-weight: 700 !important;
        }

        /* === TABLE: Name column as clean links === */
        div[class*="st-key-tbl_name_"] button {
            background: transparent !important;
            border: none !important;
            padding: 0.15rem 0 !important;
            box-shadow: none !important;
            color: var(--link) !important;
            font-weight: 500 !important;
            text-align: left !important;
            justify-content: flex-start !important;
            min-height: 0 !important;
        }
        div[class*="st-key-tbl_name_"] button:hover {
            color: var(--link-hover) !important;
            text-decoration: underline !important;
        }
        div[class*="st-key-tbl_name_"] button:focus {
            box-shadow: none !important;
        }

        /* === TABLE: Header buttons compact === */
        div[class*="st-key-hdr_"] button {
            background: transparent !important;
            border: none !important;
            padding: 0.1rem 0 !important;
            box-shadow: none !important;
            font-size: 0.8rem !important;
            font-weight: 700 !important;
            min-height: 0 !important;
            white-space: nowrap !important;
            line-height: 1.2 !important;
            color: var(--accent-dim) !important;
        }
        div[class*="st-key-hdr_"] button:hover {
            text-decoration: underline !important;
            color: var(--accent) !important;
        }

        /* === TACTICS: Disable text input in dropdowns === */
        div[class*="st-key-tac_"] input[role="combobox"] {
            pointer-events: none !important;
            caret-color: transparent !important;
        }
        div[class*="st-key-tac_"] [data-baseweb="select"] > div {
            cursor: pointer !important;
        }

        /* === TYPOGRAPHY: Better hierarchy === */
        h1 {
            letter-spacing: -0.02em !important;
        }
        .stMetric label {
            font-size: 0.75rem !important;
            text-transform: uppercase !important;
            letter-spacing: 0.05em !important;
            color: #888 !important;
        }
        .stMetric [data-testid="stMetricValue"] {
            font-weight: 700 !important;
        }

        /* === ANIMATIONS: Subtle fade-in for containers === */
        [data-testid="stVerticalBlock"] > div {
            animation: fadeIn 0.3s ease-in;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Pulse animation for full squad badge */
        .squad-full-pulse {
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }

        /* === SPACING: More breathing room === */
        [data-testid="stHorizontalBlock"] {
            gap: 0.75rem !important;
        }
        .stDivider {
            margin: 1.2rem 0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Admin rights are based on the currently-acting user (impersonated or real).
    # This means if an admin impersonates a non-admin, admin menu items disappear.
    real_uid = st.session_state.get("real_user_id") or _current_user_id()
    impersonating = bool(st.session_state.get("real_user_id"))
    # real_is_admin: needed to decide whether to show the "Exit impersonation" banner
    real_is_admin = storage.is_admin(real_uid)
    # user_is_admin: controls menu visibility based on CURRENT (possibly impersonated) user
    user_is_admin = storage.is_admin(_current_user_id())

    # Load profile of the currently-acting user (impersonated or real)
    profile = storage.get_profile(_current_user_id())
    label = _display_name(user, profile)

    # Impersonation banner
    if impersonating:
        real_email = st.session_state.get("real_user_email", "admin")
        b1, b2 = st.columns([5, 1])
        b1.warning("👤 Impersonating **{}** 🔒".format(label))
        if b2.button("🔑 Exit", use_container_width=True, key="revert_imp"):
            st.session_state["exit_impersonate_request"] = True
            st.rerun()

    # Auto-start tour for first-time users
    if not profile.get("tour_seen") and not st.session_state.get("tour_active"):
        st.session_state["tour_active"] = True
        st.session_state["tour_step"] = 0

    # Header with language toggle + help + hamburger menu
    h1, h_lang, h_help, h2 = st.columns([20, 2, 2, 2])
    with h1:
        team_name = profile.get("team_name") or "My Football Stars"
        st.title("⚽ {}".format(team_name))
    with h_help:
        st.write("")
        if st.button("❓", key="show_tour_header", use_container_width=True):
            st.session_state["tour_active"] = True
            st.session_state["tour_step"] = 0
            st.rerun()
    with h_lang:
        st.write("")
        cur_lang = _get_analysis_lang()
        lang_opts = ["ENG", "ITA"]
        lang_map = {"ENG": "English", "ITA": "Italiano"}
        cur_short = "ITA" if cur_lang == "Italiano" else "ENG"
        new_short = st.radio(
            "lang", lang_opts, index=lang_opts.index(cur_short),
            horizontal=True, label_visibility="collapsed", key="lang_toggle"
        )
        if lang_map[new_short] != cur_lang:
            _save_analysis_lang(lang_map[new_short])
    with h2:
        st.write("")
        with st.popover("☰", use_container_width=True):
            st.caption("👤 {}".format(label))
            if st.button("✏️ Edit Profile", use_container_width=True, key="open_profile"):
                st.session_state["show_profile_dialog"] = True
                st.rerun()
            if user_is_admin:
                if st.button("⚙️ App Settings", use_container_width=True, key="open_settings"):
                    st.session_state["show_settings_dialog"] = True
                    st.rerun()
                if st.button("👥 Manage Users", use_container_width=True, key="open_users"):
                    st.session_state["show_users_dialog"] = True
                    st.rerun()
                if st.button("🔑 Change Admin PIN", use_container_width=True, key="open_pin"):
                    st.session_state["show_pin_dialog"] = True
                    st.rerun()
                if st.button("⚠️ Danger Zone", use_container_width=True, key="open_danger"):
                    st.session_state["show_danger_dialog"] = True
                    st.rerun()
            if user_is_admin:
                if st.button("🔄 Refresh All Players", use_container_width=True, key="menu_refresh_all"):
                    st.session_state["do_refresh_all"] = True
                    st.rerun()
            st.divider()
            if st.button("🚪 Sign out", use_container_width=True, key="signout_btn"):
                # If impersonating, require PIN to exit first
                if st.session_state.get("real_user_id"):
                    st.session_state["exit_impersonate_request"] = True
                    st.rerun()
                else:
                    from auth import _clear_session
                    _clear_session()
                    st.stop()

    players = st.session_state.get("players", [])

    # Load cross-user ownership data (cached per render)
    if "owned_map" not in st.session_state:
        st.session_state.owned_map = storage.get_all_owned_tm_urls(
            exclude_user_id=_current_user_id()
        )
    owned_map = st.session_state.owned_map

    # --- Guided tour ---
    if st.session_state.get("tour_active"):
        _show_tour()

    # --- Post-transaction analysis (shown once after buy/sell) ---
    post_analysis = st.session_state.pop("post_txn_analysis", None)
    if post_analysis:
        with st.expander("🤖 Post-transaction analysis", expanded=True):
            st.markdown(post_analysis)

    # --- Search bar ---
    transfermarkt_search_bar()

    # --- Buy / Edit form ---
    if st.session_state.editing_id is not None:
        editing = next(
            (p for p in players if p["id"] == st.session_state.editing_id), None
        )
        if editing:
            st.divider()
            st.subheader("✏️ Editing: {}".format(editing["name"]))
            player_form(existing=editing)
            st.divider()
    elif st.session_state.get("prefill"):
        st.divider()
        buy_player_form()
        st.divider()

    # --- Budget metrics (transaction-based) ---
    budget_info = storage.compute_budget(_current_user_id())
    portfolio = sum(_mv_num(p) for p in players)
    net_invested = budget_info["total_buys"] - budget_info["total_sells"]
    gain_loss = portfolio - net_invested

    cash = budget_info["cash"]
    cash_pct = cash / budget_info["initial"] * 100 if budget_info["initial"] else 0
    cash_color = "#4CAF50" if cash_pct > 30 else "#FFC107" if cash_pct > 10 else "#F44336"
    gain_color = "#4CAF50" if gain_loss >= 0 else "#F44336"
    gain_str = "+{}".format(_fmt_m(gain_loss)) if gain_loss >= 0 else _fmt_m(gain_loss)

    b1, b2, b3, b4, b5, b6 = st.columns(6)
    b1.metric("💰 Budget", _fmt_m(budget_info["initial"]))
    b2.metric("🛒 Bought", _fmt_m(budget_info["total_buys"]))
    b3.metric("💸 Sold", _fmt_m(budget_info["total_sells"]))
    b4.markdown(
        "<small style='color:#888;text-transform:uppercase;letter-spacing:0.05em'>💵 Cash</small><br>"
        "<span style='font-size:1.8rem;font-weight:700;color:{}'>{}</span>".format(
            cash_color, _fmt_m(cash)
        ),
        unsafe_allow_html=True,
    )
    b5.metric("📊 Portfolio", _fmt_m(portfolio))
    b6.markdown(
        "<small style='color:#888;text-transform:uppercase;letter-spacing:0.05em'>📈 Gain/Loss</small><br>"
        "<span style='font-size:1.8rem;font-weight:700;color:{}'>{}</span>".format(
            gain_color, gain_str
        ),
        unsafe_allow_html=True,
    )

    if players:
        # Count by role group
        _ROLE_MAP = {
            "Goalkeeper": "GK",
            "Centre-Back": "DEF", "Right-Back": "DEF", "Left-Back": "DEF",
            "Defensive Midfield": "MID", "Central Midfield": "MID",
            "Attacking Midfield": "MID",
            "Right Winger": "ATT", "Left Winger": "ATT",
            "Second Striker": "ATT", "Centre-Forward": "ATT",
        }
        counts = {"GK": 0, "DEF": 0, "MID": 0, "ATT": 0}
        for p in players:
            role = _ROLE_MAP.get(p.get("position", ""), "")
            if role:
                counts[role] += 1
        ss_players = [p for p in players if p.get("sofascore_rating")]
        avg_ss = (
            sum(float(p["sofascore_rating"]) for p in ss_players) / len(ss_players)
            if ss_players else 0
        )
        age_players = [p for p in players if p.get("age")]
        avg_age = sum(p["age"] for p in age_players) / len(age_players) if age_players else 0
        squad_label = "{}/{}".format(len(players), _max_squad())
        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        # Pulse effect when squad is full
        if len(players) >= _max_squad():
            m1.markdown(
                "<small style='color:#888;text-transform:uppercase;letter-spacing:0.05em'>👥 Squad</small><br>"
                "<span class='squad-full-pulse' style='font-size:1.8rem;font-weight:700;color:#FFD700'>{}</span>".format(squad_label),
                unsafe_allow_html=True,
            )
        else:
            m1.metric("👥 Squad", squad_label)
        m2.metric("🧤 GK", counts["GK"])
        m3.metric("🛡️ DEF", counts["DEF"])
        m4.metric("⚙️ MID", counts["MID"])
        m5.metric("⚔️ ATT", counts["ATT"])
        m6.metric("🎂 Avg Age", "{:.1f}".format(avg_age) if avg_age else "-")

        # Color-code avg SofaScore
        ss_color = "#4CAF50" if avg_ss >= 7.5 else "#FFC107" if avg_ss >= 7.0 else "#F44336" if avg_ss else "#888"
        m7.markdown(
            "<small style='color:#888;text-transform:uppercase;letter-spacing:0.05em'>📈 Avg SofaScore</small><br>"
            "<span style='font-size:1.8rem;font-weight:700;color:{}'>{}</span>".format(
                ss_color, "{:.2f}".format(avg_ss) if avg_ss else "-"
            ),
            unsafe_allow_html=True,
        )

    st.divider()

    # --- Tabs (persist selection via query params) ---
    _TAB_NAMES = ["⚽ Squad", "🗺️ Map", "⚔️ Tactics", "🏆 Teams", "📊 Transactions", "📋 Analysis", "💬 Ask ChatGPT", "📊 Stats"]
    _TAB_KEYS = ["squad", "map", "tactics", "teams", "transactions", "analysis", "chatgpt", "stats"]

    # Inject JS to track tab clicks and update URL query param
    import streamlit.components.v1 as _components
    _components.html(
        """
        <script>
        (function() {
            const tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
            tabs.forEach(function(tab, idx) {
                tab.addEventListener('click', function() {
                    const keys = """ + json.dumps(_TAB_KEYS) + """;
                    const url = new URL(window.parent.location);
                    url.searchParams.set('tab', keys[idx]);
                    window.parent.history.replaceState(null, '', url.toString());
                });
            });
        })();
        </script>
        """,
        height=0,
    )

    # Restore tab from query param (Streamlit doesn't support default tab natively,
    # but we can use st.query_params to auto-click the right tab via JS)
    saved_tab = st.query_params.get("tab", "squad")
    if saved_tab in _TAB_KEYS:
        saved_idx = _TAB_KEYS.index(saved_tab)
        if saved_idx > 0:
            _components.html(
                """
                <script>
                (function() {{
                    setTimeout(function() {{
                        const tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
                        if (tabs.length > {idx}) tabs[{idx}].click();
                    }}, 100);
                }})();
                </script>
                """.format(idx=saved_idx),
                height=0,
            )

    all_tabs = st.tabs(_TAB_NAMES)
    tab_idx = {k: v for k, v in zip(_TAB_KEYS, all_tabs)}
    tab_squad = tab_idx["squad"]
    tab_map = tab_idx["map"]
    tab_tactics = tab_idx["tactics"]
    tab_teams = tab_idx["teams"]
    tab_transactions = tab_idx["transactions"]
    tab_report = tab_idx["analysis"]
    tab_gpt = tab_idx["chatgpt"]
    tab_stats = tab_idx["stats"]

    # Handle refresh all from menu (dialog)
    if st.session_state.pop("do_refresh_all", False):
        refresh_all_players()
        st.stop()

    with tab_squad:
        # Sort only (no filters, no view toggle — always table)
        sorted_players = _sort_players(
            players, st.session_state.table_sort_col, st.session_state.table_sort_desc
        )

        if not sorted_players:
            st.info("No players yet! Search and buy your first one. ⚽")
        else:
            player_table(sorted_players)
            last_refresh = _app_settings().get("last_refresh_at", "")
            if last_refresh:
                st.caption("📅 Data from TM & SofaScore updated at {}".format(last_refresh))

    with tab_map:
        squad_map_tab(players)

    with tab_tactics:
        tactics_tab(players)

    with tab_teams:
        saved_teams_tab(players)

    with tab_transactions:
        transactions_tab()

    with tab_report:
        # Full assessment tab
        min_p = _min_players_for_analysis()
        last_analysis = storage.get_last_analysis(_current_user_id())
        if last_analysis:
            st.markdown(last_analysis)

            # Player verdicts grouped by tier
            verdict_players = [p for p in players if p.get("verdict")]
            if verdict_players:
                st.divider()
                st.markdown("### Player Verdicts")
                tier_groups = [
                    "🔒 Lock Him In",
                    "💪 Keep & Build Around",
                    "🤔 Hold For Now",
                    "⚠️ Consider Selling",
                    "🚨 Sell ASAP",
                ]
                for tier in tier_groups:
                    group = [p for p in verdict_players if p.get("verdict") == tier]
                    if not group:
                        continue
                    st.markdown("**{}**".format(tier))
                    for p in group:
                        role = ROLE_PREFIX.get(p.get("position", ""), "")
                        reason = p.get("verdict_reason", "")
                        line = "**{name}** · {role} {pos} · Age {age} · {mv}".format(
                            name=p.get("name", "?"),
                            role=role,
                            pos=p.get("position", ""),
                            age=p.get("age", "?"),
                            mv=p.get("market_value", "?"),
                        )
                        if reason:
                            line += "  \n_{}_".format(reason)
                        st.markdown(line)

            st.divider()
        if len(players) < min_p:
            st.info("Need at least {} players for a full analysis ({}/{}).".format(
                min_p, len(players), min_p
            ))
        else:
            ac1, ac2 = st.columns(2)
            if ac1.button(
                "🔄 Update Analysis" if last_analysis else "🤖 Generate Analysis",
                use_container_width=True,
                key="report_refresh",
            ):
                with st.spinner("Sonnet is analyzing your squad..."):
                    analysis = _run_analysis()
                    if analysis:
                        st.rerun()
                    else:
                        st.error("Analysis failed. Check your API key.")
            if ac2.button(
                "⚖️ Update Verdicts",
                use_container_width=True,
                key="verdicts_refresh",
            ):
                with st.spinner("Sonnet is evaluating players..."):
                    _generate_and_save_verdicts()
                    st.rerun()


    with tab_gpt:
        chatgpt_tab(players)

    with tab_stats:
        global_stats_tab()

    # --- Dialogs ---
    if st.session_state.get("selling_id") is not None:
        sell_p = next(
            (x for x in players if x["id"] == st.session_state["selling_id"]), None
        )
        st.session_state.pop("selling_id", None)
        if sell_p:
            sell_player_dialog(sell_p)

    if st.session_state.detail_id is not None:
        detail_player = next(
            (x for x in players if x["id"] == st.session_state.detail_id), None
        )
        st.session_state.detail_id = None
        if detail_player:
            player_detail_dialog(detail_player)

    if st.session_state.pop("show_profile_dialog", False):
        profile_dialog(profile)

    if st.session_state.pop("show_settings_dialog", False):
        settings_dialog()

    if st.session_state.pop("show_users_dialog", False):
        manage_users_dialog()


    if st.session_state.pop("show_pin_dialog", False):
        change_pin_dialog()

    if st.session_state.pop("show_save_team_dialog", False):
        save_team_dialog(players)

    if st.session_state.get("view_team_id"):
        tid = st.session_state.pop("view_team_id")
        team = next((t for t in storage.list_saved_teams(_current_user_id())
                     if t.get("id") == tid), None)
        if team:
            saved_team_detail_dialog(team)

    if st.session_state.get("rename_team_id"):
        rename_team_dialog()

    if st.session_state.get("pending_impersonate"):
        impersonate_pin_setup_dialog()

    if st.session_state.get("exit_impersonate_request"):
        impersonate_pin_exit_dialog()

    if st.session_state.pop("show_danger_dialog", False):
        danger_zone_dialog()


if __name__ == "__main__":
    main()
