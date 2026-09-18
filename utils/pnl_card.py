"""Visual PnL card for Telegram take-profit / stop-loss alerts."""

from __future__ import annotations

import io
from typing import Any


def render_pnl_card(
    *,
    symbol: str,
    pnl_pct: float,
    pnl_usdt: float,
    title: str,
    side: str = "",
) -> bytes | None:
    """Return a PNG card, or None if Pillow is unavailable."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    win = float(pnl_pct) >= 0
    bg = (10, 16, 14) if win else (22, 10, 12)
    panel = (18, 28, 24) if win else (36, 16, 18)
    accent = (46, 204, 113) if win else (231, 76, 60)
    muted = (140, 155, 148)
    white = (236, 240, 241)

    img = Image.new("RGB", (960, 420), bg)
    draw = ImageDraw.Draw(img)
    try:
        draw.rounded_rectangle((36, 36, 924, 384), radius=28, fill=panel)
    except Exception:
        draw.rectangle((36, 36, 924, 384), fill=panel)
    draw.rectangle((36, 36, 52, 384), fill=accent)

    font_sm = _font(22)
    font_md = _font(32)
    font_lg = _font(72)
    font_xl = _font(48)

    headline = (title or ("TAKE PROFIT" if win else "STOP LOSS")).upper()
    draw.text((88, 60), "CHRONOS-NEXUS", font=font_sm, fill=muted)
    draw.text((88, 96), headline, font=font_md, fill=accent)
    draw.text((88, 150), str(symbol or "—"), font=font_xl, fill=white)
    if side:
        draw.text((88, 210), str(side).upper(), font=font_sm, fill=muted)

    sign = "+" if pnl_pct >= 0 else ""
    usd = "+" if pnl_usdt >= 0 else ""
    draw.text((88, 250), f"{sign}{pnl_pct:.2f}%", font=font_lg, fill=accent)
    draw.text((88, 330), f"{usd}${abs(pnl_usdt):.2f} USDT", font=font_md, fill=white)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _font(size: int) -> Any:
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()
