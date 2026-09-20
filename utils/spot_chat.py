"""Telegram manual SPOT chatbox — preview, confirm, receipt.

Completely separate from ORACLE/SENTINEL/CHAIRMAN. Manual fills are Bitget
Spot market orders only (no swap, no leverage, no SL/TP attach).
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from utils.notifier import escape_html

TICKET_TTL_S = 300
CALLBACK_CONFIRM = "s1"
CALLBACK_CANCEL = "s0"
MIN_QUOTE_USDT = 1.0
# Overflow guard only. Wallet / execute_spot_market is the real size gate
# (e.g. "SOL/USDT BUY $999999" must parse, then reject on insufficient USDT).
MAX_QUOTE_USDT = 1_000_000_000.0

_LOCK = threading.Lock()
_TICKETS: dict[str, "SpotTicket"] = {}

# NVDA/USDT BUY $10  |  NVDA BUY 10 USDT  |  BUY BTC 10  |  /buy NVDA 10
_RE_SYM_SIDE_AMT = re.compile(
    r"^(?P<symbol>[A-Za-z0-9]{2,15}(?:/[A-Za-z]{3,6})?(?::[A-Za-z]{3,6})?)\s+"
    r"(?P<side>buy|sell)\s+"
    r"\$?\s*(?P<amount>\d+(?:\.\d+)?)\s*(?:usdt|usd)?\s*$",
    re.IGNORECASE,
)
_RE_SIDE_SYM_AMT = re.compile(
    r"^(?P<side>buy|sell)\s+"
    r"(?P<symbol>[A-Za-z0-9]{2,15}(?:/[A-Za-z]{3,6})?(?::[A-Za-z]{3,6})?)\s+"
    r"\$?\s*(?P<amount>\d+(?:\.\d+)?)\s*(?:usdt|usd)?\s*$",
    re.IGNORECASE,
)
_RE_SLASH = re.compile(
    r"^/(?P<side>buy|sell)\s+"
    r"(?P<symbol>[A-Za-z0-9]{2,15}(?:/[A-Za-z]{3,6})?(?::[A-Za-z]{3,6})?)\s+"
    r"\$?\s*(?P<amount>\d+(?:\.\d+)?)\s*(?:usdt|usd)?\s*$",
    re.IGNORECASE,
)

_DESK_CMDS = {
    "positions",
    "pos",
    "p",
    "close",
    "closeall",
    "flatten",
    "flat",
    "help",
    "start",
    "?",
    "price",
    "px",
    "balance",
    "bal",
    "balances",
}


@dataclass
class SpotIntent:
    symbol: str
    side: str
    quote_usdt: float
    raw: str


@dataclass
class SpotTicket:
    id: str
    chat_id: str
    symbol: str
    side: str
    quote_usdt: float
    last: float
    qty: float
    balance_usdt: float
    created: float = field(default_factory=time.time)
    status: str = "pending"
    message_id: int | None = None


def parse_spot_intent(text: str) -> SpotIntent | None:
    """Parse a manual spot ticket. None for desk commands or incomplete /buy."""
    raw = " ".join((text or "").strip().split())
    if not raw:
        return None
    lowered = raw.lower()
    head = lowered.split()[0].lstrip("/")
    if head.split("@")[0] in _DESK_CMDS:
        return None
    match = _RE_SLASH.match(raw) or _RE_SYM_SIDE_AMT.match(raw) or _RE_SIDE_SYM_AMT.match(raw)
    if match is None:
        return None
    try:
        amount = float(match.group("amount"))
    except (TypeError, ValueError):
        return None
    if amount != amount or amount in {float("inf"), float("-inf")}:
        return None
    if amount < MIN_QUOTE_USDT or amount > MAX_QUOTE_USDT:
        return None
    side = match.group("side").lower().strip()
    if side not in {"buy", "sell"}:
        return None
    symbol = match.group("symbol").strip().upper().replace("USDT:USDT", "USDT")
    if symbol.endswith(":USDT"):
        symbol = symbol[: -len(":USDT")]
    return SpotIntent(symbol=symbol, side=side, quote_usdt=round(amount, 6), raw=raw)


def parse_callback(data: str) -> tuple[str, str] | None:
    """Return (action, ticket_id) for s1:<id> / s0:<id>."""
    raw = (data or "").strip()
    if ":" not in raw:
        return None
    action, ticket_id = raw.split(":", 1)
    if action not in {CALLBACK_CONFIRM, CALLBACK_CANCEL} or not ticket_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,24}", ticket_id):
        return None
    return action, ticket_id


def issue_ticket(
    *,
    chat_id: str,
    symbol: str,
    side: str,
    quote_usdt: float,
    last: float,
    qty: float,
    balance_usdt: float,
) -> SpotTicket:
    _purge()
    ticket = SpotTicket(
        id=secrets.token_urlsafe(9),
        chat_id=str(chat_id),
        symbol=symbol,
        side=side,
        quote_usdt=float(quote_usdt),
        last=float(last),
        qty=float(qty),
        balance_usdt=float(balance_usdt),
    )
    with _LOCK:
        _TICKETS[ticket.id] = ticket
    return ticket


def get_ticket(ticket_id: str) -> SpotTicket | None:
    _purge()
    with _LOCK:
        return _TICKETS.get(ticket_id)


def consume_ticket(ticket_id: str, *, chat_id: str, action: str) -> SpotTicket | None:
    """One-shot consume. None if missing, expired, wrong chat, or already used."""
    _purge()
    with _LOCK:
        ticket = _TICKETS.get(ticket_id)
        if ticket is None:
            return None
        if ticket.status != "pending":
            return None
        if str(ticket.chat_id) != str(chat_id):
            return None
        if time.time() - ticket.created > TICKET_TTL_S:
            ticket.status = "expired"
            return None
        ticket.status = "confirmed" if action == CALLBACK_CONFIRM else "cancelled"
        return ticket


def bind_message(ticket_id: str, message_id: int) -> None:
    with _LOCK:
        ticket = _TICKETS.get(ticket_id)
        if ticket is not None:
            ticket.message_id = int(message_id)


def confirm_keyboard(ticket_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ CONFIRM TRADE", "callback_data": f"{CALLBACK_CONFIRM}:{ticket_id}"},
                {"text": "❌ CANCEL", "callback_data": f"{CALLBACK_CANCEL}:{ticket_id}"},
            ]
        ]
    }


def preview_html(ticket: SpotTicket) -> str:
    return "\n".join(
        [
            "<b>CHRONOS-NEXUS</b>",
            "🧾 <b>SPOT TRADE PREVIEW</b>",
            "<i>Manual chatbox — Bitget Spot only. AI cycle is not used.</i>",
            "",
            f"<b>Asset:</b> <code>{escape_html(ticket.symbol)}</code>",
            f"<b>Action:</b> <code>{escape_html(ticket.side.upper())}</code>",
            f"<b>Amount:</b> <code>{ticket.quote_usdt:.2f} USDT</code>",
            f"<b>Live spot price:</b> <code>{ticket.last}</code>",
            f"<b>Estimated qty:</b> <code>{ticket.qty:.8f}</code>",
            f"<b>Available USDT (spot):</b> <code>{ticket.balance_usdt:.4f}</code>",
            "",
            "Confirm to submit a <b>Spot market</b> order. No leverage. No futures.",
        ]
    )


def timeline_html(ticket: SpotTicket, stage: str, note: str = "") -> str:
    steps = [
        ("processing", "Processing request"),
        ("balance", "Checking spot balance"),
        ("submit", "Submitting spot market order"),
        ("done", "Completed"),
    ]
    order = [s for s, _ in steps]
    current = stage if stage in order else "processing"
    idx = order.index(current)
    lines = [
        "<b>CHRONOS-NEXUS</b>",
        "⏳ <b>SPOT ORDER TIMELINE</b>",
        f"<code>{escape_html(ticket.side.upper())} {escape_html(ticket.symbol)}</code>  "
        f"{ticket.quote_usdt:.2f} USDT",
        "",
    ]
    for i, (key, label) in enumerate(steps):
        if i < idx:
            mark = "✅"
        elif i == idx:
            mark = "▶️"
        else:
            mark = "▫️"
        lines.append(f"{mark} {label}")
    if note:
        lines.append("")
        lines.append(escape_html(note))
    return "\n".join(lines)


def receipt_html(ticket: SpotTicket, order: dict[str, Any]) -> str:
    ok = bool(order.get("ok"))
    title = "✅ <b>SPOT TRADE RECEIPT</b>" if ok else "❌ <b>SPOT ORDER FAILED</b>"
    fill = order.get("price") or order.get("entry_price") or ticket.last
    qty = order.get("quantity")
    if qty is None:
        qty = order.get("amount")
    if qty is None:
        qty = ticket.qty
    lines = [
        "<b>CHRONOS-NEXUS</b>",
        title,
        "<i>Manual Bitget Spot — not an AI cycle fill.</i>",
        "",
        f"<b>Asset:</b> <code>{escape_html(str(order.get('symbol') or ticket.symbol))}</code>",
        f"<b>Action:</b> <code>{escape_html(ticket.side.upper())}</code>",
        f"<b>Market:</b> <code>SPOT</code>",
        f"<b>Amount:</b> <code>{ticket.quote_usdt:.2f} USDT</code>",
        f"<b>Fill price:</b> <code>{fill}</code>",
        f"<b>Quantity:</b> <code>{qty}</code>",
        f"<b>Order id:</b> <code>{escape_html(str(order.get('order_id') or '—'))}</code>",
        f"<b>Status:</b> <code>{escape_html(str(order.get('status') or ('filled' if ok else 'error')))}</code>",
    ]
    if order.get("error"):
        lines.append(f"<b>Error:</b> {escape_html(order.get('error'))}")
    if order.get("account_balance_after") is not None:
        lines.append(f"<b>USDT after:</b> <code>{order.get('account_balance_after')}</code>")
    return "\n".join(lines)


def cancelled_html(ticket: SpotTicket) -> str:
    return "\n".join(
        [
            "<b>CHRONOS-NEXUS</b>",
            "❌ <b>SPOT TRADE CANCELLED</b>",
            "",
            f"<code>{escape_html(ticket.side.upper())} {escape_html(ticket.symbol)}</code> "
            f"{ticket.quote_usdt:.2f} USDT was not submitted.",
        ]
    )


def _purge() -> None:
    now = time.time()
    with _LOCK:
        dead = [
            key
            for key, ticket in _TICKETS.items()
            if (ticket.status != "pending" and now - ticket.created > TICKET_TTL_S * 3)
            or (now - ticket.created > TICKET_TTL_S * 2)
        ]
        for key in dead:
            _TICKETS.pop(key, None)
