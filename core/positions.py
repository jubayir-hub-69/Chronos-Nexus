"""Open-book desk: trailing exits, thesis invalidation, and close accounting."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT
from core.schemas import AnalystBrief
from connectors.bitget_paper import (
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    BitgetPaperConnector,
    coerce_price,
    unrealized_pnl,
)

DESK_PATH = PROJECT_ROOT / "data" / "desk.json"
TRAIL_ARM_PCT = 3.0
TRAIL_GIVEBACK_PCT = 1.0
PARTIAL_ARM_PCT = 4.0
PARTIAL_FRACTION = 0.5
FLIP_CONVICTION = 55
_LOCK = threading.Lock()


class PositionDesk:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DESK_PATH

    def snapshot(self, bitget: BitgetPaperConnector | None) -> list[dict[str, Any]]:
        if bitget is None:
            return []
        try:
            book = bitget.fetch_open_book()
        except Exception as exc:
            print(f"[DESK] fetch_open_book failed: {exc}", flush=True)
            return []
        live = [row for row in book if row.get("open") and row.get("symbol")]
        stored = self._load()
        for row in live:
            meta = stored.get(_key(row["symbol"], row.get("side"))) or stored.get(
                ticker_root(str(row["symbol"]))
            ) or {}
            if meta.get("sl_price"):
                row["sl_price"] = meta["sl_price"]
            if meta.get("tp_price"):
                row["tp_price"] = meta["tp_price"]
            if meta.get("thesis"):
                row["thesis"] = meta["thesis"]
            row["high_pnl_pct"] = float(meta.get("high_pnl_pct") or row.get("pnl_pct") or 0.0)
        return live

    def record_open(self, order: dict[str, Any], brief: AnalystBrief | None = None) -> None:
        if not order or not order.get("ok"):
            return
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "buy")
        if not symbol:
            return
        key = _key(symbol, side)
        with _LOCK:
            payload = self._read()
            rows = payload.get("positions") if isinstance(payload.get("positions"), dict) else {}
            rows[key] = {
                "symbol": symbol,
                "side": side,
                "entry_price": coerce_price(order.get("entry_price"), order.get("price")),
                "qty": float(order.get("amount") or order.get("quantity") or 0.0),
                "sl_price": order.get("sl_price"),
                "tp_price": order.get("tp_price"),
                "thesis": (brief.thesis if brief is not None else "") or "",
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "order_id": order.get("order_id"),
                "high_pnl_pct": 0.0,
                "partial_taken": False,
            }
            payload["positions"] = rows
            payload["updated"] = datetime.now(timezone.utc).isoformat()
            self._write(payload)

    def drop(self, symbol: str, side: str = "") -> None:
        with _LOCK:
            payload = self._read()
            rows = payload.get("positions") if isinstance(payload.get("positions"), dict) else {}
            keys = [k for k in list(rows) if _same_symbol(k, symbol, side)]
            for key in keys:
                rows.pop(key, None)
            payload["positions"] = rows
            payload["updated"] = datetime.now(timezone.utc).isoformat()
            self._write(payload)

    def manage(
        self,
        bitget: BitgetPaperConnector | None,
        brief: AnalystBrief | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate live book. Close / partial-close when thesis or trail fires.

        Never opens a position.
        """
        actions: list[dict[str, Any]] = []
        if bitget is None:
            return actions
        live = self.snapshot(bitget)
        stored = self._load()
        live_keys = {_key(str(p["symbol"]), p.get("side")) for p in live}

        for key, meta in list(stored.items()):
            if key in live_keys:
                continue
            vanished = self._classify_vanished(bitget, meta)
            actions.append(vanished)
            self.drop(str(meta.get("symbol") or key), str(meta.get("side") or ""))

        for pos in live:
            symbol = str(pos.get("symbol") or "")
            side = str(pos.get("side") or "buy")
            key = _key(symbol, side)
            meta = stored.get(key) or {}
            pnl_pct = float(pos.get("pnl_pct") or 0.0)
            high = max(float(meta.get("high_pnl_pct") or 0.0), pnl_pct)
            self._touch_high(key, high)

            reason = _exit_reason(pos, brief, meta, high)
            if not reason:
                continue
            fraction = PARTIAL_FRACTION if reason.startswith("PARTIAL") else 1.0
            try:
                result = bitget.close_market(
                    symbol,
                    fraction=fraction,
                    reason=reason,
                    side=side,
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "ERROR",
                    "symbol": symbol,
                    "error": str(exc)[:240],
                    "pnl_usdt": pos.get("pnl_usdt") or 0.0,
                    "pnl_pct": pnl_pct,
                }
            result["reason"] = reason
            result["kind"] = _kind(reason)
            result["side"] = side
            result["mark_price"] = pos.get("mark_price")
            result["entry_price"] = pos.get("entry_price")
            actions.append(result)
            if result.get("ok") and fraction >= 0.999:
                self.drop(symbol, side)
            elif result.get("ok"):
                self._mark_partial(key)
        return actions

    def _classify_vanished(
        self, bitget: BitgetPaperConnector, meta: dict[str, Any]
    ) -> dict[str, Any]:
        symbol = str(meta.get("symbol") or "")
        side = str(meta.get("side") or "buy")
        entry = coerce_price(meta.get("entry_price"))
        sl = coerce_price(meta.get("sl_price"))
        tp = coerce_price(meta.get("tp_price"))
        mark = 0.0
        try:
            ticker = bitget.fetch_ticker(symbol)
            mark = coerce_price(ticker.get("last"), ticker.get("bid"), ticker.get("ask"), entry)
        except Exception:
            mark = entry
        qty = float(meta.get("qty") or 0.0)
        pnl_usdt, pnl_pct = unrealized_pnl(entry, mark, qty, side)
        kind = "EXCHANGE_CLOSE"
        if sl > 0 and _hit_level(side, mark, sl, stop=True):
            kind = "SL"
        elif tp > 0 and _hit_level(side, mark, tp, stop=False):
            kind = "TP"
        return {
            "ok": True,
            "status": kind,
            "symbol": symbol,
            "side": side,
            "reason": f"exchange {kind}",
            "kind": kind,
            "pnl_usdt": pnl_usdt,
            "pnl_pct": pnl_pct,
            "entry_price": entry,
            "mark_price": mark,
            "inferred": True,
        }

    def _load(self) -> dict[str, dict[str, Any]]:
        payload = self._read()
        rows = payload.get("positions") if isinstance(payload.get("positions"), dict) else {}
        return {str(k): v for k, v in rows.items() if isinstance(v, dict)}

    def _touch_high(self, key: str, high: float) -> None:
        with _LOCK:
            payload = self._read()
            rows = payload.get("positions") if isinstance(payload.get("positions"), dict) else {}
            row = rows.get(key)
            if isinstance(row, dict):
                row["high_pnl_pct"] = max(float(row.get("high_pnl_pct") or 0.0), float(high))
                rows[key] = row
                payload["positions"] = rows
                self._write(payload)

    def _mark_partial(self, key: str) -> None:
        with _LOCK:
            payload = self._read()
            rows = payload.get("positions") if isinstance(payload.get("positions"), dict) else {}
            row = rows.get(key)
            if isinstance(row, dict):
                row["partial_taken"] = True
                rows[key] = row
                payload["positions"] = rows
                self._write(payload)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"positions": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"positions": {}}
        if not isinstance(payload, dict):
            return {"positions": {}}
        if "positions" not in payload or not isinstance(payload.get("positions"), dict):
            payload["positions"] = {}
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def ticker_root(symbol: str) -> str:
    base = (symbol or "").split(":")[0].split("/")[0].upper()
    if base.startswith("R") and len(base) > 2 and base[1:].isalpha():
        base = base[1:]
    return "GOOG" if base == "GOOGL" else base


def format_book_lines(book: list[dict[str, Any]]) -> list[str]:
    if not book:
        return ["FLAT — no open Demo positions."]
    lines: list[str] = []
    for pos in book:
        pnl_pct = float(pos.get("pnl_pct") or 0.0)
        pnl_usdt = float(pos.get("pnl_usdt") or 0.0)
        sign = "+" if pnl_pct >= 0 else ""
        usd = "+" if pnl_usdt >= 0 else ""
        lines.append(
            f"{pos.get('symbol')}  {str(pos.get('side') or '').upper()}  "
            f"qty={pos.get('contracts')}  "
            f"entry={pos.get('entry_price')}  mark={pos.get('mark_price')}  "
            f"PnL {sign}{pnl_pct:.2f}%  {usd}{pnl_usdt:.2f} USDT"
        )
    return lines


def _exit_reason(
    pos: dict[str, Any],
    brief: AnalystBrief | None,
    meta: dict[str, Any],
    high: float,
) -> str:
    pnl_pct = float(pos.get("pnl_pct") or 0.0)
    side = str(pos.get("side") or "buy")
    mark = coerce_price(pos.get("mark_price"))
    sl = coerce_price(pos.get("sl_price"), meta.get("sl_price"))
    tp = coerce_price(pos.get("tp_price"), meta.get("tp_price"))
    if sl > 0 and _hit_level(side, mark, sl, stop=True):
        return "SL"
    if tp > 0 and _hit_level(side, mark, tp, stop=False):
        return "TP"
    if pnl_pct <= -STOP_LOSS_PCT * 100.0:
        return "SL"
    if pnl_pct >= TAKE_PROFIT_PCT * 100.0:
        return "TP"
    invalid = _thesis_invalidated(pos, brief)
    if invalid:
        return f"THESIS {invalid}"
    if (
        pnl_pct >= PARTIAL_ARM_PCT
        and not bool(meta.get("partial_taken"))
        and pnl_pct < TAKE_PROFIT_PCT * 100.0
    ):
        return "PARTIAL_TP"
    if high >= TRAIL_ARM_PCT and (high - pnl_pct) >= TRAIL_GIVEBACK_PCT:
        return "TRAIL"
    return ""


def _thesis_invalidated(pos: dict[str, Any], brief: AnalystBrief | None) -> str:
    if brief is None:
        return ""
    root = ticker_root(str(pos.get("symbol") or ""))
    if not root:
        return ""
    for item in brief.stay_away or []:
        if root in str(item).upper():
            return f"stay_away {root}"
    news_bad = str(brief.news_bad or "").upper()
    if root in news_bad and int(brief.conviction or 0) >= 40:
        return f"toxic tape {root}"
    pos_side = str(pos.get("side") or "buy")
    brief_root = ticker_root(brief.primary_symbol or "")
    if brief_root == root and brief.side in {"buy", "sell"}:
        if _opposite(brief.side, pos_side) and int(brief.conviction or 0) >= FLIP_CONVICTION:
            return "ORACLE flipped"
    return ""


def _opposite(a: str, b: str) -> bool:
    left = str(a).lower()
    right = str(b).lower()
    longs = {"buy", "long"}
    shorts = {"sell", "short"}
    return (left in longs and right in shorts) or (left in shorts and right in longs)


def _hit_level(side: str, mark: float, level: float, *, stop: bool) -> bool:
    if mark <= 0 or level <= 0:
        return False
    short = str(side).lower() in {"sell", "short"}
    tol = abs(level) * 0.001
    if stop:
        return mark >= level - tol if short else mark <= level + tol
    return mark <= level + tol if short else mark >= level - tol


def _kind(reason: str) -> str:
    raw = (reason or "").upper()
    if raw.startswith("SL") or " SL" in f" {raw}":
        return "SL"
    if "PARTIAL" in raw:
        return "PARTIAL"
    if raw.startswith("TP") or "TP" in raw:
        return "TP"
    if "TRAIL" in raw:
        return "TRAIL"
    if "THESIS" in raw:
        return "THESIS"
    if "MANUAL" in raw or "CLOSEALL" in raw or "CLOSE" in raw:
        return "MANUAL"
    return "CLOSE"


def _key(symbol: str, side: Any = "") -> str:
    return f"{symbol}|{str(side or 'buy').lower()}"


def _same_symbol(key: str, symbol: str, side: str) -> bool:
    if key == _key(symbol, side):
        return True
    stored_sym = key.split("|")[0]
    return ticker_root(stored_sym) == ticker_root(symbol)
