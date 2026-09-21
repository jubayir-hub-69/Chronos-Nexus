#!/usr/bin/env python3
"""Chronos-Nexus — autonomous Event-Driven Agent.

Bitget AI Base Camp Hackathon S2 · Agentic Trading · Event-Driven Agent
News-driven directional trading on Bitget Demo. Paper only.
Lifecycle: wire → debate → attest → execute.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

if sys.platform == "win32":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from agents.analyst import (
    AnalystAgent,
    STAND_DOWN_SYMBOL,
    detect_stay_away,
    is_idle_brief,
    is_none_symbol,
    session_clock,
    snap_to_universe,
)
from agents.executive import ExecutiveAgent
from agents.risk_manager import RiskManagerAgent
from connectors.arbitrum import ArbitrumSepolia
from connectors.bitget_paper import BitgetPaperConnector
from core.config import Settings, load_settings
from core.llm import QwenCortex
from core.memory import (
    DAILY_MAX_ENTRIES,
    DAILY_RISK_PCT,
    DAILY_WIN_STREAK,
    BoardMemory,
    build_engine_snapshot,
)
from core.positions import (
    PositionDesk,
    is_occupied,
    occupied_symbols,
    strip_occupied_universe,
)
from core.schemas import AnalystBrief, BoardDecision, RiskReport
from core.ta import RSI_PERIOD, SCALE_OUT_PCT, bbo_peg_price, candle_veto, confluence_veto, rsi_zone
from utils.commands import CommandDesk, TelegramCommandLoop, TerminalCommandLoop
from utils.notifier import TelegramNotifier, send_startup_message

LIVE_TRADING_ENABLED = False
VERSION = "1.2.0-pro"
HACKATHON = "Bitget AI Base Camp Hackathon S2"
CYCLE_INTERVAL_SEC = 3600
CYCLE_ERROR_BACKOFF_SEC = 300

BANNER = r"""
 ██████╗██╗  ██╗██████╗  ██████╗ ███╗   ██╗ ██████╗ ███████╗
██╔════╝██║  ██║██╔══██╗██╔═══██╗████╗  ██║██╔═══██╗██╔════╝
██║     ███████║██████╔╝██║   ██║██╔██╗ ██║██║   ██║███████╗
██║     ██╔══██║██╔══██╗██║   ██║██║╚██╗██║██║   ██║╚════██║
╚██████╗██║  ██║██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝███████║
 ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝
███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
"""

console = Console(highlight=False, emoji=False)


def render_banner() -> None:
    console.clear()
    body = Group(
        Align.center(Text(BANNER, style="bold green1")),
        Align.center(Text("WALL STREET SLEEPS.  THE NEXUS DOES NOT.", style="bold cyan")),
        Align.center(
            Text(
                "Hackathon S2  ·  Agentic Trading  ·  Event-Driven Agent  ·  PAPER ONLY",
                style="dim cyan",
            )
        ),
    )
    console.print(Panel(body, border_style="green", box=box.DOUBLE, padding=(0, 1)))
    console.print()
    lock = Table.grid(expand=True)
    lock.add_column(justify="center")
    lock.add_row(Text("LIVE TRADING LOCKED  ·  DEMO NETWORK ONLY  ·  NO REAL FUNDS", style="bold red"))
    console.print(Panel(lock, border_style="red", box=box.HEAVY, padding=(0, 1)))
    console.print()


def phase(title: str) -> None:
    console.print(Rule(f"[bold green]{title}[/]", style="green"))


def kv_panel(title: str, rows: list[tuple[str, str]], border: str = "cyan") -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column(style="bold white")
    for key, value in rows:
        grid.add_row(key, value)
    return Panel(grid, title=f"[bold]{title}[/]", border_style=border, box=box.SQUARE)


def status_dot(ok: bool, label_ok: str = "ONLINE", label_bad: str = "FAULT") -> Text:
    if ok:
        return Text(f"● {label_ok}", style="bold green")
    return Text(f"● {label_bad}", style="bold red")


def clip(text: str, n: int = 420) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def listed_demo_symbol(
    bitget: BitgetPaperConnector | None,
    chosen: str,
    universe: list[str],
) -> str:
    """Snap ORACLE's pick onto a symbol actually listed on Bitget Demo."""
    if is_none_symbol(chosen):
        return STAND_DOWN_SYMBOL
    snapped = snap_to_universe(chosen, universe)
    if is_none_symbol(snapped):
        return STAND_DOWN_SYMBOL
    markets = {}
    if bitget is not None:
        markets = getattr(bitget.exchange, "markets", None) or {}
    aliases = _symbol_aliases(snapped)
    for cand in aliases:
        if not markets or cand in markets:
            if not markets:
                return snapped
            return cand
    for member in universe:
        for cand in _symbol_aliases(member):
            if cand in markets and _ticker_root(cand) == _ticker_root(snapped):
                return cand
    return snapped


def _ticker_root(symbol: str) -> str:
    base = (symbol or "").split(":")[0].split("/")[0].upper()
    if base.startswith("R") and len(base) > 2:
        base = base[1:]
    return "GOOG" if base == "GOOGL" else base


def _symbol_aliases(symbol: str) -> list[str]:
    root = _ticker_root(symbol)
    alts = [symbol, root, f"{root}/USDT", f"{root}/USDT:USDT"]
    if root == "GOOG":
        alts += ["GOOGL", "GOOGL/USDT", "GOOGL/USDT:USDT"]
    out: list[str] = []
    for item in alts:
        if item and item not in out:
            out.append(item)
    return out


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dispatch_alerts(
    notifier: TelegramNotifier,
    brief: AnalystBrief,
    risk: RiskReport,
    decision: BoardDecision,
    order: dict[str, Any],
    attestation: Any,
    session: str = "",
) -> None:
    """Fire-and-forget Telegram. Never raises into the CIC."""
    try:
        status = str(order.get("status") or "")
        symbol = str(order.get("symbol") or decision.symbol or brief.primary_symbol or "")
        spread = ""
        if risk.spread_pct is not None:
            spread = f"{risk.spread_pct:.4f}%"
        rsi_s = f"{float(risk.rsi):.2f}" if risk.rsi is not None else ""
        if status == "POSITION_ALREADY_OPEN":
            notifier.alert_position_open(
                symbol=symbol,
                side=str(order.get("side") or decision.side or ""),
                detail=str(order.get("error") or "Position already open"),
            )
            return
        if is_idle_brief(brief) or (
            decision.action == "STAND_DOWN" and risk.verdict != "VETO" and status not in {"VETOED"}
        ):
            notifier.alert_stand_down(
                reason=brief.rationale or decision.reasoning or "No actionable news on this scan.",
                symbol=symbol or STAND_DOWN_SYMBOL,
                side=str(brief.side or "none"),
                conviction=int(brief.conviction or 0),
                model=brief.model or decision.model,
                session=session,
            )
            return
        if risk.verdict == "VETO" or decision.consensus == "VETOED" or status == "VETOED":
            notifier.alert_veto(
                reason=risk.rationale or decision.reasoning or "SENTINEL veto",
                symbol=symbol,
                flags=list(risk.black_swan_flags or []),
                model=risk.model or decision.model,
                status=status or decision.consensus,
                spread=spread,
                rsi=rsi_s,
            )
            return
        if bool(order.get("ok")):
            explorer = getattr(attestation, "explorer_url", None) if attestation is not None else None
            passed = ", ".join(brief.affected_tickers or [])
            notifier.alert_execute(
                symbol=str(order.get("symbol") or decision.symbol),
                side=str(order.get("side") or decision.side),
                amount=order.get("amount") if order.get("amount") is not None else decision.amount,
                sl_price=order.get("sl_price"),
                tp_price=order.get("tp_price"),
                order_id=order.get("order_id"),
                explorer_url=explorer,
                model=decision.model,
                why=brief.selection_reason or brief.rationale,
                thesis=brief.thesis,
                news_good=brief.news_good,
                passed_over=passed,
                session=session,
            )
            return
        if order.get("error") or status in {"ERROR", "INSUFFICIENT_MARGIN", "NO_LIVE_PRICE"}:
            notifier.alert_veto(
                reason=str(order.get("error") or status or "execution skipped"),
                symbol=symbol,
                flags=list(risk.black_swan_flags or []) + [status] if status else list(risk.black_swan_flags or []),
                model=decision.model,
                status=status,
                spread=spread,
                rsi=rsi_s,
            )
    except Exception:
        return


def _persist_engine_snapshot(
    memory: BoardMemory,
    brief: AnalystBrief,
    risk: RiskReport,
    decision: BoardDecision,
    order: dict[str, Any] | None = None,
) -> None:
    """Write the live ORACLE/SENTINEL/CHAIRMAN state so /status is never a dummy."""
    try:
        memory.record_snapshot(
            build_engine_snapshot(
                news_context=list(brief.wire_headlines or []),
                brief=brief.model_dump(),
                risk=risk.model_dump(),
                decision=decision.model_dump(),
                result=order or {},
                daily=memory.daily_state(),
            )
        )
    except Exception:
        return


def _emit_desk_actions(notifier: TelegramNotifier, actions: list[dict[str, Any]]) -> None:
    for action in actions or []:
        if not action:
            continue
        try:
            notifier.alert_pnl(
                symbol=str(action.get("symbol") or ""),
                pnl_pct=float(action.get("pnl_pct") or 0.0),
                pnl_usdt=float(action.get("pnl_usdt") or 0.0),
                kind=str(action.get("kind") or action.get("status") or "CLOSE"),
                reason=str(action.get("reason") or action.get("status") or ""),
                side=str(action.get("side") or action.get("position_side") or ""),
                entry=action.get("entry_price"),
                mark=action.get("mark_price") or action.get("price"),
            )
        except Exception:
            continue


def _run_trading_cycle(
    settings: Settings,
    cortex: QwenCortex,
    memory: BoardMemory,
    bitget: BitgetPaperConnector | None,
    arb: ArbitrumSepolia | None,
    notifier: TelegramNotifier,
    desk: PositionDesk,
) -> None:
    t0 = time.perf_counter()
    phase("PHASE 3  ·  LIVE MACRO INGEST")
    analyst = AnalystAgent(cortex, memory=memory)
    sentinel = RiskManagerAgent(cortex)
    chairman = ExecutiveAgent(cortex, memory=memory)
    mem_n = len(memory.recent())
    console.print(
        kv_panel(
            "BOARD MEMORY",
            [
                ("path", "data/history.json"),
                ("cycles", str(mem_n)),
                ("window", "last 5 trades"),
            ],
            border="yellow",
        )
    )
    console.print()
    clock = session_clock()
    daily = memory.daily_state()
    equity_usdt: float | None = None
    try:
        if bitget is not None:
            eq = bitget.fetch_account_equity()
            equity_usdt = eq.get("equity_usdt")
    except Exception as exc:
        print(f"[DAILY] fetch_balance equity failed: {exc}", flush=True)
        equity_usdt = None
    budget = round(float(equity_usdt) * DAILY_RISK_PCT, 4) if equity_usdt else None
    console.print(
        kv_panel(
            "DAILY RISK  ·  UTC",
            [
                ("day", str(daily.get("date") or "")),
                ("entries", f"{int(daily.get('entries') or 0)}/{DAILY_MAX_ENTRIES}"),
                ("wins", f"{int(daily.get('wins') or 0)}/{DAILY_WIN_STREAK}"),
                ("sl_hits", str(int(daily.get("sl_hits") or 0))),
                ("equity", "n/a" if equity_usdt is None else f"{equity_usdt:.2f} USDT"),
                ("budget 6%", "n/a" if budget is None else f"{budget:.2f} USDT"),
                ("deployed", f"{float(daily.get('deployed_usdt') or 0.0):.2f} USDT"),
                ("halt", str(daily.get("halt") or daily.get("halt_reason") or "none")),
                ("partial TP", f"+{SCALE_OUT_PCT:.0%} then trail"),
            ],
            border="red" if daily.get("halt") else "green",
        )
    )
    console.print()
    try:
        universe = bitget.fetch_equity_universe() if bitget is not None else []
    except Exception:
        universe = list(getattr(bitget, "universe", None) or []) if bitget is not None else []

    try:
        triggers = analyst.ingest_weekend_wire()
    except Exception as exc:
        from core.schemas import WeekendTrigger

        triggers = [
            WeekendTrigger(
                id="WIRE-FAIL",
                category="outage",
                headline="Live financial RSS unavailable",
                detail=str(exc)[:240],
                source="chronos-nexus",
            )
        ]
        notifier.alert_api_error(
            error=str(exc)[:400],
            where="RSS ingest",
            action="STAND_DOWN",
            session=clock["line"],
        )
    wire = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=True)
    wire.add_column("ID", style="bold green", no_wrap=True)
    wire.add_column("SRC", style="yellow", no_wrap=True)
    wire.add_column("CAT", style="magenta")
    wire.add_column("HEADLINE")
    wire.add_column("rTOKEN MAP", style="cyan")
    for trig in triggers:
        wire.add_row(
            trig.id,
            trig.source or "—",
            trig.category,
            trig.headline,
            ", ".join(trig.rtoken_map) or "—",
        )
    console.print(wire)
    console.print()
    phase("PHASE 3.4  ·  ANTI-STACK  ·  OPEN BOOK")
    book: list[dict[str, Any]] = []
    try:
        book = desk.snapshot(bitget)
    except Exception as exc:
        console.print(f"[bold yellow]Position book unavailable:[/] {exc}")
        book = []
    occupied = occupied_symbols(book)
    entry_universe = strip_occupied_universe(universe, book)
    sample = "  ".join(entry_universe[:10]) + ("  …" if len(entry_universe) > 10 else "")
    console.print(
        kv_panel(
            "LIVE DEMO UNIVERSE",
            [
                ("session", clock["line"]),
                ("listed", str(len(universe))),
                ("occupied", ", ".join(occupied) or "none"),
                ("entry_book", str(len(entry_universe))),
                ("source", str(getattr(bitget, "universe_source", None) or "unbound")),
                ("sample", sample or "(empty — ORACLE must emit NONE)"),
            ],
            border="magenta",
        )
    )
    if occupied:
        console.print(
            f"[bold yellow]ANTI-STACK[/]  ignoring {', '.join(occupied)} for NEW entries "
            "(desk monitors them separately)."
        )
    console.print()

    console.print("[dim]ORACLE is scanning the global wire against the unoccupied Demo book…[/]")
    try:
        brief: AnalystBrief = analyst.brief(
            triggers, entry_universe, occupied=occupied
        )
    except Exception as exc:
        brief = AnalystBrief(
            thesis="ORACLE degraded — live brief failed closed.",
            rationale=str(exc)[:240],
            primary_symbol=STAND_DOWN_SYMBOL,
            side="none",
            conviction=0,
            llm_degraded=True,
            model=cortex.model_name,
            wire_headlines=[t.headline for t in triggers],
            selection_reason="STAND_DOWN: ORACLE exception. No blind fallback.",
        )
    idle = is_idle_brief(brief)
    if idle:
        target = STAND_DOWN_SYMBOL
        brief.primary_symbol = STAND_DOWN_SYMBOL
        brief.side = "none"
        brief.conviction = 0
    else:
        target = listed_demo_symbol(bitget, brief.primary_symbol, entry_universe)
        brief.primary_symbol = target
        occupied_hit = is_occupied(target, book)
        if is_none_symbol(target) or occupied_hit:
            if occupied_hit:
                console.print(
                    f"[bold yellow]ANTI-STACK[/] ORACLE pick {target} is occupied — standing down."
                )
            idle = True
            brief.side = "none"
            brief.conviction = 0
            brief.primary_symbol = STAND_DOWN_SYMBOL
            target = STAND_DOWN_SYMBOL
    for flag in detect_stay_away(triggers, universe):
        if flag not in brief.stay_away:
            brief.stay_away.append(flag)

    notifier.alert_news_analysis(
        headlines=list(brief.wire_headlines or [t.headline for t in triggers]),
        news_good=brief.news_good,
        news_bad=brief.news_bad,
        session=clock["line"],
        sources=[t.source for t in triggers],
        universe_n=len(entry_universe),
    )
    if brief.stay_away:
        notifier.alert_stay_away(
            items=list(brief.stay_away),
            rationale=brief.news_bad or brief.rationale,
            session=clock["line"],
        )

    phase("PHASE 3.5  ·  POSITION DESK  ·  TRAILING SCAN")
    if not book:
        console.print("[dim]FLAT — no open Demo positions.[/]")
    else:
        pos_table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=True)
        pos_table.add_column("SYMBOL", style="bold white")
        pos_table.add_column("SIDE", no_wrap=True)
        pos_table.add_column("QTY", justify="right")
        pos_table.add_column("ENTRY", justify="right")
        pos_table.add_column("MARK", justify="right")
        pos_table.add_column("PnL %", justify="right")
        pos_table.add_column("USDT", justify="right")
        for pos in book:
            pnl_pct = float(pos.get("pnl_pct") or 0.0)
            pnl_usdt = float(pos.get("pnl_usdt") or 0.0)
            style = "green" if pnl_pct >= 0 else "red"
            pos_table.add_row(
                str(pos.get("symbol") or ""),
                str(pos.get("side") or "").upper(),
                str(pos.get("contracts") or ""),
                str(pos.get("entry_price") or ""),
                str(pos.get("mark_price") or ""),
                f"{pnl_pct:+.2f}%",
                f"{pnl_usdt:+.2f}",
                style=style,
            )
        console.print(pos_table)
    desk_actions: list[dict[str, Any]] = []
    try:
        desk_actions = desk.manage(bitget, brief)
    except Exception as exc:
        console.print(f"[bold yellow]Desk manage degraded:[/] {exc}")
        desk_actions = []
    if desk_actions:
        for action in desk_actions:
            flag = "green" if float(action.get("pnl_pct") or 0.0) >= 0 else "red"
            console.print(
                f"  [{flag}]{action.get('kind') or action.get('status')}[/]  "
                f"{action.get('symbol')}  "
                f"{float(action.get('pnl_pct') or 0.0):+.2f}%  "
                f"{float(action.get('pnl_usdt') or 0.0):+.2f} USDT  "
                f"{action.get('reason') or ''}"
            )
        _emit_desk_actions(notifier, desk_actions)
        for action in desk_actions:
            try:
                memory.note_close(action)
            except Exception:
                continue
    else:
        console.print("[dim]No autonomous close this cycle.[/]")
    console.print()
    if brief.llm_degraded:
        notifier.alert_api_error(
            error=brief.rationale or "Bitget Hackathon - Qwen 3.8 Max timeout / degraded",
            where="ORACLE / Bitget Hackathon - Qwen 3.8 Max",
            action="STAND_DOWN",
            session=clock["line"],
        )

    ticker: dict[str, Any]
    book: dict[str, Any]
    ta: dict[str, Any]
    last: float | None
    if idle:
        ticker = {"ok": False, "mocked": False, "last": None, "symbol": STAND_DOWN_SYMBOL}
        book = {"ok": False, "symbol": STAND_DOWN_SYMBOL}
        ta = {"ok": False, "rsi": None, "symbol": STAND_DOWN_SYMBOL, "timeframe": "", "period": RSI_PERIOD}
        fundamentals = {"ok": False, "symbol": STAND_DOWN_SYMBOL}
        last = None
        spread_s = "n/a"
    else:
        try:
            ticker = (
                bitget.fetch_ticker(target)
                if bitget
                else {"ok": False, "mocked": False, "last": None, "symbol": target}
            )
            book = bitget.fetch_order_book(target) if bitget else {"ok": False, "symbol": target}
        except Exception as exc:
            ticker = {"ok": False, "mocked": False, "last": None, "symbol": target, "error": str(exc)[:160]}
            book = {"ok": False, "symbol": target, "error": str(exc)[:160]}
            notifier.alert_api_error(
                error=str(exc)[:400],
                where="Bitget ticker/L2",
                action="STAND_DOWN",
                session=clock["line"],
            )
        last = bbo_peg_price(
            brief.side,
            book.get("best_bid") if book.get("best_bid") is not None else ticker.get("bid"),
            book.get("best_ask") if book.get("best_ask") is not None else ticker.get("ask"),
        )
        if last is None:
            last = _safe_float(ticker.get("mark")) or _safe_float(ticker.get("last"))
        spread = book.get("spread_pct")
        spread_s = f"{float(spread):.4f}%" if isinstance(spread, (int, float)) else "n/a"
        try:
            ta = bitget.fetch_mtf_bundle(target) if bitget else {"ok": False, "rsi": None, "symbol": target}
        except Exception as exc:
            ta = {
                "ok": False,
                "rsi": None,
                "symbol": target,
                "timeframe": "",
                "period": RSI_PERIOD,
                "error": str(exc)[:160],
            }
        try:
            fundamentals = bitget.fetch_fundamentals(target) if bitget else {"ok": False, "symbol": target}
        except Exception as exc:
            fundamentals = {"ok": False, "symbol": target, "error": str(exc)[:160]}
    console.print(
        kv_panel(
            "LIVE BOOK",
            [
                ("symbol", target),
                ("picked", "STAND_DOWN" if idle else "ORACLE · live RSS"),
                ("peg", "n/a" if last is None else str(last)),
                ("bid", str(book.get("best_bid") if book.get("best_bid") is not None else ticker.get("bid") or "n/a")),
                ("ask", str(book.get("best_ask") if book.get("best_ask") is not None else ticker.get("ask") or "n/a")),
                ("mark", str(ticker.get("mark") if ticker.get("mark") is not None else "n/a")),
                ("spread", spread_s),
                ("feed", str(ticker.get("source") or book.get("source") or "bitget.mainnet")),
                ("l2", book.get("source") or ("LIVE" if book.get("ok") else "FAULT")),
                ("model", brief.model or cortex.model_name),
            ],
            border="cyan",
        )
    )
    console.print()
    rsi_val = ta.get("rsi") if isinstance(ta.get("rsi"), (int, float)) else None
    rsi_s = "n/a" if rsi_val is None else f"{float(rsi_val):.2f}"
    zone = rsi_zone(rsi_val)
    ta_reason = "" if idle else (confluence_veto(brief.side, rsi_val) or candle_veto(brief.side, ta))
    if idle:
        ta_line = "SKIPPED — ORACLE idle"
        ta_border = "yellow"
    elif ta_reason:
        ta_line = ta_reason
        ta_border = "red"
    elif rsi_val is None and not ta.get("ok"):
        ta_line = "SKIPPED — no OHLCV; news thesis proceeds without momentum veto"
        ta_border = "yellow"
    else:
        ta_line = "PASS — news + candles + RSI agree"
        ta_border = "green"
    mcap = fundamentals.get("market_cap_usdt")
    supply = fundamentals.get("total_supply")
    console.print(
        kv_panel(
            "TA CONFLUENCE  ·  RSI + CANDLES + FUNDAMENTALS",
            [
                ("symbol", str(ta.get("symbol") or target)),
                ("timeframe", str(ta.get("timeframe") or "—")),
                ("rsi", rsi_s),
                ("zone", zone),
                ("structure", str(ta.get("structure") or "unmeasured")),
                ("pattern", str(ta.get("pattern") or "none")),
                ("volatility", str(ta.get("volatility") or "n/a")),
                ("last", str(fundamentals.get("price") or last or "n/a")),
                ("mcap", "n/a" if mcap is None else f"{float(mcap):,.0f}"),
                ("supply", "n/a" if supply is None else f"{float(supply):,.0f}"),
                ("vol 24h", str(fundamentals.get("volume_24h_usdt") or "n/a")),
                ("side", str(brief.side or "none").upper()),
                ("rule", "BUY RSI<70 & no breakdown  ·  SELL RSI>30 & no breakout"),
                ("verdict", ta_line),
            ],
            border=ta_border,
        )
    )
    console.print()

    phase("PHASE 4  ·  BOARD DEBATE")
    if idle:
        console.print("[dim]ORACLE idle — SENTINEL skipped. Clean STAND_DOWN, no NVDA fallback.[/]")
        risk = RiskReport(
            verdict="CLEAR",
            fake_news_risk="MEDIUM",
            black_swan_flags=["oracle_stand_down"],
            max_notional_usdt=0.0,
            size_multiplier=0.0,
            rationale=brief.rationale or "ORACLE STAND_DOWN: no actionable news.",
            llm_degraded=brief.llm_degraded,
            model=cortex.model_name,
            rsi=rsi_val,
            rsi_timeframe=str(ta.get("timeframe") or ""),
            rsi_period=int(ta.get("period") or RSI_PERIOD),
            ta_verdict="SKIPPED",
        )
    else:
        console.print("[dim]SENTINEL is stress-testing rumor quality, L2 spread, RSI confluence, and black-swan flags…[/]")
        try:
            risk = sentinel.evaluate(
                brief,
                ticker,
                settings.paper_notional_usdt,
                tradable_symbol=target,
                order_book=book,
                ta=ta,
                fundamentals=fundamentals,
                daily=memory.daily_state(),
                equity_usdt=equity_usdt,
            )
        except Exception as exc:
            notifier.alert_api_error(
                error=str(exc)[:400],
                where="SENTINEL / Bitget Hackathon - Qwen 3.8 Max",
                action="VETO",
                session=clock["line"],
            )
            risk = RiskReport(
                verdict="VETO",
                fake_news_risk="HIGH",
                black_swan_flags=["sentinel_rail_fault"],
                rationale=f"SENTINEL degraded closed: {exc}"[:400],
                llm_degraded=True,
                model=cortex.model_name,
                rsi=rsi_val,
                rsi_timeframe=str(ta.get("timeframe") or ""),
                rsi_period=int(ta.get("period") or RSI_PERIOD),
                ta_verdict="VETO" if ta_reason else ("SKIPPED" if rsi_val is None else "PASS"),
            )
    if risk.daily_halt:
        try:
            code = "CHOP" if "Choppy" in risk.daily_halt else (
                "STOP_LOSS" if "Stop-loss" in risk.daily_halt else (
                    "WIN_STREAK" if "Win-streak" in risk.daily_halt else (
                        "MAX_TRADES" if "limit reached" in risk.daily_halt else "DAILY"
                    )
                )
            )
            memory.halt_day(code, risk.daily_halt)
        except Exception:
            pass
    if risk.spread_pct is not None:
        spread_s = f"{risk.spread_pct:.4f}%"

    oracle_panel = Panel(
        Group(
            Text(f"BIAS  {brief.monday_gap_bias}   SIDE  {brief.side.upper()}   CONV  {brief.conviction}", style="bold cyan"),
            Text(f"SYMBOL  {brief.primary_symbol}   MODEL  {brief.model}", style="dim"),
            Text(""),
            Text(clip(brief.thesis), style="white"),
            Text(""),
            Text(clip(brief.rationale), style="grey70"),
            Text(f"degraded={brief.llm_degraded}", style="yellow" if brief.llm_degraded else "dim"),
        ),
        title="[bold cyan]ORACLE  ·  ANALYST[/]",
        border_style="cyan",
        box=box.DOUBLE,
    )
    risk_color = {"CLEAR": "green", "REDUCE": "yellow", "VETO": "red"}.get(risk.verdict, "yellow")
    sentinel_panel = Panel(
        Group(
            Text(f"VERDICT  {risk.verdict}   FAKE-NEWS  {risk.fake_news_risk}", style=f"bold {risk_color}"),
            Text(
                f"CAP  {risk.max_notional_usdt} USDT   MULT  {risk.size_multiplier}   "
                f"SPREAD  {spread_s}   RSI  {rsi_s}   TA  {risk.ta_verdict}   "
                f"RISK  {risk.asset_risk_score}   SL {risk.sl_margin_frac:.0%} margin   "
                f"MODEL  {risk.model}",
                style="dim",
            ),
            Text(""),
            Text(clip(risk.rationale), style="white"),
            Text(""),
            Text("flags: " + (", ".join(risk.black_swan_flags) or "none"), style="grey70"),
            Text(f"degraded={risk.llm_degraded}", style="yellow" if risk.llm_degraded else "dim"),
        ),
        title="[bold yellow]SENTINEL  ·  RISK  (VETO POWER)[/]",
        border_style=risk_color,
        box=box.DOUBLE,
    )
    console.print(Columns([oracle_panel, sentinel_panel]))
    console.print()

    if bitget is None:
        stub = BoardDecision(
            action="STAND_DOWN",
            consensus="VETOED" if risk.verdict == "VETO" and not idle else "DEGRADED",
            symbol=target,
            side=brief.side,
            reasoning=risk.rationale if risk.verdict == "VETO" and not idle else (
                brief.rationale or "No Bitget Demo rail — standing down this cycle."
            ),
            model=risk.model or brief.model,
        )
        stub_order = {"ok": False, "status": "VETOED" if stub.consensus == "VETOED" else "STAND_DOWN"}
        _persist_engine_snapshot(memory, brief, risk, stub, stub_order)
        _dispatch_alerts(
            notifier,
            brief,
            risk,
            stub,
            stub_order,
            None,
            session=clock["line"],
        )
        notifier.drain(timeout=4.0)
        console.print("[bold red]No Bitget Demo rail — cannot attest/execute. Standing down this cycle.[/]")
        console.print(f"  elapsed {time.perf_counter() - t0:.1f}s")
        return
    if arb is None:
        console.print("[bold yellow]Arbitrum unbound — execution will skip on-chain proof if attest fails.[/]")
        arb = ArbitrumSepolia(settings)

    phase("PHASE 5  ·  CHAIRMAN  ·  ATTEST + PAPER EXECUTE")
    try:
        bundle = chairman.convene(
            brief=brief,
            risk=risk,
            bitget=bitget,
            arbitrum=arb,
            tradable_symbol=target,
            last_price=last if last is not None else 0.0,
        )
    except Exception as exc:
        console.print(f"[bold yellow]CHAIRMAN rail fault — degrading:[/] {exc}")
        bundle = {
            "decision": chairman.synthesize(
                brief, risk, target, last if last is not None else 0.0
            ),
            "attestation": None,
            "order": {"ok": False, "status": "ERROR", "error": str(exc)[:240]},
        }
    decision = bundle["decision"]
    attestation = bundle["attestation"]
    order = bundle["order"] or {}

    action_style = "bold green" if decision.action == "EXECUTE" else "bold red"
    console.print(
        Panel(
            Group(
                Text(f"{decision.action}   consensus={decision.consensus}", style=action_style, justify="center"),
                Text(f"{decision.side.upper()}  {decision.amount:.6f}  {decision.symbol}  (~{decision.notional_usdt} USDT)", justify="center"),
                Text(f"sha256  {decision.reasoning_hash}", style="dim", justify="center"),
                Text(f"model  {decision.model or cortex.model_name}", style="dim", justify="center"),
                Text(""),
                Text(clip(decision.reasoning), justify="center"),
            ),
            title="[bold magenta]CHAIRMAN  ·  BOARD DECISION[/]",
            border_style="magenta",
            box=box.DOUBLE,
        )
    )
    console.print()

    if attestation is not None:
        att_style = "green" if attestation.ok else "yellow"
        console.print(
            kv_panel(
                "ARBITRUM PROOF OF THOUGHT",
                [
                    ("ok", str(attestation.ok)),
                    ("skipped", str(attestation.skipped)),
                    ("reason", attestation.reason or "—"),
                    ("tx", attestation.tx_hash or "—"),
                    ("explorer", attestation.explorer_url or "—"),
                    ("from", attestation.from_address or "—"),
                ],
                border=att_style,
            )
        )
        console.print()

    order_ok = bool(order.get("ok"))
    console.print(
        kv_panel(
            "BITGET PAPER ORDER",
            [
                ("ok", str(order_ok)),
                ("status", str(order.get("status") or "—")),
                ("order_id", str(order.get("order_id") or "—")),
                ("instrument", str(order.get("instrument") or order.get("symbol") or decision.symbol)),
                ("direction", str(order.get("direction") or order.get("side") or decision.side)),
                ("quantity", str(order.get("quantity") if order.get("quantity") is not None else order.get("amount") or decision.amount)),
                (
                    "price",
                    str(
                        order.get("price")
                        if order.get("price") is not None
                        else (
                            order.get("entry_price")
                            if order.get("entry_price") is not None
                            else 0.0
                        )
                    ),
                ),
                ("acct Δ", str(order.get("account_balance_change") if order.get("account_balance_change") is not None else "0.0")),
                ("sl (margin)", str(order.get("sl_price") or risk.sl_price or "—")),
                ("tp (scale 50%)", str(order.get("tp_price") or risk.tp_price or "—")),
                ("margin USDT", str(order.get("margin_usdt") if order.get("margin_usdt") is not None else risk.margin_usdt or "—")),
                ("sl_frac", str(order.get("sl_margin_frac") if order.get("sl_margin_frac") is not None else risk.sl_margin_frac)),
                ("sl_order", str((order.get("sl_order") or {}).get("id") if isinstance(order.get("sl_order"), dict) else order.get("sl_error") or "—")),
                ("tp_order", str((order.get("tp_order") or {}).get("id") if isinstance(order.get("tp_order"), dict) else order.get("tp_error") or "—")),
                ("hash", str(order.get("reasoning_hash") or decision.reasoning_hash)),
                ("log", str(order.get("log_path") or "data/logs/trades.json")),
                ("error", str(order.get("error") or "—")),
            ],
            border="green" if order_ok else "yellow",
        )
    )
    console.print()

    if bool(order.get("ok")):
        try:
            desk.record_open(order, brief)
        except Exception:
            pass

    _persist_engine_snapshot(memory, brief, risk, decision, order)

    _dispatch_alerts(
        notifier, brief, risk, decision, order, attestation, session=clock["line"]
    )
    notifier.drain(timeout=4.0)

    elapsed = time.perf_counter() - t0
    console.print(
        Panel(
            Align.center(
                Group(
                    Text("NEXUS CYCLE COMPLETE", style="bold green1", justify="center"),
                    Text(
                        f"event → decision → {'on-chain attest → ' if attestation and attestation.ok else ''}"
                        f"{'paper fill' if order_ok else order.get('status', 'stand-down')}",
                        style="cyan",
                        justify="center",
                    ),
                    Text(
                        f"{elapsed:.1f}s  ·  {HACKATHON}  ·  paper only  ·  {cortex.model_name}",
                        style="dim",
                        justify="center",
                    ),
                )
            ),
            border_style="green1",
            box=box.DOUBLE,
        )
    )


def main() -> int:
    try:
        return _main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        console.print(
            Panel(
                f"[bold red]NEXUS DEGRADED — unhandled fault swallowed[/]\n{exc}",
                border_style="red",
                box=box.HEAVY,
            )
        )
        return 1


def _main() -> int:
    if LIVE_TRADING_ENABLED:
        console.print("[bold red]REFUSING TO BOOT: live trading flag is on.[/]")
        return 2

    render_banner()

    phase("PHASE 0  ·  KERNEL / COMPLIANCE")
    try:
        settings = load_settings()
    except Exception as exc:
        console.print(f"[bold red]BOOT HALTED:[/] {exc}")
        return 2
    console.print(
        kv_panel(
            "COMPLIANCE",
            [
                ("version", VERSION),
                ("paper_trading", str(settings.bitget_paper_trading)),
                ("live_trading", str(LIVE_TRADING_ENABLED)),
                ("cortex", settings.public_status()["cortex_backend"]),
                ("qwen_requested", settings.qwen_model),
                ("qwen_resolved", settings.resolved_qwen_model or "(unresolved)"),
                ("qwen_source", settings.qwen_source or "(pending)"),
                ("qwen_key", settings.public_status()["qwen_key"]),
                ("bitget_key", settings.public_status()["bitget_key"]),
                ("arb_chain", str(settings.arbitrum_sepolia_chain_id)),
                ("telegram", settings.public_status()["telegram"]),
            ],
            border="red",
        )
    )
    console.print()

    notifier = TelegramNotifier.from_settings(settings)
    send_startup_message(notifier)
    desk = PositionDesk()

    phase("PHASE 1  ·  BITGET HACKATHON - QWEN 3.8 MAX")
    cortex = QwenCortex(settings)
    memory = BoardMemory()
    console.print(
        kv_panel(
            "CORTEX  ·  Bitget Hackathon - Qwen 3.8 Max",
            [
                ("backend", cortex.backend),
                ("model", cortex.selected_model),
                ("base_url", "https://hackathon.bitgetops.com/v1"),
                ("source", settings.qwen_source or "default"),
            ],
            border="magenta",
        )
    )
    console.print()

    phase("PHASE 2  ·  MARKET RAILS")
    bitget: BitgetPaperConnector | None = None
    bitget_ping: dict[str, Any] = {}
    bitget_bal: dict[str, Any] = {}
    bitget_err: str | None = None
    try:
        connector = BitgetPaperConnector(settings)
        bitget_ping = connector.ping()
        bitget_bal = connector.fetch_demo_balance()
        bitget = connector
    except Exception as exc:
        bitget = None
        bitget_err = str(exc)[:240]

    arb: ArbitrumSepolia | None = None
    arb_ping: dict[str, Any] = {}
    arb_bal: dict[str, Any] = {}
    arb_err: str | None = None
    try:
        arb = ArbitrumSepolia(settings)
        arb_ping = arb.ping()
        arb_bal = arb.read_balance()
    except Exception as exc:
        arb_err = str(exc)[:240]

    usdt = (bitget_bal.get("assets") or {}).get("USDT") or {}
    console.print(
        Columns(
            [
                kv_panel(
                    "BITGET DEMO",
                    [
                        ("mode", "sandbox / PAPTRADING=1"),
                        ("status", "ONLINE" if bitget else f"FAULT {bitget_err}"),
                        ("markets", str(bitget_ping.get("markets", "—"))),
                        ("universe", str(bitget_ping.get("universe", "—"))),
                        ("symbol", str(bitget_ping.get("symbol") or settings.bitget_symbol)),
                        ("type", str(bitget_ping.get("market_type", "spot"))),
                        ("USDT free", f"{usdt.get('free', '—')}"),
                    ],
                    border="cyan",
                ),
                kv_panel(
                    "ARBITRUM SEPOLIA",
                    [
                        ("chain", str(arb_ping.get("chain_id") or settings.arbitrum_sepolia_chain_id)),
                        ("status", "ONLINE" if arb_ping else f"FAULT {arb_err}"),
                        ("block", f"#{arb_ping.get('block', '—')}"),
                        ("address", str(arb_ping.get("address") or "—")),
                        ("ETH", f"{arb_bal.get('eth', '—')}"),
                    ],
                    border="green",
                ),
            ]
        )
    )
    console.print()

    table = Table(
        title="CHRONOS-NEXUS  ·  SYSTEMS CHECK",
        title_style="bold green",
        box=box.DOUBLE_EDGE,
        border_style="green",
        header_style="bold cyan",
        show_lines=True,
        expand=True,
    )
    table.add_column("SUBSYSTEM", style="bold white", no_wrap=True)
    table.add_column("DETAIL", style="grey70")
    table.add_column("STATUS", justify="center")
    table.add_row("Analyst Agent", "ORACLE · live RSS wire · anti-stack", status_dot(True))
    table.add_row("Risk Manager", "SENTINEL · spread + RSI + candles + mcap", status_dot(True))
    table.add_row("TA Confluence", "RSI(14) 15m · candles · BUY <70 · SELL >30", status_dot(True))
    table.add_row("Dynamic SL", "50–100% of invested margin by asset risk", status_dot(True))
    table.add_row("Scaled TP", f"+{SCALE_OUT_PCT:.0%} first 50% lock + trailing runner", status_dot(True))
    table.add_row(
        "Daily Limits",
        f"{DAILY_MAX_ENTRIES} entries · {DAILY_WIN_STREAK} win-streak · {DAILY_RISK_PCT:.0%} equity cap",
        status_dot(True),
    )
    table.add_row("Executive Agent", "CHAIRMAN · attest + execute", status_dot(True))
    table.add_row("Bitget Hackathon - Qwen 3.8 Max", f"{cortex.backend} · {cortex.selected_model}", status_dot(cortex.backend != "offline"))
    table.add_row(
        "Bitget Paper",
        f"{bitget_ping.get('symbol') or bitget_err or 'unbound'} · univ {bitget_ping.get('universe', '—')}",
        status_dot(bitget is not None),
    )
    table.add_row(
        "Arbitrum Sepolia",
        f"chain {arb_ping.get('chain_id', 421614)}",
        status_dot(bool(arb_ping)),
    )
    table.add_row("Board Memory", f"{len(memory.recent())} cycles · data/history.json", status_dot(True))
    table.add_row(
        "Telegram Alerts",
        "armed · async HTML + PnL cards" if notifier.enabled else "disarmed · no token/chat",
        status_dot(notifier.enabled, label_ok="ARMED", label_bad="OFF"),
    )
    table.add_row(
        "Telegram Commands",
        "/positions /close /closeall /price /balance /pnl /status + Spot chatbox"
        if notifier.enabled and bitget is not None
        else "disarmed",
        status_dot(notifier.enabled and bitget is not None, label_ok="ARMED", label_bad="OFF"),
    )
    table.add_row(
        "Terminal Commands",
        "nexus> /positions /close /closeall /pnl /status /help",
        status_dot(True, label_ok="ARMED", label_bad="OFF"),
    )
    table.add_row("Position Desk", "SL/TP · trail · thesis close", status_dot(True))
    table.add_row("Compliance Lock", "live trading hard-disabled", status_dot(not LIVE_TRADING_ENABLED))
    console.print(table)
    console.print()

    if bitget is None or arb is None:
        console.print("[bold yellow]Rails incomplete — board will still debate; execution may skip.[/]\n")

    command_desk = CommandDesk(
        bitget,
        desk,
        on_close=lambda result: _emit_desk_actions(notifier, [result]),
        memory=memory,
    )
    TelegramCommandLoop(notifier, command_desk).start()
    TerminalCommandLoop(command_desk, printer=lambda msg: console.print(msg)).start()

    while True:
        try:
            _run_trading_cycle(
                settings=settings,
                cortex=cortex,
                memory=memory,
                bitget=bitget,
                arb=arb,
                notifier=notifier,
                desk=desk,
            )
            console.print(
                f"  [dim]Hourly daemon — next cycle in {CYCLE_INTERVAL_SEC}s. "
                "Type /positions /close /pnl /status at nexus>.[/]"
            )
            time.sleep(CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            console.print(
                Panel(
                    f"[bold red]NEXUS CYCLE FAULT — daemon recovering[/]\n{exc}",
                    border_style="red",
                    box=box.HEAVY,
                )
            )
            try:
                notifier.alert_api_error(
                    error=str(exc)[:400],
                    where="cycle",
                    action="STAND_DOWN",
                    session=session_clock()["line"],
                )
                notifier.drain(timeout=3.0)
            except Exception:
                pass
            console.print(
                f"  [dim]Retrying in {CYCLE_ERROR_BACKOFF_SEC}s (5 minutes).[/]"
            )
            time.sleep(CYCLE_ERROR_BACKOFF_SEC)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n  [bold red]■[/] [red]interrupt — Nexus halted.[/]")
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"\n  [bold red]■[/] [red]Nexus halted (degraded): {exc}[/]")
        raise SystemExit(1)
