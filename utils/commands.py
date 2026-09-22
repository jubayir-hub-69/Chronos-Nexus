"""Shared command desk — Telegram and local terminal. Never opens a position."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter

from core.config import PROJECT_ROOT
from core.memory import BoardMemory, sentiment_label
from core.positions import PositionDesk, ticker_root
from connectors.bitget_paper import BitgetPaperConnector
from utils.notifier import TelegramNotifier, escape_html
from utils.spot_chat import (
    CALLBACK_CANCEL,
    CALLBACK_CONFIRM,
    bind_message,
    cancelled_html,
    confirm_keyboard,
    consume_ticket,
    issue_ticket,
    parse_callback,
    parse_spot_intent,
    preview_html,
    receipt_html,
    timeline_html,
)

OFFSET_PATH = PROJECT_ROOT / "data" / "telegram_offset.json"
TELEGRAM_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
TELEGRAM_SET_COMMANDS = "https://api.telegram.org/bot{token}/setMyCommands"
BOT_MENU = [
    {"command": "menu", "description": "Headless terminal dashboard"},
    {"command": "positions", "description": "Live book + PnL"},
    {"command": "close", "description": "Market-close one symbol"},
    {"command": "closeall", "description": "Flatten the whole Demo book"},
    {"command": "price", "description": "Last, 24h high/low, volume, cap"},
    {"command": "balance", "description": "Live ledger free / used / total"},
    {"command": "pnl", "description": "Today's realized PnL and daily limits"},
    {"command": "status", "description": "Live ORACLE / SENTINEL desk state"},
]
HELP_PLAIN = (
    "CHRONOS-NEXUS COMMANDS\n"
    "AI desk opens are automated only. Manual chatbox is Bitget SPOT.\n\n"
    "/menu               headless terminal dashboard + buttons\n"
    "/positions          live book + PnL\n"
    "/close SYMBOL       market-close one name\n"
    "/closeall           flatten the whole Demo book\n"
    "/price SYMBOL       last, 24h high/low, volume, market cap\n"
    "/balance SYMBOL     live ledger free / used / total\n"
    "/pnl                today's realized PnL, W/L, trades left\n"
    "/status             live ORACLE sentiment + SENTINEL desk\n"
    "NVDA/USDT BUY $10   Spot preview + confirm buttons\n"
    "/help               this list"
)
HELP_HTML = (
    "<b>CHRONOS-NEXUS COMMANDS</b>\n"
    "AI cycle is untouched. Manual chatbox is <b>Bitget Spot</b> only.\n\n"
    "<code>/menu</code> — headless terminal dashboard + live buttons\n"
    "<code>/positions</code> — live book + PnL\n"
    "<code>/close SYMBOL</code> — market-close one name\n"
    "<code>/closeall</code> — flatten the whole Demo book\n"
    "<code>/price SYMBOL</code> — last, 24h high/low, volume, market cap\n"
    "<code>/balance SYMBOL</code> — live ledger free / used / total\n"
    "<code>/pnl</code> — today's realized PnL, win/loss, trades left\n"
    "<code>/status</code> — live ORACLE sentiment + SENTINEL desk\n"
    "<code>NVDA/USDT BUY $10</code> — Spot preview, then Confirm / Cancel\n"
    "<code>/help</code> — this list"
)
_DESK_CALLBACKS = {
    "nx:status": "status",
    "nx:pnl": "pnl",
    "nx:positions": "positions",
    "nx:closeall": "closeall_ask",
    "nx:closeall_yes": "closeall_run",
    "nx:home": "menu",
}


@dataclass
class CommandResult:
    cmd: str
    ok: bool
    plain: str
    html: str
    closes: list[dict[str, Any]] = field(default_factory=list)


_DESK_COMMANDS = {
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
    "buy",
    "sell",
    "open",
    "long",
    "short",
    "price",
    "px",
    "balance",
    "bal",
    "balances",
    "pnl",
    "status",
    "menu",
    "dashboard",
}


def parse_command(text: str) -> tuple[str, str]:
    """Return (cmd, arg). cmd is lowercase without a leading slash. Empty cmd if not a command.

    Trading pairs such as SOL/USDT are tickers, never Telegram slash commands.
    Bare words that are not desk commands are ignored (not 'unknown /sol/usdt').
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    had_slash = raw.startswith("/")
    if had_slash:
        raw = raw[1:]
    parts = raw.split()
    if not parts:
        return "", ""
    token = parts[0].split("@")[0]
    # SOL/USDT, BTC/USDT:USDT, etc. — the slash is a pair separator, not /help.
    if "/" in token or ":" in token:
        return "", ""
    cmd = token.lower()
    aliases = {
        "pos": "positions",
        "p": "positions",
        "flatten": "closeall",
        "flat": "closeall",
        "start": "help",
        "?": "help",
        "px": "price",
        "bal": "balance",
        "balances": "balance",
        "dashboard": "menu",
    }
    mapped = aliases.get(cmd, cmd)
    if mapped not in _DESK_COMMANDS and not had_slash:
        return "", ""
    arg = " ".join(parts[1:]).strip()
    return mapped, arg


class CommandDesk:
    """Single close/monitor implementation used by Telegram and the terminal."""

    def __init__(
        self,
        bitget: BitgetPaperConnector | None,
        desk: PositionDesk,
        on_close: Callable[[dict[str, Any]], None] | None = None,
        memory: BoardMemory | None = None,
    ) -> None:
        self.bitget = bitget
        self.desk = desk
        self.on_close = on_close
        self.memory = memory

    def handle(self, text: str, *, source: str = "terminal") -> CommandResult:
        cmd, arg = parse_command(text)
        if not cmd:
            return CommandResult("", False, "", "", [])
        if cmd in {"help"}:
            return CommandResult("help", True, HELP_PLAIN, HELP_HTML, [])
        if cmd == "positions":
            return self._cmd_positions()
        if cmd == "closeall":
            return self._cmd_closeall(source=source)
        if cmd == "close":
            return self._cmd_close(arg, source=source)
        if cmd == "price":
            return self._cmd_price(arg)
        if cmd == "balance":
            return self._cmd_balance(arg)
        if cmd == "pnl":
            return self._cmd_pnl()
        if cmd == "status":
            return self._cmd_status()
        if cmd == "menu":
            return self._cmd_menu()
        if cmd in {"buy", "sell", "open", "long", "short"}:
            msg = (
                "REFUSED — manual open is disabled on the AI desk. "
                "For a Bitget Spot chatbox fill send: NVDA/USDT BUY $10"
            )
            return CommandResult(cmd, False, msg, f"<b>{msg}</b>", [])
        msg = f"Unknown command /{cmd}. Try /help."
        return CommandResult(cmd, False, msg, f"Unknown command <code>/{cmd}</code>. Try /help.", [])

    def _cmd_positions(self) -> CommandResult:
        if self.bitget is None:
            msg = "Bitget Demo rail unbound — cannot read positions."
            return CommandResult("positions", False, msg, msg, [])
        book = self.desk.snapshot(self.bitget)
        if not book:
            plain = "FLAT — no open Demo positions."
            html = "<b>CHRONOS-NEXUS</b>\n📊 <b>OPEN POSITIONS</b>\n\nFLAT — no open Demo positions."
            return CommandResult("positions", True, plain, html, [])
        plain_lines = ["OPEN POSITIONS"]
        html_lines = ["<b>CHRONOS-NEXUS</b>", "📊 <b>OPEN POSITIONS</b>", ""]
        for pos in book:
            pnl_pct = _opt_float(pos.get("pnl_pct"))
            pnl_usdt = _opt_float(pos.get("pnl_usdt"))
            symbol = pos.get("symbol")
            side = str(pos.get("side") or "").upper()
            entry_s = _fmt_px(pos.get("entry_price"))
            mark_s = _fmt_px(pos.get("mark_price"))
            if pnl_pct is None:
                pnl_pct_s = "n/a"
                pnl_usdt_s = "n/a" if pnl_usdt is None else _fmt_signed(pnl_usdt)
                emoji = "MARK"
                html_emoji = "⚪"
            else:
                emoji = "GREEN" if pnl_pct >= 0 else "RED"
                html_emoji = "🟢" if pnl_pct >= 0 else "🔴"
                pnl_pct_s = f"{_fmt_signed(pnl_pct)}%"
                pnl_usdt_s = "n/a" if pnl_usdt is None else _fmt_signed(pnl_usdt)
            plain_lines.append(
                f"{emoji} {symbol}  {side}  qty={pos.get('contracts')}  "
                f"entry={entry_s}  mark={mark_s}  "
                f"PnL {pnl_pct_s}  {pnl_usdt_s} USDT"
            )
            html_lines.append(
                f"{html_emoji} <code>{symbol}</code>  {side}\n"
                f"qty <code>{pos.get('contracts')}</code>  "
                f"entry <code>{entry_s}</code>  "
                f"mark <code>{mark_s}</code>\n"
                f"PnL <b>{pnl_pct_s}</b>  "
                f"<b>{pnl_usdt_s} USDT</b>"
            )
            html_lines.append("")
        return CommandResult(
            "positions",
            True,
            "\n".join(plain_lines),
            "\n".join(html_lines).rstrip(),
            [],
        )

    def _cmd_price(self, arg: str) -> CommandResult:
        if self.bitget is None:
            msg = "Bitget rail unbound — cannot fetch a live price."
            return CommandResult("price", False, msg, msg, [])
        query = (arg or "").strip()
        if not query:
            msg = "Usage: /price SYMBOL   e.g. /price BGB  or  /price BGB/USDT"
            return CommandResult(
                "price",
                False,
                msg,
                "Usage: <code>/price SYMBOL</code> — e.g. <code>/price BGB</code>",
                [],
            )
        try:
            quote = self.bitget.fetch_live_price(query)
        except Exception as exc:
            msg = f"Price lookup failed for {query}: {exc}"
            return CommandResult("price", False, msg, f"<b>Price lookup failed</b>\n{exc}", [])
        last = _safe_px(quote.get("last"), quote.get("ask"), quote.get("bid"), quote.get("mark"))
        if last <= 0 or not quote.get("ok"):
            err = quote.get("error") or f"No live mainnet price for {query}."
            return CommandResult(
                "price",
                False,
                err,
                f"<b>NO LIVE PRICE</b>\n<code>{query}</code>\n{err}",
                [],
            )
        symbol = str(quote.get("symbol") or query).upper()
        bid = _safe_px(quote.get("bid"))
        ask = _safe_px(quote.get("ask"))
        mark = _safe_px(quote.get("mark"))
        high = _metric(quote.get("high"))
        low = _metric(quote.get("low"))
        volume = _metric(quote.get("volume_24h"))
        source = str(quote.get("source") or "bitget.mainnet")
        cap_px = _safe_px(quote.get("market_cap"))
        if cap_px > 0:
            cap_label = "market cap"
            cap_value = _metric(quote.get("market_cap"))
        else:
            cap_label = "notional liquidity"
            cap_value = _metric(quote.get("notional_liquidity"))
        plain = (
            f"LIVE PRICE  {symbol}\n"
            f"last {last}\n"
            f"24h high {high}\n"
            f"24h low {low}\n"
            f"24h volume {volume}\n"
            f"{cap_label} {cap_value}\n"
            f"bid  {bid if bid > 0 else 'n/a'}\n"
            f"ask  {ask if ask > 0 else 'n/a'}\n"
            f"feed {source}"
        )
        html_lines = [
            "<b>CHRONOS-NEXUS</b>",
            "📡 <b>LIVE PRICE</b>  ·  Bitget mainnet",
            "",
            f"<b>Symbol:</b> <code>{symbol}</code>",
            f"<b>Last:</b> <code>{last}</code>",
            f"<b>24h High:</b> <code>{high}</code>",
            f"<b>24h Low:</b> <code>{low}</code>",
            f"<b>24h Volume:</b> <code>{volume}</code>",
            f"<b>{cap_label.title()}:</b> <code>{cap_value}</code>",
            f"<b>Bid:</b> <code>{bid if bid > 0 else 'n/a'}</code>",
            f"<b>Ask:</b> <code>{ask if ask > 0 else 'n/a'}</code>",
        ]
        if mark > 0:
            html_lines.append(f"<b>Mark:</b> <code>{mark}</code>")
            plain += f"\nmark {mark}"
        html_lines.append(f"<b>Feed:</b> <code>{source}</code>")
        return CommandResult("price", True, plain, "\n".join(html_lines), [])

    def _cmd_balance(self, arg: str) -> CommandResult:
        coin = _wallet_coin(arg)
        if not coin:
            msg = "Please specify a token (e.g., /balance USDT or /balance NVDA)"
            return CommandResult("balance", False, msg, msg, [])
        if self.bitget is None:
            msg = "Bitget rail unbound — cannot read the wallet."
            return CommandResult("balance", False, msg, msg, [])
        try:
            payload = self.bitget.fetch_asset_balance(coin)
        except Exception as exc:
            msg = f"Wallet lookup failed: {exc}"
            return CommandResult("balance", False, msg, f"<b>Wallet lookup failed</b>\n{exc}", [])
        if payload.get("ok") is False:
            err = str(payload.get("error") or "Bitget ledger unavailable")
            msg = f"Wallet lookup failed: {err}"
            return CommandResult("balance", False, msg, f"<b>Wallet lookup failed</b>\n{escape_html(err)}", [])
        free = _amt0(payload.get("free"))
        used = _amt0(payload.get("used"))
        total = _amt0(payload.get("total"))
        found = bool(payload.get("found"))
        if not found or (free <= 0 and total <= 0):
            msg = f"0.00 {coin} found in wallet."
            return CommandResult(
                "balance",
                True,
                msg,
                f"<code>0.00 {coin}</code> found in wallet.",
                [],
            )
        label = str(payload.get("coin") or coin).upper()
        source = str(payload.get("source") or "bitget.fetch_balance")
        plain = (
            f"{label}\n"
            f"free  {free:.8f}\n"
            f"used  {used:.8f}\n"
            f"total {total:.8f}\n"
            f"ledger {source}"
        )
        html = "\n".join(
            [
                "<b>CHRONOS-NEXUS</b>",
                f"<code>{label}</code>",
                f"free <code>{free:.8f}</code>",
                f"used <code>{used:.8f}</code>",
                f"total <code>{total:.8f}</code>",
                f"ledger <code>{escape_html(source)}</code>",
            ]
        )
        return CommandResult("balance", True, plain, html, [])

    def _cmd_pnl(self) -> CommandResult:
        mem = self.memory if self.memory is not None else BoardMemory()
        session = mem.session_pnl()
        realized = float(session.get("realized_pnl_usdt") or 0.0)
        wins = int(session.get("wins") or 0)
        losses = int(session.get("losses") or 0)
        left = int(session.get("trades_left") or 0)
        entries = int(session.get("entries") or 0)
        cap = int(session.get("entries_max") or 0)
        sl_hits = int(session.get("sl_hits") or 0)
        day = str(session.get("date") or "")
        halt = str(session.get("halt") or "") or "none"
        halt_reason = str(session.get("halt_reason") or "")
        sign = "+" if realized >= 0 else ""
        unrealized = 0.0
        open_n = 0
        if self.bitget is not None:
            try:
                book = self.desk.snapshot(self.bitget)
            except Exception:
                book = []
            for pos in book or []:
                try:
                    unrealized += float(pos.get("pnl_usdt") or 0.0)
                except (TypeError, ValueError):
                    continue
                open_n += 1
        u_sign = "+" if unrealized >= 0 else ""
        plain_lines = [
            f"SESSION PnL  UTC {day}",
            f"Today's Realized PnL  {sign}{realized:.2f} USDT",
            f"Win / Loss  {wins}W / {losses}L",
            f"Trades left: {left}",
            f"Entries  {entries}/{cap}",
            f"SL hits  {sl_hits}",
            f"Halt  {halt}",
        ]
        html_lines = [
            "<b>CHRONOS-NEXUS</b>",
            "📈 <b>SESSION PnL</b>",
            f"<i>UTC {escape_html(day)} · live ledger + Bitget book</i>",
            "",
            f"<b>Today's Realized PnL:</b> <b>{sign}{realized:.2f} USDT</b>",
            f"<b>Win / Loss:</b> <code>{wins}W / {losses}L</code>",
            f"<b>Trades left:</b> <code>{left}</code>",
            f"<b>Entries:</b> <code>{entries}/{cap}</code>",
            f"<b>SL hits:</b> <code>{sl_hits}</code>",
            f"<b>Halt:</b> <code>{escape_html(halt)}</code>",
        ]
        if halt_reason and halt != "none":
            plain_lines.append(f"Halt reason  {halt_reason}")
            html_lines.append(f"<b>Halt reason:</b> {escape_html(halt_reason)}")
        if self.bitget is not None:
            plain_lines.append(f"Open unrealized  {u_sign}{unrealized:.2f} USDT ({open_n})")
            html_lines.append(
                f"<b>Open unrealized:</b> <b>{u_sign}{unrealized:.2f} USDT</b>  "
                f"({open_n} names)"
            )
        closes = [
            fill
            for fill in (session.get("fills") or [])
            if isinstance(fill, dict) and "pnl_usdt" in fill
        ]
        if closes:
            html_lines.append("")
            html_lines.append("<b>Today's closes</b>")
            plain_lines.append("Today's closes")
            for fill in closes[-8:]:
                try:
                    pnl = float(fill.get("pnl_usdt") or 0.0)
                except (TypeError, ValueError):
                    pnl = 0.0
                kind = str(fill.get("kind") or "CLOSE")
                symbol = str(fill.get("symbol") or "")
                bit = f"{kind} {symbol}  {pnl:+.2f} USDT"
                plain_lines.append(f"  {bit}")
                html_lines.append(f"• <code>{escape_html(bit)}</code>")
        return CommandResult("pnl", True, "\n".join(plain_lines), "\n".join(html_lines), [])

    def _cmd_status(self) -> CommandResult:
        mem = self.memory if self.memory is not None else BoardMemory()
        snap = mem.latest_snapshot()
        daily = mem.daily_state()
        scored = bool(snap.get("scored"))
        sentiment = snap.get("sentiment")
        conviction = snap.get("conviction")
        label = str(snap.get("sentiment_label") or "") or sentiment_label(
            sentiment, scored=scored and sentiment is not None
        )
        desk = str(snap.get("desk") or "") or "STAND_DOWN"
        halt = str(daily.get("halt") or snap.get("daily_halt") or "") or "none"
        action = str(snap.get("action") or "") or "—"
        symbol = str(snap.get("oracle_symbol") or "NONE")
        side = str(snap.get("oracle_side") or "none")
        verdict = str(snap.get("sentinel_verdict") or "—")
        ts = str(snap.get("ts") or "")
        thesis = str(snap.get("oracle_thesis") or "")
        if scored and sentiment is not None:
            try:
                sent_plain = f"{label} {float(sentiment):.2f}"
            except (TypeError, ValueError):
                sent_plain = label
        else:
            sent_plain = "NO SCAN YET"
        if scored and conviction is not None:
            conv_plain = f"{int(conviction)}/100"
        elif scored:
            conv_plain = "0/100"
        else:
            conv_plain = "NO SCAN YET"
        plain_lines = [
            "AI DESK STATUS",
            f"Market Sentiment  {sent_plain}",
            f"AI Conviction  {conv_plain}",
            f"Desk  {desk}",
            f"Last action  {action}",
            f"ORACLE  {symbol} / {side}",
            f"SENTINEL  {verdict}",
            f"Halt  {halt}",
        ]
        if ts:
            plain_lines.append(f"Last scan  {ts}")
        html_lines = [
            "<b>CHRONOS-NEXUS</b>",
            "🧠 <b>AI DESK STATUS</b>",
            "<i>Live ORACLE + SENTINEL snapshot from board memory</i>",
            "",
            f"<b>Market Sentiment:</b> <code>{escape_html(sent_plain)}</code>",
            f"<b>AI Conviction:</b> <code>{escape_html(conv_plain)}</code>",
            f"<b>Desk:</b> <code>{escape_html(desk)}</code>",
            f"<b>Last action:</b> <code>{escape_html(action)}</code>",
            f"<b>ORACLE:</b> <code>{escape_html(symbol)} / {escape_html(side)}</code>",
            f"<b>SENTINEL:</b> <code>{escape_html(verdict)}</code>",
            f"<b>Halt:</b> <code>{escape_html(halt)}</code>",
        ]
        if ts:
            html_lines.append(f"<b>Last scan:</b> <code>{escape_html(ts)}</code>")
        rsi = snap.get("rsi")
        if rsi is not None and rsi != "":
            try:
                rsi_s = f"{float(rsi):.2f}"
            except (TypeError, ValueError):
                rsi_s = str(rsi)
            ta = str(snap.get("ta_verdict") or "")
            plain_lines.append(f"RSI  {rsi_s}  TA  {ta or '—'}")
            html_lines.append(
                f"<b>RSI:</b> <code>{escape_html(rsi_s)}</code>  "
                f"<b>TA:</b> <code>{escape_html(ta or '—')}</code>"
            )
        setup_score = snap.get("setup_score")
        if setup_score is not None and setup_score != "":
            try:
                setup_s = f"{float(setup_score):.1f}"
            except (TypeError, ValueError):
                setup_s = str(setup_score)
            thresh = snap.get("setup_threshold")
            thresh_s = ""
            try:
                if thresh is not None and thresh != "":
                    thresh_s = f"/{float(thresh):.0f}"
            except (TypeError, ValueError):
                thresh_s = ""
            mtf = str(snap.get("mtf_align") or "")
            plain_lines.append(f"Setup  {setup_s}{thresh_s}  MTF  {mtf or '—'}")
            html_lines.append(
                f"<b>Setup:</b> <code>{escape_html(setup_s)}{escape_html(thresh_s)}</code>  "
                f"<b>MTF:</b> <code>{escape_html(mtf or '—')}</code>"
            )
        headlines = [str(h) for h in (snap.get("headlines") or []) if str(h).strip()]
        if headlines:
            html_lines.append("")
            html_lines.append("<b>Latest news scan</b>")
            for idx, headline in enumerate(headlines[:6], start=1):
                html_lines.append(f"{idx}. {escape_html(headline)}")
        if thesis:
            html_lines.append("")
            html_lines.append(f"<b>Thesis:</b> {escape_html(thesis)}")
        return CommandResult(
            "status",
            True,
            "\n".join(plain_lines),
            "\n".join(html_lines),
            [],
        )

    def _cmd_menu(self) -> CommandResult:
        """Headless terminal home. Same backends as /pnl and /status — never dummy tape."""
        mem = self.memory if self.memory is not None else BoardMemory()
        session = mem.session_pnl()
        snap = mem.latest_snapshot()
        daily = mem.daily_state()
        scored = bool(snap.get("scored"))
        sentiment = snap.get("sentiment")
        conviction = snap.get("conviction")
        label = str(snap.get("sentiment_label") or "") or sentiment_label(
            sentiment, scored=scored and sentiment is not None
        )
        if scored and sentiment is not None:
            try:
                sent_plain = f"{label} {float(sentiment):.2f}"
            except (TypeError, ValueError):
                sent_plain = label
        else:
            sent_plain = "NO SCAN YET"
        if scored and conviction is not None:
            conv_plain = f"{int(conviction)}/100"
        elif scored:
            conv_plain = "0/100"
        else:
            conv_plain = "NO SCAN YET"
        desk = str(snap.get("desk") or "") or "STAND_DOWN"
        halt = str(daily.get("halt") or snap.get("daily_halt") or "") or "none"
        realized = float(session.get("realized_pnl_usdt") or 0.0)
        wins = int(session.get("wins") or 0)
        losses = int(session.get("losses") or 0)
        left = int(session.get("trades_left") or 0)
        day = str(session.get("date") or "")
        sign = "+" if realized >= 0 else ""
        open_n = 0
        if self.bitget is not None:
            try:
                book = self.desk.snapshot(self.bitget)
            except Exception:
                book = []
            open_n = len(book or [])
        plain = (
            f"HEADLESS TERMINAL  UTC {day}\n"
            f"Desk  {desk}\n"
            f"Market Sentiment  {sent_plain}\n"
            f"AI Conviction  {conv_plain}\n"
            f"Today's Realized PnL  {sign}{realized:.2f} USDT\n"
            f"Win / Loss  {wins}W / {losses}L\n"
            f"Trades left: {left}\n"
            f"Open book  {open_n}\n"
            f"Halt  {halt}"
        )
        html = "\n".join(
            [
                "<b>CHRONOS-NEXUS</b>",
                "🖥 <b>HEADLESS TERMINAL</b>",
                f"<i>UTC {escape_html(day)} · live ledger + ORACLE snapshot</i>",
                "",
                f"<b>Desk:</b> <code>{escape_html(desk)}</code>",
                f"<b>Market Sentiment:</b> <code>{escape_html(sent_plain)}</code>",
                f"<b>AI Conviction:</b> <code>{escape_html(conv_plain)}</code>",
                f"<b>Today's Realized PnL:</b> <b>{sign}{realized:.2f} USDT</b>",
                f"<b>Win / Loss:</b> <code>{wins}W / {losses}L</code>",
                f"<b>Trades left:</b> <code>{left}</code>",
                f"<b>Open book:</b> <code>{open_n}</code> names",
                f"<b>Halt:</b> <code>{escape_html(halt)}</code>",
                "",
                "<i>Buttons call the same live functions as /status · /pnl · /positions · /closeall.</i>",
            ]
        )
        return CommandResult("menu", True, plain, html, [])

    def _cmd_close(self, arg: str, *, source: str) -> CommandResult:
        if self.bitget is None:
            msg = "Bitget Demo rail unbound — cannot close."
            return CommandResult("close", False, msg, msg, [])
        if not arg:
            msg = "Usage: /close SYMBOL"
            return CommandResult("close", False, msg, "Usage: <code>/close SYMBOL</code>", [])
        book = self.desk.snapshot(self.bitget)
        match = _match_symbol(arg, book)
        if match is None:
            msg = f"No open position matching {arg}."
            return CommandResult(
                "close",
                False,
                msg,
                f"No open position matching <code>{arg}</code>.",
                [],
            )
        tag = f"MANUAL /close ({source})"
        result = self.bitget.close_market(
            str(match.get("symbol")),
            fraction=1.0,
            reason=tag,
            side=str(match.get("side") or ""),
        )
        result["kind"] = "MANUAL"
        result["reason"] = tag
        if result.get("ok") and str(result.get("status") or "") == "DUST":
            msg = f"DUST {match.get('symbol')} — below 1 USDT minimum, not sent"
            return CommandResult("close", True, msg, f"<b>{msg}</b>", [result])
        if result.get("ok"):
            self.desk.drop(str(match.get("symbol")), str(match.get("side") or ""))
            if self.on_close:
                self.on_close(result)
            msg = (
                f"CLOSED {match.get('symbol')}  "
                f"{float(result.get('pnl_pct') or 0.0):+.2f}%  "
                f"{float(result.get('pnl_usdt') or 0.0):+.2f} USDT"
            )
            return CommandResult("close", True, msg, f"<b>{msg}</b>", [result])
        err = str(result.get("error") or result.get("status") or "close failed")
        msg = f"CLOSE FAILED {match.get('symbol')} — {err}"
        return CommandResult(
            "close",
            False,
            msg,
            f"<b>CLOSE FAILED</b> <code>{match.get('symbol')}</code>\n{err}",
            [result],
        )

    def _cmd_closeall(self, *, source: str) -> CommandResult:
        if self.bitget is None:
            msg = "Bitget Demo rail unbound — cannot close."
            return CommandResult("closeall", False, msg, msg, [])
        tag = f"MANUAL /closeall ({source})"
        results = self.bitget.close_all(reason=tag)
        if not results:
            msg = "FLAT — nothing to close."
            return CommandResult("closeall", True, msg, msg, [])
        lines: list[str] = []
        html_lines: list[str] = []
        closes: list[dict[str, Any]] = []
        for result in results:
            result["kind"] = "MANUAL"
            result["reason"] = result.get("reason") or tag
            if result.get("ok") and str(result.get("status") or "") == "DUST":
                line = (
                    f"DUST {result.get('symbol')} — below 1 USDT minimum, not sent"
                )
                lines.append(line)
                html_lines.append(f"<b>{line}</b>")
                closes.append(result)
                continue
            status = str(result.get("status") or "")
            if result.get("ok") and status == "CLOSED":
                self.desk.drop(str(result.get("symbol") or ""), str(result.get("side") or ""))
                if self.on_close:
                    self.on_close(result)
                line = (
                    f"CLOSED {result.get('symbol')}  "
                    f"{_fmt_signed(float(result.get('pnl_pct') or 0.0))}%  "
                    f"{_fmt_signed(float(result.get('pnl_usdt') or 0.0))} USDT"
                )
                lines.append(line)
                html_lines.append(f"<b>{line}</b>")
            elif result.get("ok") and status == "PARTIAL":
                line = (
                    f"PARTIAL {result.get('symbol')} — still open "
                    f"qty={result.get('remaining')}"
                )
                lines.append(line)
                html_lines.append(f"<b>{line}</b>")
            else:
                err = str(result.get("error") or result.get("status") or "close failed")
                line = f"CLOSE FAILED {result.get('symbol')} — {err}"
                lines.append(line)
                html_lines.append(
                    f"<b>CLOSE FAILED</b> <code>{result.get('symbol')}</code>\n{err}"
                )
            closes.append(result)
        return CommandResult("closeall", True, "\n".join(lines), "\n".join(html_lines), closes)


class TelegramCommandLoop:
    def __init__(
        self,
        notifier: TelegramNotifier,
        commands: CommandDesk,
    ) -> None:
        self.notifier = notifier
        self.commands = commands
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._http = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)

    def start(self) -> None:
        if not self.notifier.enabled:
            print("[TELEGRAM] command loop disarmed — no token/chat", flush=True)
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="chronos-telegram-commands"
        )
        self._thread.start()
        menu = register_bot_menu(self.notifier.token)
        menu_bit = "menu registered" if menu.get("ok") else "menu skipped"
        print(
            "[TELEGRAM] command loop armed  "
            "/menu /positions /close /closeall /price /balance /pnl /status  "
            f"+ Spot chatbox  ({menu_bit})",
            flush=True,
        )

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        offset = _load_offset()
        while not self._stop.is_set():
            try:
                updates, offset = self._poll(offset)
                if offset:
                    _save_offset(offset)
                for update in updates:
                    try:
                        self._handle(update)
                    except Exception as exc:
                        print(
                            f"[TELEGRAM] update skipped — {_telegram_fault(exc)}",
                            flush=True,
                        )
            except Exception as exc:
                print(f"[TELEGRAM] command loop retry — {_telegram_fault(exc)}", flush=True)
                time.sleep(2.0)

    def _poll(self, offset: int) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "timeout": 20,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset:
            params["offset"] = offset
        # Connect fails fast. Read stays above the 20s long-poll.
        try:
            resp = self._http.get(
                TELEGRAM_UPDATES.format(token=self.notifier.token),
                params=params,
                timeout=(5, 25),
            )
        except requests.RequestException as exc:
            raise RuntimeError(_telegram_fault(exc)) from exc
        data = resp.json() if resp.content else {}
        if not data.get("ok"):
            raise RuntimeError(str(data.get("description") or "getUpdates failed"))
        updates = list(data.get("result") or [])
        nxt = offset
        for item in updates:
            try:
                nxt = max(nxt, int(item.get("update_id") or 0) + 1)
            except (TypeError, ValueError):
                continue
        return updates, nxt

    def _handle(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else None
        if callback:
            self._handle_callback(callback)
            return
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "")
        if chat_id != str(self.notifier.chat_id):
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        intent = parse_spot_intent(text)
        if intent is not None:
            self._preview_spot(intent, chat_id)
            return
        result = self.commands.handle(text, source="telegram")
        if not result.cmd:
            return
        if result.cmd == "menu":
            self.notifier.send_html_sync(
                result.html,
                reply_markup=desk_keyboard(),
                chat_id=chat_id,
            )
            return
        if result.html:
            self.notifier.reply(result.html)

    def _preview_spot(self, intent: Any, chat_id: str) -> None:
        bitget = self.commands.bitget
        if bitget is None:
            self.notifier.reply("<b>Bitget Demo rail unbound</b> — cannot preview a Spot ticket.")
            return
        try:
            quote = bitget.fetch_spot_quote(intent.symbol, side=intent.side)
        except Exception as exc:
            self.notifier.reply(f"<b>Spot ticker failed</b>\n{exc}")
            return
        symbol = str(quote.get("symbol") or intent.symbol or "")
        last = _safe_px(quote.get("peg"), quote.get("last"), quote.get("ask"), quote.get("bid"))
        if not quote.get("ok") or last <= 0 or not symbol:
            err = quote.get("error") or f"{intent.symbol} is not a Bitget Spot listing."
            self.notifier.reply(
                "<b>SPOT PREVIEW REJECTED</b>\n"
                f"{err}\n"
                "Manual chat trades are <b>Spot only</b> — futures/perps are reserved for the AI cycle."
            )
            return
        try:
            balance = float(bitget.fetch_spot_usdt_free())
        except Exception:
            balance = 0.0
        qty = intent.quote_usdt / last
        ticket = issue_ticket(
            chat_id=chat_id,
            symbol=symbol,
            side=intent.side,
            quote_usdt=intent.quote_usdt,
            last=last,
            qty=qty,
            balance_usdt=balance,
        )
        sent = self.notifier.send_html_sync(
            preview_html(ticket),
            reply_markup=confirm_keyboard(ticket.id),
            chat_id=chat_id,
        )
        mid = sent.get("message_id")
        if mid:
            bind_message(ticket.id, int(mid))

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        cq_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "")
        message_id = message.get("message_id")
        if chat_id != str(self.notifier.chat_id):
            self.notifier.answer_callback(cq_id, "Ignored.")
            return
        desk_action = parse_desk_callback(data)
        if desk_action:
            self._handle_desk_callback(
                cq_id,
                desk_action,
                chat_id,
                int(message_id or 0),
            )
            return
        parsed = parse_callback(data)
        if parsed is None:
            self.notifier.answer_callback(cq_id, "Unknown button.")
            return
        action, ticket_id = parsed
        ticket = consume_ticket(ticket_id, chat_id=chat_id, action=action)
        if ticket is None:
            self.notifier.answer_callback(cq_id, "Expired or already used.")
            if message_id:
                self.notifier.edit_html(
                    int(message_id),
                    "<b>CHRONOS-NEXUS</b>\nThis Spot preview expired or was already used.",
                    chat_id=chat_id,
                )
            return
        if action == CALLBACK_CANCEL:
            self.notifier.answer_callback(cq_id, "Cancelled.")
            self.notifier.edit_html(
                int(message_id or ticket.message_id or 0),
                cancelled_html(ticket),
                chat_id=chat_id,
            )
            return
        self.notifier.answer_callback(cq_id, "Submitting Spot order…")
        self._execute_spot(ticket, int(message_id or ticket.message_id or 0), chat_id)

    def _handle_desk_callback(
        self,
        cq_id: str,
        action: str,
        chat_id: str,
        message_id: int,
    ) -> None:
        """Edit-in-place. Every view is CommandDesk.handle of the live slash command."""
        labels = {
            "status": "Live market status",
            "pnl": "My real PnL",
            "positions": "Open positions",
            "closeall_ask": "Force close all",
            "closeall_run": "Flattening Demo book",
            "menu": "Dashboard",
        }
        self.notifier.answer_callback(cq_id, labels.get(action, "Loading live desk…"))
        html, markup = self._desk_view(action)
        if not message_id:
            self.notifier.send_html_sync(html, reply_markup=markup, chat_id=chat_id)
            return
        self.notifier.edit_html(message_id, html, reply_markup=markup, chat_id=chat_id)

    def _desk_view(self, action: str) -> tuple[str, dict[str, Any]]:
        if action == "closeall_ask":
            return self._closeall_prompt()
        if action == "closeall_run":
            result = self.commands.handle("/closeall", source="telegram")
            body = result.html or result.plain or "FLAT — nothing to close."
            return body, desk_keyboard()
        cmd = {"status": "/status", "pnl": "/pnl", "positions": "/positions", "menu": "/menu"}.get(
            action, "/menu"
        )
        result = self.commands.handle(cmd, source="telegram")
        return result.html or result.plain or "No live data.", desk_keyboard()

    def _closeall_prompt(self) -> tuple[str, dict[str, Any]]:
        bitget = self.commands.bitget
        if bitget is None:
            return (
                "<b>CHRONOS-NEXUS</b>\n🛑 <b>FORCE CLOSE ALL</b>\n\n"
                "Bitget Demo rail unbound — cannot close.",
                desk_keyboard(),
            )
        try:
            book = self.commands.desk.snapshot(bitget)
        except Exception as exc:
            return (
                f"<b>CHRONOS-NEXUS</b>\n🛑 <b>FORCE CLOSE ALL</b>\n\n"
                f"Live book unavailable: {escape_html(str(exc))}",
                desk_keyboard(),
            )
        if not book:
            return (
                "<b>CHRONOS-NEXUS</b>\n🛑 <b>FORCE CLOSE ALL</b>\n\n"
                "FLAT — nothing to close.",
                desk_keyboard(),
            )
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            "🛑 <b>FORCE CLOSE ALL</b>",
            "<i>Live Demo book — same path as /closeall. Confirm to flatten.</i>",
            "",
            f"<b>Open names:</b> <code>{len(book)}</code>",
            "",
        ]
        for pos in book:
            pnl_usdt = float(pos.get("pnl_usdt") or 0.0)
            usd = "+" if pnl_usdt >= 0 else ""
            symbol = str(pos.get("symbol") or "")
            side = str(pos.get("side") or "").upper()
            lines.append(
                f"• <code>{escape_html(symbol)}</code> {escape_html(side)}  "
                f"{usd}{pnl_usdt:.2f} USDT"
            )
        return "\n".join(lines), flatten_keyboard()

    def _execute_spot(self, ticket: Any, message_id: int, chat_id: str) -> None:
        bitget = self.commands.bitget
        edit = self.notifier.edit_html
        if bitget is None:
            edit(message_id, "<b>Bitget unbound</b> — Spot order not sent.", chat_id=chat_id)
            return
        edit(message_id, timeline_html(ticket, "processing"), chat_id=chat_id)
        edit(message_id, timeline_html(ticket, "balance"), chat_id=chat_id)
        try:
            free = float(bitget.fetch_spot_usdt_free())
        except Exception as exc:
            edit(
                message_id,
                timeline_html(ticket, "balance", f"Balance check failed: {exc}"),
                chat_id=chat_id,
            )
            return
        if ticket.side == "buy" and free + 1e-9 < ticket.quote_usdt:
            edit(
                message_id,
                receipt_html(
                    ticket,
                    {
                        "ok": False,
                        "status": "INSUFFICIENT_MARGIN",
                        "error": f"Spot USDT free {free:.4f} < {ticket.quote_usdt:.2f}",
                        "symbol": ticket.symbol,
                    },
                ),
                chat_id=chat_id,
            )
            return
        edit(message_id, timeline_html(ticket, "submit"), chat_id=chat_id)
        try:
            order = bitget.execute_spot_market(ticket.symbol, ticket.side, ticket.quote_usdt)
        except Exception as exc:
            order = {"ok": False, "status": "ERROR", "error": str(exc)[:400], "symbol": ticket.symbol}
        edit(message_id, timeline_html(ticket, "done"), chat_id=chat_id)
        edit(message_id, receipt_html(ticket, order), chat_id=chat_id)


class TerminalCommandLoop:
    """Local stdin mirror of Telegram commands. Does not block the hourly cycle."""

    def __init__(self, commands: CommandDesk, printer: Callable[[str], None] | None = None) -> None:
        self.commands = commands
        self.printer = printer or (lambda msg: print(msg, flush=True))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stdin or not sys.stdin.isatty():
            print("[TERMINAL] command loop skipped — stdin is not a TTY", flush=True)
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="chronos-terminal-commands"
        )
        self._thread.start()
        print(
            "[TERMINAL] command loop armed  "
            "/positions /close /closeall /price /balance /pnl /status /help",
            flush=True,
        )

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                line = input("nexus> ")
            except EOFError:
                return
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"[TERMINAL] input fault: {exc}", flush=True)
                time.sleep(0.4)
                continue
            text = (line or "").strip()
            if not text:
                continue
            if parse_spot_intent(text):
                self.printer(
                    "Spot chatbox is Telegram-only. Send NVDA/USDT BUY $10 in Telegram to preview + confirm."
                )
                continue
            try:
                result = self.commands.handle(text, source="terminal")
            except Exception as exc:
                self.printer(f"[TERMINAL] command failed: {exc}")
                continue
            if not result.cmd:
                self.printer("Unknown input. Try /help.")
                continue
            if result.plain:
                self.printer(result.plain)


def desk_keyboard() -> dict[str, Any]:
    """Inline ChatOps pad. Callbacks map 1:1 onto /status /pnl /positions /closeall."""
    return {
        "inline_keyboard": [
            [
                {"text": "📡 Live Market Status", "callback_data": "nx:status"},
                {"text": "📈 My Real PnL", "callback_data": "nx:pnl"},
            ],
            [
                {"text": "📊 Open Positions", "callback_data": "nx:positions"},
                {"text": "🛑 Force Close All", "callback_data": "nx:closeall"},
            ],
            [{"text": "🖥 Dashboard", "callback_data": "nx:home"}],
        ]
    }


def flatten_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ CONFIRM FLATTEN", "callback_data": "nx:closeall_yes"},
                {"text": "❌ BACK", "callback_data": "nx:home"},
            ]
        ]
    }


def parse_desk_callback(data: str) -> str | None:
    """Return the desk action for nx:* buttons. None for Spot s1:/s0: tickets."""
    return _DESK_CALLBACKS.get((data or "").strip())


def register_bot_menu(token: str) -> dict[str, Any]:
    """Register the native Telegram Menu button command list. Fail-open."""
    raw = (token or "").strip()
    if not raw:
        return {"ok": False, "skipped": True}
    try:
        resp = requests.post(
            TELEGRAM_SET_COMMANDS.format(token=raw),
            json={"commands": BOT_MENU},
            timeout=12,
        )
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            return {"ok": False, "error": "setMyCommands returned a non-object"}
        if not data.get("ok"):
            print(
                f"[TELEGRAM ERROR] setMyCommands: {data.get('description') or data}",
                flush=True,
            )
            return {"ok": False, "error": str(data.get("description") or "setMyCommands failed")}
        return {"ok": True, "commands": [row["command"] for row in BOT_MENU]}
    except Exception as exc:
        print(f"[TELEGRAM ERROR] setMyCommands: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}


def _telegram_fault(exc: BaseException) -> str:
    """Short fault line. Never includes the bot token or the request URL."""
    text = str(exc or "")
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered or "connecttimeout" in lowered:
        return "connection to api.telegram.org timed out"
    if "max retries" in lowered or "failed to establish" in lowered:
        return "connection to api.telegram.org failed"
    text = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot…", text)
    return text[:180]


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _fmt_signed(value: float) -> str:
    """Two decimals for a normal print. Four when a tick would otherwise show as 0.00."""
    if abs(value) >= 0.01:
        return f"{value:+.2f}"
    return f"{value:+.4f}"


def _fmt_px(value: Any) -> str:
    parsed = _opt_float(value)
    if parsed is None or parsed <= 0:
        return "n/a"
    return f"{parsed:.8f}".rstrip("0").rstrip(".")


def _metric(value: Any) -> str:
    """Live ticker figure, or n/a when the venue did not send one."""
    px = _safe_px(value)
    if px <= 0:
        return "n/a"
    if px >= 100:
        return f"{px:,.2f}"
    text = f"{px:.8f}".rstrip("0").rstrip(".")
    return text or "n/a"


def _safe_px(*values: Any) -> float:
    """First finite positive number. Never raises on None."""
    for value in values:
        if value is None or value == "":
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed == parsed and parsed not in {float("inf"), float("-inf")} and parsed > 0:
            return parsed
    return 0.0


def _amt0(value: Any) -> float:
    """Finite wallet amount ≥ 0. None-safe."""
    if value is None or value == "":
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return 0.0
    return max(0.0, parsed)


def _wallet_coin(arg: str) -> str:
    """Asset id from /balance SYMBOL. Pairs collapse to the base (NVDA/USDT → NVDA)."""
    raw = (arg or "").strip()
    if not raw:
        return ""
    token = raw.split()[0].split("@")[0].upper().replace("USDT:USDT", "USDT")
    if token.endswith(":USDT"):
        token = token[: -len(":USDT")]
    if "/" in token:
        token = token.split("/", 1)[0]
    return token.strip()


def _match_symbol(query: str, book: list[dict[str, Any]]) -> dict[str, Any] | None:
    q = (query or "").strip()
    if not q:
        return None
    q_up = q.upper()
    q_root = ticker_root(q)
    for pos in book:
        symbol = str(pos.get("symbol") or "")
        if symbol.upper() == q_up or ticker_root(symbol) == q_root:
            return pos
    return None


def _load_offset() -> int:
    if not OFFSET_PATH.exists():
        return 0
    try:
        payload = json.loads(OFFSET_PATH.read_text(encoding="utf-8"))
        return int(payload.get("offset") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
