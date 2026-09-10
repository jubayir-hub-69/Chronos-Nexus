#!/usr/bin/env python3
"""Chronos-Nexus — autonomous weekend-arbitrage mesh.

Bitget AI Base Camp Hackathon S2 · Agentic Trading
Paper trading only. Demo lifecycle: wire → debate → attest → execute.
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

from agents.analyst import AnalystAgent, snap_to_universe
from agents.executive import ExecutiveAgent
from agents.risk_manager import RiskManagerAgent
from connectors.arbitrum import ArbitrumSepolia
from connectors.bitget_paper import BitgetPaperConnector
from core.config import load_settings
from core.llm import GeminiCortex
from core.memory import BoardMemory
from core.schemas import AnalystBrief, BoardDecision, RiskReport
from utils.notifier import TelegramNotifier, send_startup_message

LIVE_TRADING_ENABLED = False
VERSION = "0.5.0-universe"
HACKATHON = "Bitget AI Base Camp Hackathon S2"

# Bitget Demo stock-perp universe — ORACLE must pick ONE of these from the live wire.
TOKEN_UNIVERSE: list[str] = [
    "AAPL/USDT:USDT",
    "NVDA/USDT:USDT",
    "MSFT/USDT:USDT",
    "GOOG/USDT:USDT",
    "TSLA/USDT:USDT",
]

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
            Text("Hackathon S2  ·  Agentic Trading  ·  rToken 24/7  ·  PAPER ONLY", style="dim cyan")
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
    snapped = snap_to_universe(chosen, universe)
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
    risk: RiskReport,
    decision: BoardDecision,
    order: dict[str, Any],
    attestation: Any,
) -> None:
    """Fire-and-forget Telegram. Never raises into the CIC."""
    try:
        status = str(order.get("status") or "")
        symbol = str(order.get("symbol") or decision.symbol or "")
        if status == "POSITION_ALREADY_OPEN":
            notifier.alert_position_open(
                symbol=symbol,
                side=str(order.get("side") or decision.side or ""),
                detail=str(order.get("error") or "Position already open"),
            )
            return
        if risk.verdict == "VETO" or decision.consensus == "VETOED":
            notifier.alert_veto(
                reason=risk.rationale,
                symbol=symbol,
                flags=list(risk.black_swan_flags or []),
                model=risk.model or decision.model,
            )
            return
        if bool(order.get("ok")):
            explorer = getattr(attestation, "explorer_url", None) if attestation is not None else None
            notifier.alert_execute(
                symbol=str(order.get("symbol") or decision.symbol),
                side=str(order.get("side") or decision.side),
                amount=order.get("amount") if order.get("amount") is not None else decision.amount,
                sl_price=order.get("sl_price"),
                tp_price=order.get("tp_price"),
                order_id=order.get("order_id"),
                explorer_url=explorer,
                model=decision.model,
            )
    except Exception:
        return


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

    t0 = time.perf_counter()
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
                ("gemini_requested", settings.gemini_model),
                ("gemini_resolved", settings.resolved_gemini_model or "(unresolved)"),
                ("gemini_source", settings.gemini_discovery_source),
                ("gemini_key", settings.public_status()["gemini_key"]),
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

    phase("PHASE 1  ·  GEMINI CORTEX")
    cortex = GeminiCortex(settings)
    memory = BoardMemory()
    catalog_n = len(settings.gemini_catalog)
    console.print(
        kv_panel(
            "CORTEX",
            [
                ("backend", cortex.backend),
                ("model", cortex.selected_model),
                ("catalog", f"{catalog_n} generateContent models" if catalog_n else "fallback chain"),
                ("source", settings.gemini_discovery_source),
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
    table.add_row("Analyst Agent", "ORACLE · live RSS wire", status_dot(True))
    table.add_row("Risk Manager", "SENTINEL · veto + L2 spread", status_dot(True))
    table.add_row("Executive Agent", "CHAIRMAN · attest + execute", status_dot(True))
    table.add_row("Gemini Cortex", f"{cortex.backend} · {cortex.selected_model}", status_dot(cortex.backend != "offline"))
    table.add_row("Bitget Paper", str(bitget_ping.get("symbol") or bitget_err or "unbound"), status_dot(bitget is not None))
    table.add_row(
        "Arbitrum Sepolia",
        f"chain {arb_ping.get('chain_id', 421614)}",
        status_dot(bool(arb_ping)),
    )
    table.add_row("Board Memory", f"{len(memory.recent())} cycles · data/history.json", status_dot(True))
    table.add_row(
        "Telegram Alerts",
        "armed · async HTML" if notifier.enabled else "disarmed · no token/chat",
        status_dot(notifier.enabled, label_ok="ARMED", label_bad="OFF"),
    )
    table.add_row("Compliance Lock", "live trading hard-disabled", status_dot(not LIVE_TRADING_ENABLED))
    console.print(table)
    console.print()

    if bitget is None or arb is None:
        console.print("[bold yellow]Rails incomplete — board will still debate; execution may skip.[/]\n")

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
    console.print(
        kv_panel(
            "TOKEN UNIVERSE",
            [("symbols", "  ".join(TOKEN_UNIVERSE))],
            border="magenta",
        )
    )
    console.print()

    console.print("[dim]ORACLE is pricing Monday cash gaps and picking the most relevant rToken…[/]")
    try:
        brief: AnalystBrief = analyst.brief(triggers, TOKEN_UNIVERSE)
    except Exception as exc:
        brief = AnalystBrief(
            thesis="ORACLE degraded — live brief failed closed.",
            rationale=str(exc)[:240],
            primary_symbol=snap_to_universe("", TOKEN_UNIVERSE, [t.headline for t in triggers]),
            conviction=10,
            llm_degraded=True,
            model=cortex.model_name,
            wire_headlines=[t.headline for t in triggers],
        )
    target = listed_demo_symbol(bitget, brief.primary_symbol, TOKEN_UNIVERSE)
    brief.primary_symbol = target
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
    last = _safe_float(ticker.get("last"))
    spread = book.get("spread_pct")
    spread_s = f"{float(spread):.4f}%" if isinstance(spread, (int, float)) else "n/a"
    console.print(
        kv_panel(
            "LIVE BOOK",
            [
                ("symbol", target),
                ("picked", "ORACLE · live RSS"),
                ("last", "n/a" if last is None else str(last)),
                ("bid", str(book.get("best_bid") if book.get("best_bid") is not None else ticker.get("bid") or "n/a")),
                ("ask", str(book.get("best_ask") if book.get("best_ask") is not None else ticker.get("ask") or "n/a")),
                ("spread", spread_s),
                ("l2", book.get("source") or ("LIVE" if book.get("ok") else "FAULT")),
                ("model", brief.model or cortex.model_name),
            ],
            border="cyan",
        )
    )
    console.print()

    phase("PHASE 4  ·  BOARD DEBATE")
    console.print("[dim]SENTINEL is stress-testing rumor quality, L2 spread, and black-swan flags…[/]")
    try:
        risk: RiskReport = sentinel.evaluate(
            brief,
            ticker,
            settings.paper_notional_usdt,
            tradable_symbol=target,
            order_book=book,
        )
    except Exception as exc:
        risk = RiskReport(
            verdict="VETO",
            fake_news_risk="HIGH",
            black_swan_flags=["sentinel_rail_fault"],
            rationale=f"SENTINEL degraded closed: {exc}"[:400],
            llm_degraded=True,
            model=cortex.model_name,
        )
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
                f"SPREAD  {spread_s}   MODEL  {risk.model}",
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
        if risk.verdict == "VETO":
            stub = BoardDecision(
                action="STAND_DOWN",
                consensus="VETOED",
                symbol=target,
                side=brief.side,
                reasoning=risk.rationale,
                model=risk.model,
            )
            _dispatch_alerts(notifier, risk, stub, {"ok": False, "status": "VETOED"}, None)
            notifier.drain(timeout=2.0)
        console.print("[bold red]No Bitget Demo rail — cannot attest/execute. Halt after debate.[/]")
        console.print(f"  elapsed {time.perf_counter() - t0:.1f}s")
        return 1
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
                ("symbol", str(order.get("symbol") or decision.symbol)),
                ("side", str(order.get("side") or decision.side)),
                ("amount", str(order.get("amount") or decision.amount)),
                ("entry", str(order.get("entry_price") or "—")),
                ("sl -2%", str(order.get("sl_price") or "—")),
                ("tp +5%", str(order.get("tp_price") or "—")),
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

    _dispatch_alerts(notifier, risk, decision, order, attestation)
    notifier.drain(timeout=2.5)

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
    console.print("  [dim]Press Enter to shut down the Nexus.[/]")
    try:
        input()
    except EOFError:
        pass
    console.print("  [bold red]■[/] [red]CHRONOS-NEXUS offline.[/]")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n  [bold red]■[/] [red]interrupt — Nexus halted.[/]")
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"\n  [bold red]■[/] [red]Nexus halted (degraded): {exc}[/]")
        raise SystemExit(1)
