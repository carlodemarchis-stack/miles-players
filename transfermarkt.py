"""Scrape player data from a Transfermarkt profile URL."""

import re
import urllib.parse
import urllib.request
from typing import List, Optional

from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.transfermarkt.us/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

POSITION_MAP = {
    "Goalkeeper": "Goalkeeper",
    "Centre-Back": "Centre-Back",
    "Right-Back": "Right-Back",
    "Left-Back": "Left-Back",
    "Defensive Midfield": "Defensive Midfield",
    "Central Midfield": "Central Midfield",
    "Attacking Midfield": "Attacking Midfield",
    "Right Winger": "Right Winger",
    "Left Winger": "Left Winger",
    "Second Striker": "Second Striker",
    "Centre-Forward": "Centre-Forward",
}


def _get_scrapingbee_key():
    try:
        import streamlit as st
        return st.secrets.get("app", {}).get("scrapingbee_api_key", "")
    except Exception:
        return ""


def _fetch(url: str) -> str:
    # Try ScrapingBee first if key is available (handles TM blocks)
    sb_key = _get_scrapingbee_key()
    if sb_key:
        try:
            sb_url = "https://app.scrapingbee.com/api/v1/?api_key={}&url={}&render_js=false".format(
                sb_key, urllib.parse.quote(url, safe="")
            )
            req = urllib.request.Request(sb_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                encoding = r.headers.get("Content-Encoding", "").lower()
                if encoding == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                elif encoding == "deflate":
                    import zlib
                    raw = zlib.decompress(raw)
                return raw.decode("utf-8", errors="ignore")
        except Exception:
            pass  # Fall through to direct fetch

    # Direct fetch (dev or fallback)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        encoding = r.headers.get("Content-Encoding", "").lower()
        if encoding == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return raw.decode("utf-8", errors="ignore")


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _info_table_dict(soup: BeautifulSoup) -> dict:
    """Parse the 'Player data' info table into label->value dict."""
    out = {}
    for row in soup.select(".info-table__content--regular"):
        label = _text(row).rstrip(":").strip()
        val_el = row.find_next_sibling("span", class_="info-table__content--bold")
        if label and val_el:
            out[label] = _text(val_el)
    return out


def _parse_age(dob_age: str) -> Optional[int]:
    # "Mar 3, 2005 (21)"
    m = re.search(r"\((\d{1,3})\)", dob_age or "")
    return int(m.group(1)) if m else None


def _parse_position(pos_raw: str) -> str:
    # "Defender - Right-Back" -> "Right-Back"
    if not pos_raw:
        return ""
    parts = pos_raw.split("-", 1)
    if len(parts) == 2 and parts[0].strip().lower() in {
        "defender",
        "midfield",
        "attack",
        "striker",
        "goalkeeper",
    }:
        specific = parts[1].strip()
        return POSITION_MAP.get(specific, specific)
    return POSITION_MAP.get(pos_raw.strip(), pos_raw.strip())


def _normalize_height(h: str) -> str:
    # "1,86 m" -> "1.86 m"
    return (h or "").replace(",", ".").strip()


def _market_value(soup: BeautifulSoup) -> str:
    mv = soup.select_one("a.data-header__market-value-wrapper")
    if not mv:
        return ""
    # Strip "Last update:" tail
    txt = _text(mv)
    txt = re.sub(r"Last update.*$", "", txt).strip()
    # Standard format like "€30.00m"
    m = re.search(r"[€$£]\s*[\d.,]+\s*[mk]?", txt, re.IGNORECASE)
    return m.group(0).replace(" ", "") if m else txt


def _league(soup: BeautifulSoup) -> str:
    for a in soup.select(".data-header__club-info a"):
        href = a.get("href", "")
        if "/startseite/wettbewerb/" in href:
            return a.get_text(strip=True)
    return ""


def _photo(soup: BeautifulSoup) -> str:
    img = soup.select_one("img.data-header__profile-image")
    src = img.get("src") if img else ""
    # Prefer big portrait over header
    if src and "/header/" in src:
        src = src.replace("/portrait/header/", "/portrait/big/")
    return src or ""


def scrape_player(url: str) -> dict:
    """Scrape player info from a Transfermarkt profile URL.

    Returns a dict with keys matching the app's player schema
    (may contain empty strings for missing fields).
    """
    if not url.startswith("http"):
        raise ValueError("URL must start with http(s)://")

    html = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    info = _info_table_dict(soup)

    # Name from the info table's page title
    name_el = soup.select_one("h1.data-header__headline-wrapper")
    if name_el:
        # Strip shirt number span if present
        for span in name_el.find_all("span"):
            span.decompose()
        name = _text(name_el)
    else:
        name = ""

    stats = scrape_current_season_stats(url)

    return {
        "apps": stats["apps"],
        "goals": stats["goals"],
        "assists": stats["assists"],
        "name": name,
        "club": info.get("Current club", ""),
        "position": _parse_position(info.get("Position", "")),
        "nationality": info.get("Citizenship", "").split()[0]
        if info.get("Citizenship")
        else "",
        "photo_url": _photo(soup),
        "age": _parse_age(info.get("Date of birth/Age", "")),
        "height": _normalize_height(info.get("Height", "")),
        "market_value": _market_value(soup),
        "foot": info.get("Foot", ""),
        "dob": info.get("Date of birth/Age", "").split("(")[0].strip(),
        "on_loan_from": info.get("On loan from", ""),
        "league": _league(soup),
        "birthplace": info.get("Place of birth", ""),
        "tm_url": url,
    }


def scrape_current_season_stats(profile_url: str) -> dict:
    """Fetch current-season totals (apps, goals, assists) from the stats page.

    Returns {"apps": int, "goals": int, "assists": int} with 0 when '-' / missing.
    """
    stats_url = profile_url.replace("/profil/spieler/", "/leistungsdaten/spieler/")
    html = _fetch(stats_url)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("div.responsive-table table")
    if not table:
        return {"apps": 0, "goals": 0, "assists": 0}

    def _num(x: str) -> int:
        x = (x or "").strip()
        if not x or x == "-":
            return 0
        try:
            return int(x.replace(".", "").replace(",", ""))
        except ValueError:
            return 0

    foot = table.select_one("tfoot tr")
    if not foot:
        return {"apps": 0, "goals": 0, "assists": 0}
    cells = [td.get_text(strip=True) for td in foot.find_all("td")]
    # cells: [label, '', apps, goals, assists, ...]
    return {
        "apps": _num(cells[2]) if len(cells) > 2 else 0,
        "goals": _num(cells[3]) if len(cells) > 3 else 0,
        "assists": _num(cells[4]) if len(cells) > 4 else 0,
    }


def search_player(name: str) -> List[dict]:
    """Search Transfermarkt for players by name. Returns up to 10 candidates."""
    q = urllib.parse.quote(name)
    url = f"https://www.transfermarkt.us/schnellsuche/ergebnis/schnellsuche?query={q}"
    html = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    results = []
    # Player results table
    table = soup.select_one("table.items")
    if not table:
        return results
    for row in table.select("tbody tr"):
        link = row.select_one("td.hauptlink a")
        if not link:
            continue
        href = link.get("href", "")
        if "/profil/spieler/" not in href:
            continue
        full_url = urllib.parse.urljoin("https://www.transfermarkt.us", href)
        cells = row.find_all("td")
        pos = _text(cells[4]) if len(cells) > 4 else ""
        age = _text(cells[6]) if len(cells) > 6 else ""
        value = _text(cells[8]) if len(cells) > 8 else ""
        club_img = row.select_one("img.tiny_wappen")
        club = club_img.get("alt", "") if club_img else ""
        # Skip retired/no-value players
        club_lower = club.lower().strip()
        if club_lower in ("retired", "career break", "without club", ""):
            continue
        if not value or value.strip() in ("-", "€0"):
            continue
        results.append(
            {
                "name": _text(link),
                "url": full_url,
                "position": pos,
                "club": club,
                "age": age,
                "value": value,
            }
        )
        if len(results) >= 10:
            break
    return results
