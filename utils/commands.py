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

OFFSET_PATH = PROJECT_ROOT / "data" / "telegram_offset.json"
TELEGRAM_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
HELP_PLAIN = (
    "CHRONOS-NEXUS COMMANDS\n"
    "Manual open is disabled. Close override is yours.\n\n"
    "/positions          live book + PnL\n"
    "/close SYMBOL       market-close one name\n"
    "/closeall           flatten the whole Demo book\n"
    "/help               this list"
)
HELP_HTML = (
    "<b>CHRONOS-NEXUS COMMANDS</b>\n"
    "Manual open is disabled. Close override is yours.\n\n"
    "<code>/positions</code> — live book + PnL\n"
    "<code>/close SYMBOL</code> — market-close one name\n"
    "<code>/closeall</code> — flatten the whole Demo book\n"
    "<code>/help</code> — this list"
)


@dataclass
class CommandResult:
    cmd: str
    ok: bool
    plain: str
    html: str
    closes: list[dict[str, Any]] = field(default_factory=list)


def parse_command(text: str) -> tuple[str, str]:
    """Return (cmd, arg). cmd is lowercase without a leading slash. Empty cmd if not a command."""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("/"):
        raw = raw[1:]
    parts = raw.split()
    if not parts:
        return "", ""
    cmd = parts[0].split("@")[0].lower()
    aliases = {
        "pos": "positions",
        "p": "positions",
        "flatten": "closeall",
        "flat": "closeall",
        "start": "help",
        "?": "help",
    }
    cmd = aliases.get(cmd, cmd)
    arg = " ".join(parts[1:]).strip()
    return cmd, arg


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
        if cmd in {"buy", "sell", "open", "long", "short"}:
            msg = "REFUSED — manual open is disabled. Use /close or /closeall."
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
        print("[TELEGRAM] command loop armed  /positions /close /closeall", flush=True)

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
        params: dict[str, Any] = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
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
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "")
        if chat_id != str(self.notifier.chat_id):
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        result = self.commands.handle(text, source="telegram")
        if not result.cmd:
            return
        if result.html:
            self.notifier.reply(result.html)


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
