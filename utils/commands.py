"""Shared command desk — Telegram and local terminal. Never opens a position."""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from core.config import PROJECT_ROOT
from core.positions import PositionDesk, ticker_root
from connectors.bitget_paper import BitgetPaperConnector
from utils.notifier import TelegramNotifier
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
HELP_PLAIN = (
    "CHRONOS-NEXUS COMMANDS\n"
    "AI desk opens are automated only. Manual chatbox is Bitget SPOT.\n\n"
    "/positions          live book + PnL\n"
    "/close SYMBOL       market-close one name\n"
    "/closeall           flatten the whole Demo book\n"
    "/price SYMBOL       live mainnet last / bid / ask\n"
    "/balance SYMBOL     wallet free + total for one asset\n"
    "NVDA/USDT BUY $10   Spot preview + confirm buttons\n"
    "/help               this list"
)
HELP_HTML = (
    "<b>CHRONOS-NEXUS COMMANDS</b>\n"
    "AI cycle is untouched. Manual chatbox is <b>Bitget Spot</b> only.\n\n"
    "<code>/positions</code> — live book + PnL\n"
    "<code>/close SYMBOL</code> — market-close one name\n"
    "<code>/closeall</code> — flatten the whole Demo book\n"
    "<code>/price SYMBOL</code> — live Bitget mainnet last / bid / ask\n"
    "<code>/balance SYMBOL</code> — wallet free + total for one asset\n"
    "<code>NVDA/USDT BUY $10</code> — Spot preview, then Confirm / Cancel\n"
    "<code>/help</code> — this list"
)


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
    ) -> None:
        self.bitget = bitget
        self.desk = desk
        self.on_close = on_close

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
            pnl_pct = float(pos.get("pnl_pct") or 0.0)
            pnl_usdt = float(pos.get("pnl_usdt") or 0.0)
            emoji = "GREEN" if pnl_pct >= 0 else "RED"
            sign = "+" if pnl_pct >= 0 else ""
            usd = "+" if pnl_usdt >= 0 else ""
            symbol = pos.get("symbol")
            side = str(pos.get("side") or "").upper()
            plain_lines.append(
                f"{emoji} {symbol}  {side}  qty={pos.get('contracts')}  "
                f"entry={pos.get('entry_price')}  mark={pos.get('mark_price')}  "
                f"PnL {sign}{pnl_pct:.2f}%  {usd}{pnl_usdt:.2f} USDT"
            )
            html_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            html_lines.append(
                f"{html_emoji} <code>{symbol}</code>  {side}\n"
                f"qty <code>{pos.get('contracts')}</code>  "
                f"entry <code>{pos.get('entry_price')}</code>  "
                f"mark <code>{pos.get('mark_price')}</code>\n"
                f"PnL <b>{sign}{pnl_pct:.2f}%</b>  "
                f"<b>{usd}{pnl_usdt:.2f} USDT</b>"
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
        source = str(quote.get("source") or "bitget.mainnet")
        plain = (
            f"LIVE PRICE  {symbol}\n"
            f"last {last}\n"
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
        free = _amt0(payload.get("free"))
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
        plain = f"{label}\nfree  {free:.8f}\ntotal {total:.8f}"
        html = "\n".join(
            [
                "<b>CHRONOS-NEXUS</b>",
                f"<code>{label}</code>",
                f"free <code>{free:.8f}</code>",
                f"total <code>{total:.8f}</code>",
            ]
        )
        return CommandResult("balance", True, plain, html, [])

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
            if result.get("ok"):
                self.desk.drop(str(result.get("symbol") or ""), str(result.get("side") or ""))
                if self.on_close:
                    self.on_close(result)
                line = (
                    f"CLOSED {result.get('symbol')}  "
                    f"{float(result.get('pnl_pct') or 0.0):+.2f}%  "
                    f"{float(result.get('pnl_usdt') or 0.0):+.2f} USDT"
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

    def start(self) -> None:
        if not self.notifier.enabled:
            print("[TELEGRAM] command loop disarmed — no token/chat", flush=True)
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="chronos-telegram-commands"
        )
        self._thread.start()
        print(
            "[TELEGRAM] command loop armed  /positions /close /closeall  + Spot chatbox",
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
                    self._handle(update)
            except Exception as exc:
                print(f"[TELEGRAM ERROR] command loop: {exc}", flush=True)
                time.sleep(3.0)

    def _poll(self, offset: int) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "timeout": 20,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset:
            params["offset"] = offset
        resp = requests.get(
            TELEGRAM_UPDATES.format(token=self.notifier.token),
            params=params,
            timeout=30,
        )
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
            "[TERMINAL] command loop armed  /positions /close SYMBOL /closeall /help",
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
