"""Lightweight board memory — last N paper cycles on disk.

ORACLE and CHAIRMAN inject this into Gemini so the desk does not
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
        """Compact recap for Gemini context. Empty memory is stated explicitly."""
        rows = self.recent()
        if not rows:
            return "BOARD MEMORY: empty (no prior cycles)."
        lines = [
            f"BOARD MEMORY (last {len(rows)} cycles — do not repeat identical failed setups):"
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
                "thesis": _clip(decision.get("thesis"), 400),
                "model": str(decision.get("model") or ""),
            },
            "result": {
                "ok": bool(result.get("ok")),
                "status": str(result.get("status") or ""),
                "order_id": result.get("order_id"),
                "error": _clip(result.get("error"), 240),
            },
        }
        with _LOCK:
            trades = _read_trades(self.path)
            if not trades and self.path == HISTORY_PATH:
                trades = _hydrate_from_trade_log()
            trades.append(entry)
            trades = trades[-self.max_trades :]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"updated": entry["ts"], "max_trades": self.max_trades, "trades": trades}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        return self.path


def _read_trades(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    trades = payload.get("trades") if isinstance(payload, dict) else payload
    if not isinstance(trades, list):
        return []
    return [row for row in trades if isinstance(row, dict)]


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
    return (
        f"ts={row.get('ts') or '?'} "
        f"news=[{headlines}] "
        f"action={decision.get('action')} "
        f"side={decision.get('side')} "
        f"symbol={decision.get('symbol')} "
        f"verdict={decision.get('verdict')} "
        f"result={status}{err_bit}"
    )
