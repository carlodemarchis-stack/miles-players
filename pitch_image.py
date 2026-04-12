"""Generate a squad map image using Pillow."""

from __future__ import annotations

import io
import urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter


# Pitch dimensions
PITCH_W = 1600
PITCH_H = 2100
MARGIN_TOP = 80  # space for header (team name, stats)
HEADER_H = 220  # full header area

# Colors
GREEN_DARK = (26, 107, 53)
GREEN_LIGHT = (31, 122, 61)
WHITE = (255, 255, 255)
WHITE_TRANSPARENT = (255, 255, 255, 180)
CARD_BG = (0, 0, 0, 190)
TEXT_WHITE = (255, 255, 255)
TEXT_LIGHT = (220, 220, 220)
TEXT_DIM = (170, 170, 170)
BG_DARK = (15, 20, 15)

# Position coordinates (0-100 scale) — spaced out to avoid card overlaps
_POSITION_COORDS = {
    "Goalkeeper":         {"y": 94, "x_base": 50},
    "Centre-Back":        {"y": 78, "x_base": 50},
    "Right-Back":         {"y": 74, "x_base": 85},
    "Left-Back":          {"y": 74, "x_base": 15},
    "Defensive Midfield": {"y": 62, "x_base": 50},
    "Central Midfield":   {"y": 50, "x_base": 50},
    "Attacking Midfield": {"y": 38, "x_base": 50},
    "Right Winger":       {"y": 24, "x_base": 82},
    "Left Winger":        {"y": 24, "x_base": 18},
    "Second Striker":     {"y": 14, "x_base": 50},
    "Centre-Forward":     {"y": 8, "x_base": 50},
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


def _spread_players(group, x_base, spread=16):
    n = len(group)
    if n == 1:
        return [(x_base, group[0])]
    coords = []
    total = spread * (n - 1)
    start = x_base - total / 2
    for i, p in enumerate(group):
        x = max(8, min(92, start + i * spread))
        coords.append((x, p))
    return coords


def _load_font(size, bold=False):
    """Try to load a decent font, fall back to default."""
    candidates = []
    if bold:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _fetch_photo(url, size=110):
    """Download and round-crop a player photo."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            img = Image.open(io.BytesIO(r.read())).convert("RGBA")
        img = ImageOps.fit(img, (size, size), Image.LANCZOS)
        # Circular mask
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        img.putalpha(mask)
        return img
    except Exception:
        return None


def _draw_pitch_background(draw, x0, y0, w, h):
    """Draw mowed-stripe pitch background."""
    stripe_h = h / 12
    for i in range(12):
        color = GREEN_DARK if i % 2 == 0 else GREEN_LIGHT
        y = y0 + int(i * stripe_h)
        draw.rectangle([x0, y, x0 + w, y + int(stripe_h) + 1], fill=color)


def _draw_pitch_markings(draw, x0, y0, w, h):
    """Draw all the white lines for a football pitch."""
    line_color = (255, 255, 255, 180)
    lw = 3
    # Outer border
    draw.rectangle([x0, y0, x0 + w, y0 + h], outline=WHITE, width=lw)
    # Halfway line
    mid_y = y0 + h // 2
    draw.line([(x0, mid_y), (x0 + w, mid_y)], fill=WHITE, width=lw)
    # Center circle
    cx, cy = x0 + w // 2, mid_y
    r = 90
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=lw)
    # Center spot
    draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=WHITE)
    # Top penalty area
    pa_w = int(w * 0.56)
    pa_h = int(h * 0.14)
    pa_x = x0 + (w - pa_w) // 2
    draw.rectangle([pa_x, y0, pa_x + pa_w, y0 + pa_h], outline=WHITE, width=lw)
    # Top goal area
    ga_w = int(w * 0.32)
    ga_h = int(h * 0.07)
    ga_x = x0 + (w - ga_w) // 2
    draw.rectangle([ga_x, y0, ga_x + ga_w, y0 + ga_h], outline=WHITE, width=lw)
    # Bottom penalty area
    draw.rectangle([pa_x, y0 + h - pa_h, pa_x + pa_w, y0 + h], outline=WHITE, width=lw)
    # Bottom goal area
    draw.rectangle([ga_x, y0 + h - ga_h, ga_x + ga_w, y0 + h], outline=WHITE, width=lw)


def _draw_text_with_shadow(draw, xy, text, font, fill=TEXT_WHITE):
    """Draw text with a subtle shadow for readability."""
    x, y = xy
    draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, 180))
    draw.text((x, y), text, font=font, fill=fill)


def _get_text_width(draw, text, font):
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * 8


def render_squad_map_image(team_name, players, stats_line="", subtitle=""):
    """Render a squad map as a PIL Image. Returns the image."""
    width = PITCH_W
    height = PITCH_H + HEADER_H

    img = Image.new("RGBA", (width, height), BG_DARK + (255,))
    draw = ImageDraw.Draw(img)

    # Header
    title_font = _load_font(56, bold=True)
    subtitle_font = _load_font(28)
    stats_font = _load_font(26)

    _draw_text_with_shadow(draw, (50, 30), "⚽ " + team_name, title_font)
    if subtitle:
        _draw_text_with_shadow(draw, (50, 105), subtitle, subtitle_font, fill=TEXT_LIGHT)
    if stats_line:
        _draw_text_with_shadow(draw, (50, 145), stats_line, stats_font, fill=TEXT_LIGHT)

    # Pitch
    pitch_x = 40
    pitch_y = HEADER_H
    pitch_w = width - 80
    pitch_h = PITCH_H - 40

    _draw_pitch_background(draw, pitch_x, pitch_y, pitch_w, pitch_h)
    _draw_pitch_markings(draw, pitch_x, pitch_y, pitch_w, pitch_h)

    # Group players by position
    by_position = {}
    for p in players:
        pos = p.get("position", "")
        if pos in _POSITION_COORDS:
            by_position.setdefault(pos, []).append(p)

    # Place players
    placed = []
    for pos, group in by_position.items():
        coords = _POSITION_COORDS[pos]
        spread = _spread_players(group, coords["x_base"])
        for x_pct, p in spread:
            placed.append((x_pct, coords["y"], p))

    # Card geometry
    photo_size = 100
    card_w = 210
    card_h = 230
    padding = 12

    name_font = _load_font(24, bold=True)
    pos_font = _load_font(18, bold=True)
    stat_font = _load_font(18)
    mv_font = _load_font(20, bold=True)

    for x_pct, y_pct, p in placed:
        px = pitch_x + int(pitch_w * x_pct / 100)
        py = pitch_y + int(pitch_h * y_pct / 100)

        # Card background (centered on px,py, contained within pitch)
        card_x0 = max(pitch_x + 5, min(pitch_x + pitch_w - card_w - 5, px - card_w // 2))
        card_y0 = max(pitch_y + 5, min(pitch_y + pitch_h - card_h - 5, py - card_h // 2))
        card_cx = card_x0 + card_w // 2

        # Dark rounded background
        overlay = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rounded_rectangle((0, 0, card_w - 1, card_h - 1), radius=14, fill=CARD_BG)
        img.paste(overlay, (card_x0, card_y0), overlay)

        y_cursor = card_y0 + padding

        # Position tag (top)
        pos_code = SHORT_POS.get(p.get("position", ""), "?")
        pos_w = _get_text_width(draw, pos_code, pos_font)
        _draw_text_with_shadow(
            draw, (card_cx - pos_w // 2, y_cursor), pos_code, pos_font, fill=TEXT_DIM
        )
        y_cursor += 26

        # Photo (centered)
        photo_x = card_cx - photo_size // 2
        photo_y = y_cursor
        photo = _fetch_photo(p.get("photo_url", ""), photo_size) if p.get("photo_url") else None
        # White border
        draw.ellipse(
            [photo_x - 3, photo_y - 3, photo_x + photo_size + 3, photo_y + photo_size + 3],
            outline=WHITE, width=3,
        )
        if photo:
            img.paste(photo, (photo_x, photo_y), photo)
        else:
            draw.ellipse(
                [photo_x, photo_y, photo_x + photo_size, photo_y + photo_size],
                fill=(90, 90, 90),
            )
        y_cursor += photo_size + 10

        # Name (surname, fits in card width)
        surname = p.get("name", "?").split()[-1] if p.get("name") else "?"
        # Truncate if too wide
        while _get_text_width(draw, surname, name_font) > card_w - 2 * padding and len(surname) > 6:
            surname = surname[:-1]
        name_w = _get_text_width(draw, surname, name_font)
        _draw_text_with_shadow(
            draw, (card_cx - name_w // 2, y_cursor), surname, name_font
        )
        y_cursor += 30

        # Age · SofaScore
        age = str(p.get("age", "")) if p.get("age") else ""
        ss = str(p.get("sofascore_rating", "")) if p.get("sofascore_rating") else ""
        stat_parts = []
        if age:
            stat_parts.append(age + "y")
        if ss:
            stat_parts.append("\u2605 " + ss)
        stat_text = "  ".join(stat_parts)
        if stat_text:
            stat_w = _get_text_width(draw, stat_text, stat_font)
            _draw_text_with_shadow(
                draw, (card_cx - stat_w // 2, y_cursor),
                stat_text, stat_font, fill=TEXT_LIGHT,
            )
        y_cursor += 24

        # Market value
        mv = p.get("market_value", "")
        if mv:
            mv_w = _get_text_width(draw, mv, mv_font)
            _draw_text_with_shadow(
                draw, (card_cx - mv_w // 2, y_cursor),
                mv, mv_font, fill=(255, 220, 120),
            )

    # Convert to RGB for PNG (drop alpha)
    final = Image.new("RGB", (width, height), BG_DARK)
    final.paste(img, (0, 0), img)
    return final
