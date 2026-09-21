"""Lightweight board memory — last N paper cycles on disk.

ORACLE and CHAIRMAN inject this into Bitget Hackathon - Qwen 3.8 Max so the desk does not
repeat an identical failed setup. Not a vector store: a JSON ring buffer.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT

HISTORY_PATH = PROJECT_ROOT / "data" / "history.json"
TRADE_LOG = PROJECT_ROOT / "data" / "logs" / "trades.json"
MAX_TRADES = 5
DAILY_MAX_ENTRIES = 4
DAILY_WIN_STREAK = 3
DAILY_RISK_PCT = 0.06
DAILY_RISK_PCT_MIN = 0.05
VETO_REASON_DAILY_MAX = "VETO: Daily trade limit reached"
VETO_REASON_WIN_STREAK = "VETO: Win-streak stand-down — protecting the day's P&L"
VETO_REASON_STOP_LOSS_DAY = "VETO: Stop-loss halt — no new entries today"
VETO_REASON_DAILY_RISK = "VETO: Daily 6% portfolio risk budget exhausted"
VETO_REASON_NO_EQUITY = "VETO: No live Bitget fetch_balance equity — refusing to size"
_LOCK = threading.Lock()


class BoardMemory:
    def __init__(self, path: Path | None = None, max_trades: int = MAX_TRADES) -> None:
        self.path = path or HISTORY_PATH
        self.max_trades = max(1, int(max_trades))

    def load(self) -> list[dict[str, Any]]:
        rows = _read_trades(self.path)
        if rows:
            return rows[-self.max_trades :]
        if self.path == HISTORY_PATH:
            return _hydrate_from_trade_log()[-self.max_trades :]
        return []

    def recent(self, n: int | None = None) -> list[dict[str, Any]]:
        limit = self.max_trades if n is None else max(1, int(n))
        return self.load()[-limit:]

    def prompt_block(self) -> str:
        """Compact recap for Qwen 3.8 Max context. Empty memory is stated explicitly."""
        rows = self.recent()
        daily = self.daily_state()
        day_line = (
            f"DAILY RISK (UTC {daily.get('date')}): "
            f"entries={int(daily.get('entries') or 0)}/{DAILY_MAX_ENTRIES} "
            f"wins={int(daily.get('wins') or 0)}/{DAILY_WIN_STREAK} "
            f"sl={int(daily.get('sl_hits') or 0)} "
            f"deployed={float(daily.get('deployed_usdt') or 0.0):.2f} USDT "
            f"halt={daily.get('halt') or 'none'}"
        )
        if not rows:
            return f"BOARD MEMORY: empty (no prior cycles).\n{day_line}"
        lines = [
            f"BOARD MEMORY (last {len(rows)} cycles — do not repeat identical failed setups):",
            day_line,
        ]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {_format_row(row)}")
        return "\n".join(lines)

    def record(
        self,
        *,
        news_context: list[str],
        decision: dict[str, Any],
        result: dict[str, Any],
    ) -> Path:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "news_context": [str(h) for h in (news_context or []) if str(h).strip()][:8],
            "decision": {
                "action": decision.get("action"),
                "side": decision.get("side"),
                "symbol": decision.get("symbol"),
                "consensus": decision.get("consensus"),
                "verdict": decision.get("verdict"),
                "rsi": decision.get("rsi"),
                "ta_verdict": decision.get("ta_verdict"),
                "thesis": _clip(decision.get("thesis"), 400),
                "model": str(decision.get("model") or ""),
                "conviction": _opt_int(decision.get("conviction")),
                "sentiment": _opt_float(decision.get("sentiment")),
                "news_credibility": _opt_float(decision.get("news_credibility")),
                "news_conflict": bool(decision.get("news_conflict")),
                "news_impact": str(decision.get("news_impact") or ""),
                "news_good": _clip(decision.get("news_good"), 240),
                "news_bad": _clip(decision.get("news_bad"), 240),
            },
            "result": {
                "ok": bool(result.get("ok")),
                "status": str(result.get("status") or ""),
                "order_id": result.get("order_id"),
                "error": _clip(result.get("error"), 240),
            },
        }
        with _LOCK:
            payload = _read_payload(self.path)
            trades = list(payload.get("trades") or [])
            if not trades and self.path == HISTORY_PATH:
                trades = _hydrate_from_trade_log()
            trades.append(entry)
            trades = trades[-self.max_trades :]
            payload["trades"] = trades
            payload["updated"] = entry["ts"]
            payload["max_trades"] = self.max_trades
            daily = _coerce_daily(payload.get("daily"))
            payload["daily"] = daily
            payload["snapshot"] = _snapshot_from_trade(entry, daily)
            _atomic_write(self.path, payload)
        return self.path

    def daily_state(self) -> dict[str, Any]:
        """UTC-day ledger. Resets at 00:00 UTC. Survives hourly daemon restarts."""
        with _LOCK:
            payload = _read_payload(self.path)
            daily = _coerce_daily(payload.get("daily"))
            if payload.get("daily") != daily:
                payload["daily"] = daily
                payload["updated"] = datetime.now(timezone.utc).isoformat()
                if "trades" not in payload:
                    payload["trades"] = _read_trades(self.path)
                _atomic_write(self.path, payload)
            return dict(daily)

    def can_enter(self) -> tuple[bool, str]:
        reason = daily_block_reason(self.daily_state())
        return (not reason), reason

    def halt_day(self, code: str, reason: str) -> dict[str, Any]:
        def _apply(daily: dict[str, Any]) -> dict[str, Any]:
            if not daily.get("halt"):
                daily["halt"] = str(code or "HALT")
                daily["halt_reason"] = str(reason or code)
            return daily

        return self._mutate_daily(_apply)

    def note_entry(
        self,
        *,
        symbol: str,
        margin_usdt: float,
        order_id: str = "",
    ) -> dict[str, Any]:
        def _apply(daily: dict[str, Any]) -> dict[str, Any]:
            daily["entries"] = int(daily.get("entries") or 0) + 1
            try:
                add = max(0.0, float(margin_usdt or 0.0))
            except (TypeError, ValueError):
                add = 0.0
            daily["deployed_usdt"] = round(float(daily.get("deployed_usdt") or 0.0) + add, 6)
            fills = list(daily.get("fills") or [])
            fills.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "ENTRY",
                    "symbol": symbol,
                    "margin_usdt": add,
                    "order_id": order_id,
                }
            )
            daily["fills"] = fills[-32:]
            if int(daily["entries"]) >= DAILY_MAX_ENTRIES and not daily.get("halt"):
                daily["halt"] = "MAX_TRADES"
                daily["halt_reason"] = VETO_REASON_DAILY_MAX
            return daily

        return self._mutate_daily(_apply)

    def note_close(self, action: dict[str, Any] | None) -> dict[str, Any]:
        row = action if isinstance(action, dict) else {}
        if not row.get("ok"):
            return self.daily_state()
        kind = str(row.get("kind") or row.get("status") or row.get("reason") or "").upper()
        try:
            pnl = float(row.get("pnl_usdt") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0

        def _apply(daily: dict[str, Any]) -> dict[str, Any]:
            fills = list(daily.get("fills") or [])
            fills.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": kind,
                    "symbol": str(row.get("symbol") or ""),
                    "pnl_usdt": pnl,
                    "status": str(row.get("status") or ""),
                }
            )
            daily["fills"] = fills[-32:]
            try:
                prev = float(daily.get("realized_pnl_usdt") or 0.0)
            except (TypeError, ValueError):
                prev = 0.0
            daily["realized_pnl_usdt"] = round(prev + pnl, 6)
            if "PARTIAL" in kind:
                return daily
            if kind == "SL" or kind.startswith("SL") or " SL" in f" {kind}":
                daily["sl_hits"] = int(daily.get("sl_hits") or 0) + 1
                daily["losses"] = int(daily.get("losses") or 0) + 1
                daily["halt"] = "STOP_LOSS"
                daily["halt_reason"] = VETO_REASON_STOP_LOSS_DAY
                return daily
            if pnl > 0:
                daily["wins"] = int(daily.get("wins") or 0) + 1
                if int(daily["wins"]) >= DAILY_WIN_STREAK:
                    daily["halt"] = "WIN_STREAK"
                    daily["halt_reason"] = VETO_REASON_WIN_STREAK
            elif pnl < 0:
                daily["losses"] = int(daily.get("losses") or 0) + 1
            return daily

        return self._mutate_daily(_apply)

    def _mutate_daily(self, fn) -> dict[str, Any]:
        with _LOCK:
            payload = _read_payload(self.path)
            daily = fn(_coerce_daily(payload.get("daily")))
            payload["daily"] = daily
            payload["updated"] = datetime.now(timezone.utc).isoformat()
            if "trades" not in payload:
                payload["trades"] = []
            _atomic_write(self.path, payload)
            return dict(daily)

    def record_snapshot(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        """Persist the live ORACLE/SENTINEL/CHAIRMAN state for /status."""
        row = snapshot if isinstance(snapshot, dict) else {}
        with _LOCK:
            payload = _read_payload(self.path)
            daily = _coerce_daily(payload.get("daily"))
            payload["daily"] = daily
            stamped = dict(row)
            if not stamped.get("ts"):
                stamped["ts"] = datetime.now(timezone.utc).isoformat()
            stamped["desk"] = live_desk_status(daily, str(stamped.get("action") or ""))
            payload["snapshot"] = stamped
            payload["updated"] = stamped["ts"]
            if "trades" not in payload:
                payload["trades"] = []
            _atomic_write(self.path, payload)
            return dict(stamped)

    def latest_snapshot(self) -> dict[str, Any]:
        """Exact last engine snapshot. Empty dict fields stay empty — never invented."""
        with _LOCK:
            payload = _read_payload(self.path)
            snap = payload.get("snapshot")
            trades = list(payload.get("trades") or [])
            daily = _coerce_daily(payload.get("daily"))
        if isinstance(snap, dict) and snap:
            out = dict(snap)
            out["desk"] = live_desk_status(daily, str(out.get("action") or ""))
            return out
        if trades:
            return _snapshot_from_trade(trades[-1], daily)
        return {
            "ts": "",
            "scored": False,
            "desk": live_desk_status(daily, ""),
            "action": "",
        }

    def session_pnl(self) -> dict[str, Any]:
        """UTC-day realized PnL, win/loss, and remaining entry slots from the live ledger."""
        daily = self.daily_state()
        try:
            entries = int(daily.get("entries") or 0)
        except (TypeError, ValueError):
            entries = 0
        try:
            wins = int(daily.get("wins") or 0)
        except (TypeError, ValueError):
            wins = 0
        try:
            losses = int(daily.get("losses") or 0)
        except (TypeError, ValueError):
            losses = 0
        try:
            sl_hits = int(daily.get("sl_hits") or 0)
        except (TypeError, ValueError):
            sl_hits = 0
        fills = [f for f in (daily.get("fills") or []) if isinstance(f, dict)]
        fill_pnl = _sum_fill_pnl(fills)
        try:
            stored = float(daily.get("realized_pnl_usdt") or 0.0)
        except (TypeError, ValueError):
            stored = 0.0
        realized = stored
        if abs(realized) < 1e-12 and abs(fill_pnl) >= 1e-12:
            realized = fill_pnl
        blocked = bool(daily_block_reason(daily))
        trades_left = 0 if blocked else max(0, DAILY_MAX_ENTRIES - max(0, entries))
        return {
            "date": str(daily.get("date") or utc_today()),
            "realized_pnl_usdt": round(float(realized), 6),
            "wins": wins,
            "losses": losses,
            "sl_hits": sl_hits,
            "entries": entries,
            "entries_max": DAILY_MAX_ENTRIES,
            "trades_left": trades_left,
            "halt": str(daily.get("halt") or ""),
            "halt_reason": str(daily.get("halt_reason") or ""),
            "deployed_usdt": float(daily.get("deployed_usdt") or 0.0),
            "fills": fills,
            "desk": live_desk_status(daily, ""),
        }


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def empty_daily(day: str | None = None) -> dict[str, Any]:
    return {
        "date": day or utc_today(),
        "entries": 0,
        "wins": 0,
        "losses": 0,
        "sl_hits": 0,
        "deployed_usdt": 0.0,
        "realized_pnl_usdt": 0.0,
        "halt": "",
        "halt_reason": "",
        "fills": [],
    }


def daily_block_reason(daily: dict[str, Any] | None) -> str:
    """Hard stand-down reason for new entries, or empty if the desk may trade."""
    row = daily if isinstance(daily, dict) else {}
    halt = str(row.get("halt") or "").strip()
    reason = str(row.get("halt_reason") or "").strip()
    if halt:
        return reason or f"VETO: Daily hard stand-down ({halt})"
    try:
        entries = int(row.get("entries") or 0)
    except (TypeError, ValueError):
        entries = 0
    try:
        wins = int(row.get("wins") or 0)
    except (TypeError, ValueError):
        wins = 0
    try:
        sl_hits = int(row.get("sl_hits") or 0)
    except (TypeError, ValueError):
        sl_hits = 0
    if sl_hits >= 1:
        return VETO_REASON_STOP_LOSS_DAY
    if wins >= DAILY_WIN_STREAK:
        return VETO_REASON_WIN_STREAK
    if entries >= DAILY_MAX_ENTRIES:
        return VETO_REASON_DAILY_MAX
    return ""


def remaining_risk_usdt(equity_usdt: float, daily: dict[str, Any] | None) -> float:
    try:
        equity = max(0.0, float(equity_usdt or 0.0))
    except (TypeError, ValueError):
        equity = 0.0
    try:
        deployed = max(0.0, float((daily or {}).get("deployed_usdt") or 0.0))
    except (TypeError, ValueError):
        deployed = 0.0
    return max(0.0, equity * DAILY_RISK_PCT - deployed)


def clip_notional_to_daily_budget(
    *,
    equity_usdt: float | None,
    daily: dict[str, Any] | None,
    requested_notional: float,
    leverage: float,
) -> tuple[float, str]:
    """Cap this entry so the UTC day stays inside 6% of live equity.

    Returns (notional_usdt, veto_reason). Remaining budget is split across
    unused daily slots so four trades cannot stack past the cap.
    """
    if equity_usdt is None:
        return 0.0, VETO_REASON_NO_EQUITY
    try:
        equity = float(equity_usdt)
    except (TypeError, ValueError):
        return 0.0, VETO_REASON_NO_EQUITY
    if equity <= 0:
        return 0.0, VETO_REASON_NO_EQUITY
    try:
        requested = max(0.0, float(requested_notional or 0.0))
    except (TypeError, ValueError):
        requested = 0.0
    try:
        lev = max(1.0, float(leverage or 1.0))
    except (TypeError, ValueError):
        lev = 1.0
    row = daily if isinstance(daily, dict) else {}
    try:
        entries = int(row.get("entries") or 0)
    except (TypeError, ValueError):
        entries = 0
    remaining = remaining_risk_usdt(equity, row)
    slots = max(1, DAILY_MAX_ENTRIES - max(0, entries))
    margin_cap = remaining / float(slots)
    notional_cap = margin_cap * lev
    sized = min(requested, notional_cap) if requested > 0 else notional_cap
    if sized < 1.0 or margin_cap < 0.25:
        return 0.0, VETO_REASON_DAILY_RISK
    return round(sized, 6), ""


def _coerce_daily(raw: Any) -> dict[str, Any]:
    day = utc_today()
    if not isinstance(raw, dict) or str(raw.get("date") or "") != day:
        return empty_daily(day)
    out = empty_daily(day)
    out.update({k: raw.get(k, out[k]) for k in out})
    out["date"] = day
    try:
        out["entries"] = int(out.get("entries") or 0)
    except (TypeError, ValueError):
        out["entries"] = 0
    try:
        out["wins"] = int(out.get("wins") or 0)
    except (TypeError, ValueError):
        out["wins"] = 0
    try:
        out["losses"] = int(out.get("losses") or 0)
    except (TypeError, ValueError):
        out["losses"] = 0
    try:
        out["sl_hits"] = int(out.get("sl_hits") or 0)
    except (TypeError, ValueError):
        out["sl_hits"] = 0
    try:
        out["deployed_usdt"] = float(out.get("deployed_usdt") or 0.0)
    except (TypeError, ValueError):
        out["deployed_usdt"] = 0.0
    try:
        out["realized_pnl_usdt"] = float(out.get("realized_pnl_usdt") or 0.0)
    except (TypeError, ValueError):
        out["realized_pnl_usdt"] = 0.0
    out["halt"] = str(out.get("halt") or "")
    out["halt_reason"] = str(out.get("halt_reason") or "")
    fills = out.get("fills")
    out["fills"] = [f for f in fills if isinstance(f, dict)] if isinstance(fills, list) else []
    return out


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"trades": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"trades": []}
    if isinstance(raw, list):
        return {"trades": [row for row in raw if isinstance(row, dict)]}
    if not isinstance(raw, dict):
        return {"trades": []}
    trades = raw.get("trades")
    if not isinstance(trades, list):
        raw["trades"] = []
    else:
        raw["trades"] = [row for row in trades if isinstance(row, dict)]
    return raw


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_trades(path: Path) -> list[dict[str, Any]]:
    return list(_read_payload(path).get("trades") or [])


def _hydrate_from_trade_log() -> list[dict[str, Any]]:
    """First-run bootstrap: map the paper ledger into memory schema."""
    out: list[dict[str, Any]] = []
    for row in _read_trades(TRADE_LOG):
        board = row.get("board") if isinstance(row.get("board"), dict) else {}
        analyst = board.get("analyst") if isinstance(board.get("analyst"), dict) else {}
        risk = board.get("risk") if isinstance(board.get("risk"), dict) else {}
        executive = board.get("executive") if isinstance(board.get("executive"), dict) else {}
        headlines = analyst.get("wire_headlines") if isinstance(analyst.get("wire_headlines"), list) else []
        if not headlines and analyst.get("thesis"):
            headlines = [str(analyst.get("thesis"))]
        out.append(
            {
                "ts": row.get("ts"),
                "news_context": [str(h) for h in headlines if str(h).strip()][:8],
                "decision": {
                    "action": executive.get("action") or row.get("status"),
                    "side": row.get("side") or executive.get("side"),
                    "symbol": row.get("symbol") or executive.get("symbol"),
                    "consensus": executive.get("consensus"),
                    "verdict": risk.get("verdict"),
                    "rsi": risk.get("rsi"),
                    "ta_verdict": risk.get("ta_verdict"),
                    "thesis": _clip(analyst.get("thesis"), 400),
                    "model": str(executive.get("model") or analyst.get("model") or ""),
                },
                "result": {
                    "ok": bool(row.get("ok")),
                    "status": str(row.get("status") or ""),
                    "order_id": row.get("order_id"),
                    "error": _clip(row.get("error"), 240),
                },
            }
        )
    return out


def live_desk_status(daily: dict[str, Any] | None, action: str = "") -> str:
    """ARMED when the desk may still take risk; STAND_DOWN on halt or last chair action."""
    if daily_block_reason(daily):
        return "STAND_DOWN"
    act = str(action or "").strip().upper().replace(" ", "_")
    if act == "STAND_DOWN":
        return "STAND_DOWN"
    if act == "EXECUTE":
        return "ARMED"
    return "ARMED"


def sentiment_label(score: Any, *, scored: bool = True) -> str:
    if not scored or score is None or score == "":
        return "UNSCANNED"
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "UNSCANNED"
    if value >= 58:
        return "BULL"
    if value <= 42:
        return "BEAR"
    return "NEUTRAL"


def build_engine_snapshot(
    *,
    news_context: list[str] | None = None,
    brief: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    daily: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map live ORACLE/SENTINEL/CHAIRMAN objects onto the persisted snapshot."""
    brief_d = brief if isinstance(brief, dict) else {}
    risk_d = risk if isinstance(risk, dict) else {}
    decision_d = decision if isinstance(decision, dict) else {}
    result_d = result if isinstance(result, dict) else {}
    headlines = [
        str(h)
        for h in (news_context or brief_d.get("wire_headlines") or [])
        if str(h).strip()
    ][:8]
    action = str(decision_d.get("action") or "")
    sentiment = _opt_float(brief_d.get("sentiment_score"), decision_d.get("sentiment"))
    conviction = _opt_int(brief_d.get("conviction"), decision_d.get("conviction"))
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "scored": True,
        "action": action,
        "desk": live_desk_status(daily, action),
        "sentiment": sentiment,
        "conviction": conviction if conviction is not None else 0,
        "sentiment_label": sentiment_label(sentiment, scored=sentiment is not None),
        "oracle_side": str(brief_d.get("side") or decision_d.get("side") or "none"),
        "oracle_symbol": str(brief_d.get("primary_symbol") or decision_d.get("symbol") or "NONE"),
        "oracle_thesis": _clip(brief_d.get("thesis") or decision_d.get("thesis"), 400),
        "news_good": _clip(brief_d.get("news_good"), 240),
        "news_bad": _clip(brief_d.get("news_bad"), 240),
        "news_conflict": bool(brief_d.get("news_conflict")),
        "news_credibility": _opt_float(brief_d.get("news_credibility")),
        "news_impact": str(brief_d.get("news_impact") or ""),
        "headlines": headlines,
        "sentinel_verdict": str(risk_d.get("verdict") or decision_d.get("verdict") or ""),
        "sentinel_rationale": _clip(risk_d.get("rationale"), 400),
        "rsi": _opt_float(risk_d.get("rsi"), decision_d.get("rsi")),
        "ta_verdict": str(risk_d.get("ta_verdict") or decision_d.get("ta_verdict") or ""),
        "setup_score": _opt_float(risk_d.get("setup_score")),
        "setup_threshold": _opt_float(risk_d.get("setup_threshold")),
        "mtf_align": str(risk_d.get("mtf_align") or ""),
        "daily_halt": str(risk_d.get("daily_halt") or (daily or {}).get("halt") or ""),
        "model": str(
            decision_d.get("model") or brief_d.get("model") or risk_d.get("model") or ""
        ),
        "result_status": str(result_d.get("status") or ""),
    }


def _snapshot_from_trade(row: dict[str, Any], daily: dict[str, Any] | None) -> dict[str, Any]:
    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    news = row.get("news_context") if isinstance(row.get("news_context"), list) else []
    headlines = [str(h) for h in news if str(h).strip()][:8]
    sentiment = _opt_float(decision.get("sentiment"))
    conviction = _opt_int(decision.get("conviction"))
    scored = sentiment is not None or conviction is not None
    if sentiment is None and headlines:
        wire = _score_headlines(headlines)
        if wire.get("scored"):
            sentiment = _opt_float(wire.get("sentiment"))
            scored = True
            if conviction is None:
                conviction = _opt_int(wire.get("conviction"))
    action = str(decision.get("action") or "")
    return {
        "ts": str(row.get("ts") or ""),
        "scored": scored,
        "action": action,
        "desk": live_desk_status(daily, action),
        "sentiment": sentiment,
        "conviction": conviction,
        "sentiment_label": sentiment_label(sentiment, scored=scored and sentiment is not None),
        "oracle_side": str(decision.get("side") or "none"),
        "oracle_symbol": str(decision.get("symbol") or "NONE"),
        "oracle_thesis": _clip(decision.get("thesis"), 400),
        "news_good": _clip(decision.get("news_good"), 240),
        "news_bad": _clip(decision.get("news_bad"), 240),
        "news_conflict": bool(decision.get("news_conflict")),
        "news_credibility": _opt_float(decision.get("news_credibility")),
        "news_impact": str(decision.get("news_impact") or ""),
        "headlines": headlines,
        "sentinel_verdict": str(decision.get("verdict") or ""),
        "sentinel_rationale": _clip(result.get("error"), 400),
        "rsi": _opt_float(decision.get("rsi")),
        "ta_verdict": str(decision.get("ta_verdict") or ""),
        "daily_halt": str((daily or {}).get("halt") or ""),
        "model": str(decision.get("model") or ""),
        "result_status": str(result.get("status") or ""),
    }


def _score_headlines(headlines: list[str]) -> dict[str, Any]:
    try:
        from core.news import score_wire

        return score_wire([{"headline": h} for h in headlines if h])
    except Exception:
        return {"scored": False}


def _sum_fill_pnl(fills: list[dict[str, Any]]) -> float:
    total = 0.0
    for fill in fills:
        if "pnl_usdt" not in fill:
            continue
        try:
            total += float(fill.get("pnl_usdt") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(total, 6)


def _opt_float(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed == parsed and parsed not in {float("inf"), float("-inf")}:
            return parsed
    return None


def _opt_int(*values: Any) -> int | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _clip(value: Any, n: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _format_row(row: dict[str, Any]) -> str:
    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    news = row.get("news_context") if isinstance(row.get("news_context"), list) else []
    headlines = "; ".join(str(h) for h in news[:3] if h) or "(no headlines)"
    status = result.get("status") or ("ok" if result.get("ok") else "unknown")
    err = result.get("error")
    err_bit = f" error={err}" if err else ""
    rsi = decision.get("rsi")
    try:
        rsi_bit = f"{float(rsi):.2f}" if rsi is not None and rsi != "" else "n/a"
    except (TypeError, ValueError):
        rsi_bit = "n/a"
    ta_bit = decision.get("ta_verdict") or "SKIPPED"
    return (
        f"ts={row.get('ts') or '?'} "
        f"news=[{headlines}] "
        f"action={decision.get('action')} "
        f"side={decision.get('side')} "
        f"symbol={decision.get('symbol')} "
        f"verdict={decision.get('verdict')} "
        f"rsi={rsi_bit} "
        f"ta={ta_bit} "
        f"result={status}{err_bit}"
    )
