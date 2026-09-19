"""Shared contracts between the Board of Directors and the rails."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

GapBias = Literal["GAP_UP", "GAP_DOWN", "MIXED", "FADE"]
Side = Literal["buy", "sell", "none"]
RiskVerdict = Literal["CLEAR", "REDUCE", "VETO"]
BoardAction = Literal["EXECUTE", "STAND_DOWN"]
Consensus = Literal["UNANIMOUS", "MAJORITY", "VETOED", "DEGRADED"]


class WeekendTrigger(BaseModel):
    id: str
    category: str
    headline: str
    detail: str
    rtoken_map: list[str] = Field(default_factory=list)
    source: str = ""
    link: str = ""
    published: str = ""


class AnalystBrief(BaseModel):
    callsign: str = "ORACLE"
    thesis: str
    monday_gap_bias: GapBias = "MIXED"
    primary_symbol: str = "NONE"
    side: Side = "none"
    conviction: int = Field(ge=0, le=100, default=0)
    horizon: str = "weekend_to_monday_open"
    rationale: str
    affected_tickers: list[str] = Field(default_factory=list)
    wire_headlines: list[str] = Field(default_factory=list)
    news_good: str = ""
    news_bad: str = ""
    stay_away: list[str] = Field(default_factory=list)
    selection_reason: str = ""
    sentiment_score: float = 50.0
    news_credibility: float = 0.5
    news_conflict: bool = False
    news_impact: str = "low"
    llm_degraded: bool = False
    model: str = ""


class RiskReport(BaseModel):
    callsign: str = "SENTINEL"
    verdict: RiskVerdict = "VETO"
    fake_news_risk: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    black_swan_flags: list[str] = Field(default_factory=list)
    max_notional_usdt: float = 15.0
    size_multiplier: float = 1.0
    rationale: str
    spread_pct: float | None = None
    rsi: float | None = None
    rsi_timeframe: str = ""
    rsi_period: int = 14
    ta_verdict: Literal["PASS", "VETO", "SKIPPED"] = "SKIPPED"
    asset_risk_score: float = 50.0
    sl_margin_frac: float = 0.75
    margin_usdt: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    candle_structure: str = ""
    candle_bias: str = ""
    candle_pattern: str = ""
    volatility: str = ""
    market_cap_usdt: float | None = None
    total_supply: float | None = None
    last_price: float | None = None
    fund_ok: bool = False
    daily_halt: str = ""
    equity_usdt: float | None = None
    daily_budget_usdt: float | None = None
    daily_deployed_usdt: float | None = None
    daily_entries: int = 0
    setup_score: float = 0.0
    setup_threshold: float = 90.0
    mtf_align: str = ""
    book_imbalance: float | None = None
    rvol: float | None = None
    vwap_dev_pct: float | None = None
    pullback_ok: bool = False
    sentiment_score: float | None = None
    llm_degraded: bool = False
    model: str = ""


class BoardDecision(BaseModel):
    callsign: str = "CHAIRMAN"
    action: BoardAction = "STAND_DOWN"
    consensus: Consensus = "VETOED"
    symbol: str
    side: Side = "none"
    amount: float = 0.0
    notional_usdt: float = 0.0
    reasoning: str
    reasoning_hash: str = ""
    llm_degraded: bool = False
    model: str = ""


class AttestationResult(BaseModel):
    ok: bool
    skipped: bool = False
    tx_hash: str | None = None
    explorer_url: str | None = None
    from_address: str | None = None
    chain_id: int = 421614
    reason: str = ""


class PaperOrderResult(BaseModel):
    ok: bool
    sandbox: bool = True
    symbol: str
    side: str
    amount: float
    order_id: str | None = None
    status: str
    ticker: dict[str, Any] = Field(default_factory=dict)
    raw_order: dict[str, Any] = Field(default_factory=dict)
    reasoning_hash: str = ""
    log_path: str = ""
    error: str = ""
