# CHRONOS-NEXUS

```text
 ██████╗██╗  ██╗██████╗  ██████╗ ███╗   ██╗ ██████╗ ███████╗
██╔════╝██║  ██║██╔══██╗██╔═══██╗████╗  ██║██╔═══██╗██╔════╝
██║     ███████║██████╔╝██║   ██║██╔██╗ ██║██║   ██║███████╗
██║     ██╔══██║██╔══██╗██║   ██║██║╚██╗██║██║   ██║╚════██║
╚██████╗██║  ██║██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝███████╗
 ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚══════╝ ╚══════╝
███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
```

### Wall Street sleeps. The Nexus does not.

**Event-Driven Agent** for **news-driven directional trading** on tokenized US equities (rTokens / stock perps)

Bitget AI Base Camp Hackathon S2 · Track: **Agentic Trading** · Sub-theme: **Event-Driven Agent** · Mode: **Paper / Demo only** · Version: **0.6.0-glasshouse**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Bitget Qwen 3.8 Max](https://img.shields.io/badge/Bitget-Hackathon%20Qwen%203.8%20Max-00C3A5?style=for-the-badge)](https://bitget-ai.gitbook.io/bitgetai_hackathons2#qwen-token-subsidy-during-the-hackathon)
[![Bitget Paper Trading](https://img.shields.io/badge/Bitget-Paper%20Trading%20%7C%20PAPTRADING%3D1-00C3A5?style=for-the-badge)](https://www.bitget.com/api-doc/classic/demotrading/restapi)
[![Arbitrum Sepolia](https://img.shields.io/badge/Arbitrum-Sepolia%20421614-28A0F0?style=for-the-badge)](https://sepolia.arbiscan.io/)
[![Telegram Glasshouse](https://img.shields.io/badge/Telegram-Glasshouse%20Observability-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Live RSS](https://img.shields.io/badge/Wire-Yahoo%20%2B%20CNBC%20%2B%20MarketWatch-720E9E?style=for-the-badge)](https://finance.yahoo.com/news/rssindex)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](#license)

Cash US equities close. Macro does not. Tokenized names keep trading 24/7 on Bitget while NYSE / NASDAQ are dark. **Chronos-Nexus** is a three-agent Board of Directors that runs as an **unattended hourly daemon**: it **reads the live global wire**, **maps a headline onto a listed Demo equity**, **stress-tests rumor quality and L2 spread**, **anchors the decision on Arbitrum Sepolia**, and **dispatches a Bitget Demo paper order** — or it **stands down to `NONE`**.

This is **not** spread capture. The LLM does not mint/redeem NAV, cross-list a basis, or harvest a quote discrepancy. It takes a **directional** paper position when a macro / earnings / policy event on the live tape maps onto a tradable rToken or stock perp — and SENTINEL may veto it.

The LLM is the decision-maker, not a chatbot. SENTINEL holds a binding veto. No live capital is reachable from this tree. **Zero dummy data. Zero hardcoded five-name book. Zero silent hangs. Zero assumed BUY.**

---

## Hackathon identity (GitBook Chapter IV)

| Field | Value |
|---|---|
| **Track** | Agentic Trading |
| **Sub-theme** | Event-Driven Agent |
| **What we built** | News / announcements / macro events → LLM interpretation → risk-cleared directional paper order |
| **What we did not build** | rToken vs native-stock / NAV mint-redeem spread capture |
| **Validation** | Bitget Demo paper trading (`PAPTRADING=1`) during the S2 competition window |
| **Scoring mix** | 50% quantitative + 50% judge (explainability, architecture, risk layer) |

GitBook positioning we implement: *“The LLM is the primary trading decision-maker, not just an assistant. The Agent must sense the environment, make independent judgments, and autonomously place orders with risk controls.”*

---

## What judges should look at first

This is a production-shaped Event-Driven Agent, not a single-shot script. The CIC boots once, then scans the tape every hour until you interrupt it.

| # | Capability | What it actually does |
|---|---|---|
| **1** | **True 24/7 autonomous daemon** | `while True` hourly cycles. No `input("Press Enter")`. Cycle faults sleep 5 minutes and recover. Ctrl+C is the only shutdown. |
| **2** | **Unlimited market universe** | Boot + each cycle call CCXT `load_markets()` and discover **all** Bitget Demo equity / stock-perp / rToken listings. ORACLE may pick any listed name, or `NONE`. |
| **3** | **GitBook paper log** | Every row in `data/logs/trades.json` carries `timestamp`, `instrument`, `direction`, `quantity`, `price`, and `account_balance_change`. `price` is never null. Wallet Δ is live USDT before/after a fill, else simulated margin + taker fee. |
| **4** | **Glasshouse Telegram** | Live HTML alerts: market wire, Stay Away, rationale-backed EXECUTE, exact-reason VETO, API timeout, Qwen quota stand-down. |
| **5** | **Bitget Hackathon - Qwen 3.8 Max** | **45s** Qwen proxy hard timeout. **503 / 429 quota** stand down to `NONE`. The daemon keeps running. |

```
 LIVE RSS (Yahoo / CNBC / MarketWatch / CoinTelegraph)
      │
      ▼
 ORACLE  ·  session clock + open universe + NONE if no tape
      │
      ├─ idle / timeout / 503 / 429 quota ──► STAND_DOWN · Telegram · history.json
      │
      ▼ pick
 SENTINEL  ·  rumor + L2 spread > 1.5% + dark-book fail-safe
      │
      ├─ VETO / position already open ──────► Telegram + trades.json
      │
      ▼ CLEAR / REDUCE
 CHAIRMAN  ·  sha256(board minutes)
      │
      ▼
 Arbitrum Sepolia  ·  value = 0  ·  CHRONOS-NEXUS/v1:{hash}
      │
      ▼
 Bitget Demo  ·  PAPTRADING=1  ·  SL −2%  ·  TP +5%  ·  no stacked book
      │
      ▼
 Telegram EXECUTE (why this name) + Arbiscan proof
 data/logs/trades.json  ·  data/history.json
      │
      ▼
 sleep 3600s  ·  next cycle
```

---

## Elevator pitch

Weekend geopolitics, supply-chain shocks, and sector leaks hit the tape while cash equity is closed. Human desks wait for Sunday night futures. rTokens already moved.

Chronos-Nexus runs that window as an **event → debate → risk clearance → on-chain Proof of Thought → guarded paper execution** loop — **every hour, unattended**, on **Bitget Hackathon - Qwen 3.8 Max**. The trade is a **directional** paper ticket on the name the news actually maps to, not a basis harvest.

| Pain | What the Nexus does |
|---|---|
| Wall Street is closed; rTokens are not | ORACLE prices session / Monday-gap risk on the **live** 24/7 tape, with a real UTC clock |
| A hardcoded five-name book is a toy | The desk **discovers the live Demo universe** and will trade any listed equity — or stand down |
| Weekend “news” is often rumor | SENTINEL scores fake-news / black-swan and may **VETO** or **REDUCE** |
| Wide books eat paper fills | Python **L2 spread veto** if bid–ask **> 1.5%**, or if the book is dark / crossed |
| Agentic trades are unauditable | CHAIRMAN hashes the board minutes and anchors `sha256` on **Arbitrum Sepolia** |
| Unguarded fills | Auto **SL −2% / TP +5%** + **position lock on both longs and shorts** |
| Paper logs fail GitBook review | Every record stamps **timestamp / instrument / direction / quantity / price / account_balance_change** |
| Free-tier daemons “crash” at quota | **429 / Resource Exhausted** → exact Telegram stand-down, daemon keeps running |
| Hackathon / evaluation risk | Bitget **Demo only** (`set_sandbox_mode(True)`, `PAPTRADING=1`) |

**Live E2E (this repo):** Bitget Hackathon - Qwen 3.8 Max (`qwen3.8-max`) · board debate completed · Sepolia attestation mined · Bitget Demo paper orders submitted on listed stock perps. Subsequent live tests confirmed **NONE stand-down** (no NVDA default) when the tape or the model is not actionable.

---

## Part 3: Quantitative Metrics & Agent Architecture (GitBook Compliance)

Agentic Trading is scored **50% quantitative + 50% judge**. GitBook Part 3 (Strategy / Agent) requires test period, returns, Sharpe / Sortino, max drawdown, win rate, turnover, plus fees, slippage, and funding — each figure labeled **observed / estimated / targeted**.

The daemon is in its **competition-window incubation**. Headline numbers below are **not invented**. They are reserved for the live `data/logs/trades.json` run and will be filled as observed.

### Required GitBook metrics

| Metric | Value | Label |
|---|---|---|
| **Test period** | S2 competition window **2026-09-03 → 2026-09-21 (UTC+8)**. Paper log started **2026-09-10**. Incubation continues through the deadline. Recommended GitBook duration ≥ 2 weeks (Observing/Calculating from live trades.json run) | observed window / targeted ≥2 weeks |
| **Returns** | (Observing/Calculating from live trades.json run) | observed |
| **Sharpe** | (Observing/Calculating from live trades.json run) | observed |
| **Sortino** | (Observing/Calculating from live trades.json run) | observed |
| **Max drawdown** | (Observing/Calculating from live trades.json run) | observed |
| **Win rate** | (Observing/Calculating from live trades.json run) | observed |
| **Turnover** | (Observing/Calculating from live trades.json run) | observed |
| **Costs: fees** | Bitget USDT-M taker **6 bps** (`TAKER_FEE_RATE = 0.0006`) applied to every fill notional; live wallet Δ preferred when Demo balance is readable | estimated / observed |
| **Costs: slippage** | L2 bid–ask captured per cycle; **spread > 1.5% is a binding VETO** (not a cost — a refused trade). Fill slippage vs mid = (Observing/Calculating from live trades.json run) | observed / estimated |
| **Costs: funding** | Stock-perp funding on Demo = (Observing/Calculating from live trades.json run). Untaken hours labeled **n/a** until a position is held across a funding timestamp | observed / targeted |

Judge-facing distribution proof (GitBook also asks these; none are live users yet):

| Proof | Value | Label |
|---|---|---|
| Activation | Paper daemon running unattended on Demo | observed |
| Trading volume | Sum of filled `notional_usdt` in `trades.json` | (Observing/Calculating from live trades.json run) |
| AUM | Demo USDT equity snapshot per cycle | (Observing/Calculating from live trades.json run) |
| Retention | Hourly cycles completed / attempted | (Observing/Calculating from live trades.json run) |
| Incremental fee | Sum of estimated taker fees on fills | (Observing/Calculating from live trades.json run) |
| Risk | Binding SENTINEL veto, 1.5% spread kill, SL −2% / TP +5%, position lock, 15 USDT default notional cap | observed |

How the numbers will be computed from `trades.json` (no hand-waving):

- **Returns** — Demo USDT equity path: `Σ account_balance_change` on fills, plus mark-to-market on open stock perps when the log records it.
- **Sharpe / Sortino** — hourly cycle equity returns, sample stdev (Sharpe) and downside stdev (Sortino), annualized with 24 × 365 hourly bars because the desk does not sleep.
- **Max drawdown** — peak-to-trough on the same equity path.
- **Win rate** — closed paper tickets with `account_balance_change > 0` ÷ closed tickets. Vetoes and stand-downs are **not** wins.
- **Turnover** — `Σ \|notional_usdt\|` on fills ÷ average Demo equity.

### Agent architecture (the other 50%)

| Callsign | Agent | File | Mandate |
|---|---|---|---|
| **ORACLE** | Analyst | `agents/analyst.py` | Ingest live RSS. Apply the session clock. Map **any** equity/sector onto the **live Demo universe**, or emit `NONE`. Reads board memory. |
| **SENTINEL** | Risk Manager | `agents/risk_manager.py` | Fake-news / black-swan, **L2 spread > 1.5%**, dark-book fail-safe, concentration, size. Verdicts: **CLEAR** · **REDUCE** · **VETO**. Veto is binding. |
| **CHAIRMAN** | Executive | `agents/executive.py` | Synthesize (memory-aware), lock the action, SHA-256 the minutes, `log_proof_of_thought()`, then `execute_paper_order()` with SL/TP — or `STAND_DOWN`. |

Event → decision → execution (required Agentic Trading demonstration):

1. **Event** — live Yahoo / CNBC / MarketWatch / CoinTelegraph RSS, plus a UTC session clock (open / pre-market / after-hours / overnight / weekend).
2. **Decision** — ORACLE brief (thesis, side, conviction, listed symbol or `NONE`) → SENTINEL CLEAR / REDUCE / VETO → CHAIRMAN `EXECUTE` or `STAND_DOWN`. Python overrides the model on veto, idle, timeout, and 429.
3. **Execution** — zero-value Arbitrum Sepolia attestation of `sha256(board minutes)`, then Bitget Demo market order with SL −2% / TP +5%, logged with GitBook columns.

### Risk contract (non-negotiable)

| Condition | CHAIRMAN action | Size |
|---|---|---|
| SENTINEL `CLEAR` | `EXECUTE` | Full paper notional cap |
| SENTINEL `REDUCE` | `EXECUTE` | `max_notional_usdt × size_multiplier` |
| SENTINEL `VETO` | `STAND_DOWN` | Zero — Python overrides the model |
| ORACLE idle (`NONE` / `none` / conviction 0) | `STAND_DOWN` | Zero — not a risk veto, a clean pass |
| API timeout / 503 / parser fault | `STAND_DOWN` | Zero — degraded closed |
| Qwen **429 quota** | `STAND_DOWN` | Zero — wait for reset, daemon stays up |
| No live last price | `STAND_DOWN` | Zero |

```mermaid
flowchart LR
    R[Live RSS] --> O[ORACLE]
    M[Board Memory · last 5] --> O
    Ck[UTC session clock] --> O
    U[CCXT load_markets universe] --> O
    O -->|NONE / timeout / 429 quota| SD[STAND_DOWN · Telegram]
    O -->|listed pick| S[SENTINEL]
    S -->|spread / rumor / dark book| V[VETO · Telegram]
    S -->|CLEAR / REDUCE| C[CHAIRMAN]
    C --> H[sha256 board minutes]
    H --> A[Arbitrum Sepolia · value = 0]
    A --> P{Position already open?}
    P -->|yes| K[Skip · Telegram]
    P -->|no| B[Bitget Demo · SL -2% · TP +5%]
    B --> T[Telegram EXECUTE + why]
    SD --> L[trades.json + history.json]
    V --> L
    K --> L
    T --> L
    L --> Z[sleep 3600s]
    Z --> R
```

---

## Table of contents

1. [Hackathon identity](#hackathon-identity-gitbook-chapter-iv)
2. [Part 3: Quantitative Metrics & Agent Architecture (GitBook Compliance)](#part-3-quantitative-metrics--agent-architecture-gitbook-compliance)
3. [True 24/7 autonomous daemon](#1-true-247-autonomous-daemon)
4. [Unlimited market universe](#2-unlimited-market-universe)
5. [Glasshouse Telegram observability](#3-glasshouse-telegram-observability)
6. [Bulletproof resilience & Qwen quota](#4-bulletproof-resilience--qwen-quota)
7. [Testnet / paper-trading compliance](#5-testnet--paper-trading-compliance)
8. [Quickstart](#6-quickstart--setup)
9. [Verifiable audit trail](#7-verifiable-audit-trail--on-chain-proofs)
10. [Production migration](#8-production--mainnet-migration-guide)
11. [Repository map](#9-repository-map)
12. [License](#license)

---

## 1. True 24/7 autonomous daemon

`main.py` is a CIC, not a one-shot demo.

- **Phases 0–2** (compliance, Bitget Hackathon - Qwen 3.8 Max cortex, Bitget + Sepolia rails) boot **once**.
- **Phases 3–5** (live ingest → board debate → attest + paper execute) run inside `while True`.
- After each cycle: `time.sleep(3600)` — hourly trading.
- Any cycle exception: log, Telegram API-fault alert, `time.sleep(300)`, continue. The process does not die.
- There is **no** `input("Press Enter to shut down")`. Stop the desk with **Ctrl+C**.

That is the shape of a 7-day unattended evaluation, not a laptop script that waits for a keypress.

---

## 2. Unlimited market universe

ORACLE is not locked to AAPL / NVDA / MSFT / GOOG / TSLA.

At boot and on every cycle, `connectors/bitget_paper.py` calls CCXT `exchange.load_markets()` and classifies **every** Demo listing that looks like:

- a stock perp (`SUSDT-FUTURES` / equity product tags)
- an rToken (`rAAPL`, `rNVDA`, …)
- a US-equity-like USDT swap

ORACLE receives that live catalog. `primary_symbol` must be copied **exactly** from it, or it must be **`NONE`**. Python snaps aliases (`rAAPL`, `GOOGL`, `AAPL/USDT`) onto a listed market. Off-list garbage, timeouts, empty tape, and quota faults all become `NONE` — **never a blind NVDA or an assumed BUY**.

The live RSS wire is global, not a five-name keyword toy:

| Source | Role |
|---|---|
| **Yahoo Finance** | Broad equity / macro tape |
| **CNBC** | Institutional headlines |
| **MarketWatch** | Top stories |
| **CoinTelegraph** | Crypto spillover into tokenized names |

Each brief injects a **session clock**: exact UTC timestamp, weekday, US/Eastern time, and cash session (open / pre-market / after-hours / overnight / **weekend**). Friday close is not Monday open.

---

## 3. Glasshouse Telegram observability

The bot is a live terminal, not a fill ping. Disarmed if `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is empty — the CIC still runs. Armed, every material action is HTML-escaped and fire-and-forget (`utils/notifier.py`). A Telegram fault never raises into the board.

| Event | What you receive |
|---|---|
| **System startup** | Glasshouse desk is ONLINE (synchronous boot ping — credentials proven before cycle 1) |
| **Market wire** | Session clock, Demo universe size, **what's working**, **what's hurting**, ranked headlines |
| **Stay Away** | Dedicated warning when the tape is toxic (scandal, earnings miss, crash, fraud, guidance cut, lawsuit) with a short rationale |
| **EXECUTE** | Symbol, side, amount, SL −2%, TP +5%, order id, Arbiscan proof, and **why this name beat the rest of the tape** |
| **VETO / skipped** | Exact reason (spread, crossed book, position already open, margin, venue error) |
| **API timeout / 503** | Parser-safe stand-down: `primary_symbol=NONE`, `side=none`, `conviction=0` |
| **Qwen quota (429)** | Exact text: *Bitget Qwen API quota reached. System safely standing down until limits reset.* |

---

## 4. Bulletproof resilience & Qwen quota

The daemon is designed to live on **Bitget Hackathon - Qwen 3.8 Max** for a multi-day eval. Quota is not a crash. It is a weather report.

| Fault | Behaviour |
|---|---|
| Qwen proxy hung | **45s** hard cap (`LLM_TIMEOUT_S`) via SDK timeout + daemon-thread join |
| Truncated JSON / unescaped quotes in RSS-echoed strings | `JSONDecodeError` caught as **parser fault** → fallback dict, no `Unterminated string` bleed |
| **503 UNAVAILABLE** (high demand) | Stand down to `NONE` / `none` / `0` |
| **429 Resource Exhausted / quota exceeded** | Rationale **exactly**: `Bitget Qwen API quota reached. System safely standing down until limits reset.` Telegram broadcasts that sentence. Hourly loop keeps running. |
| No actionable listed name | Same idle path — never default to NVDA, never assume BUY |
| Missing / dark L2 book | Fail-safe **illiquid veto** (unmeasured spread is not “fine”) |
| Spread **> 1.5%** | Binding `Illiquid Market / High Spread` |
| Open long **or** short | `POSITION_ALREADY_OPEN` — no stacked book |

ORACLE, SENTINEL, and CHAIRMAN **skip further Bitget Hackathon - Qwen 3.8 Max calls** once quota or timeout is latched, so a rate-limit window is not burned by three agents retrying the same 429.

External rails sit behind exponential backoff (`core/retry.py`: 3 attempts, 0.75s base, 6s cap). Auth failures and insufficient margin are **not** retried.

### Bitget Hackathon - Qwen 3.8 Max model

`QWEN_MODEL` is read from `.env`. If empty or `auto`, the cortex falls back to `qwen3.8-max`. The client is the standard `openai` package pointed at `https://hackathon.bitgetops.com/v1` with `QWEN_API_KEY`.

Quota and hard timeouts **stop further model attempts**.

### Cryptographic Proof of Thought

Before the paper order is sent, CHAIRMAN canonicalizes the board minutes and SHA-256s them. `connectors/arbitrum.py` submits a **zero-value self-transaction** on Arbitrum Sepolia whose `input` is:

```text
CHRONOS-NEXUS/v1:{64-char sha256}
```

Anyone can recompute the hash from `trades.json` and match it to the tx calldata on [Sepolia Arbiscan](https://sepolia.arbiscan.io/). Insufficient gas is a skipped attestation, not a crash. Telegram EXECUTE alerts deep-link the explorer URL as **Proof**.

---

## 5. Testnet / paper-trading compliance

This repository is built for **Bitget AI Base Camp Hackathon S2** evaluation. It will not spend real funds.

| Surface | Lock | How it is enforced |
|---|---|---|
| Bitget | Demo / paper only | `BITGET_PAPER_TRADING=true` required. `set_sandbox_mode(True)` is the **first** call after construct. Header `PAPTRADING: 1`. |
| Bitget keys | Demo API key | Create under **Bitget → Demo mode → API Key Management**. Live keys are the wrong environment. |
| Arbitrum | Sepolia, chain ID **421614** | RPC default `https://sepolia-rollup.arbitrum.io/rpc`. `eth_chainId` mismatch aborts attest. |
| Value transfer | None | Proof-of-Thought `value = 0`, `to = from` (self-tx). Payload is calldata only. |
| Process flag | Deadman | `LIVE_TRADING_ENABLED = False` — boot returns `2` if flipped on. |

Evaluators: claim **Bitget Demo virtual USDT** before a fill is possible. An unfunded Demo wallet records `INSUFFICIENT_MARGIN` in `trades.json` rather than failing the loop silently.

---

## 6. Quickstart & setup

### Prerequisites

- Python **3.10+** (verified on 3.12)
- A Bitget hackathon `QWEN_API_KEY` (admin-provisioned Qwen 3.8 Max / `qwen3.8-max`)
- Bitget **Demo** API key + secret + passphrase
- An Arbitrum **Sepolia** account with a small ETH balance (faucet) for 0-value txs
- Virtual Demo USDT claimed on Bitget Demo (required for a fill)
- Optional: Telegram bot token + chat id for glasshouse alerts

### Install

```bash
git clone https://github.com/Jubayir-hub-69/Chronos-Nexus.git
cd Chronos-Nexus
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env   # Windows: copy .env.example .env
```

### Configure `.env`

```dotenv
QWEN_API_KEY=your_bitget_qwen_api_key
QWEN_MODEL=qwen3.8-max

BITGET_API_KEY=your_demo_api_key
BITGET_API_SECRET=your_demo_secret
BITGET_PASSPHRASE=your_demo_passphrase
BITGET_PAPER_TRADING=true
BITGET_SYMBOL=rNVDA/USDT

ARBITRUM_SEPOLIA_RPC=https://sepolia-rollup.arbitrum.io/rpc
ARBITRUM_SEPOLIA_CHAIN_ID=421614
ARBITRUM_PRIVATE_KEY=0xyour_sepolia_private_key

TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

Leave Telegram blank to run headless.

### Run the daemon

```bash
python main.py
```

Expected lifecycle:

1. Kernel / compliance lock
2. Bitget Hackathon - Qwen 3.8 Max cortex (**45s** timeout)
3. Bitget Demo + Arbitrum Sepolia health + **live universe count**
4. Hourly cycle: live RSS → ORACLE → SENTINEL → CHAIRMAN
5. Telegram glasshouse dispatch
6. Sleep **3600s** → repeat

Stop with **Ctrl+C**. Minutes land in `data/logs/trades.json` with GitBook columns. Memory updates `data/history.json`.

### Tests

```bash
python -m unittest discover -s tests -v
```

---

## 7. Verifiable audit trail & on-chain proofs

GitBook required paper-trading columns (every row):

| Column | JSON key | Source |
|---|---|---|
| timestamp | `timestamp` (`ts` kept) | UTC ISO-8601 at write |
| instrument | `instrument` (`symbol` kept) | Demo market id |
| direction | `direction` (`side` kept) | `buy` / `sell` / `none` |
| quantity | `quantity` (`amount` kept) | sized contracts / base |
| price | `price` (`entry_price` kept, never null) | fill average, else live last, else `0.0` on stand-down |
| account balance change | `account_balance_change` | Demo USDT after − before; else `-(margin + taker fee)` on a swap fill; `0.0` on veto |

| File | Role |
|---|---|
| `data/logs/trades.json` | Append-only paper ledger — `EXECUTE`, `VETOED`, `POSITION_ALREADY_OPEN`, `INSUFFICIENT_MARGIN`, `NO_LIVE_PRICE`, idle `STAND_DOWN`, venue `ERROR` |
| `data/history.json` | Last **5** cycles for ORACLE / CHAIRMAN memory |

```text
reasoning_hash = sha256( json.dumps({
    analyst, risk, action, symbol, side, amount, notional_usdt, reasoning
}, sort_keys=True, separators=(',', ':')) )
```

On-chain `input` (hex-decoded ASCII): `CHRONOS-NEXUS/v1:{reasoning_hash}`

### Verified Sepolia attestations (this repo)

| UTC | `reasoning_hash` | Tx | Result |
|---|---|---|---|
| 2026-09-10 07:30 | `0b36683f…e1306cc7` | [`0x1b3ecc83…556fe721`](https://sepolia.arbiscan.io/tx/0x1b3ecc83c316e46dc7b18edb3b86da96c04a57d8af6eb68765542b96556fe721) | Anchored (`value = 0`, chain **421614**) |
| 2026-09-10 07:51 | `a51fd447…d2cda5b6` | [`0xacf4e0ea…fde73b24`](https://sepolia.arbiscan.io/tx/0xacf4e0ea402cc66e03984e307c349094be088acc7b499e6eb0a381aefde73b24) | Anchored, then **Bitget Demo order `1481850718917124096`** on `NVDA/USDT:USDT` |

Attester (Sepolia): [`0x9AFe5CeF11fC10756faef213f7A30D9873B5d372`](https://sepolia.arbiscan.io/address/0x9AFe5CeF11fC10756faef213f7A30D9873B5d372)

---

## 8. Production / mainnet migration guide

**This tree ships with the hackathon deadman on.** Flipping one env var is **not** enough. That is intentional.

Agent prompts, risk contract, hashing, Rich lifecycle, Telegram, board memory, and the Executive sequence stay the same. You are swapping **rails and keys**.

| Gate | File | Today | Mainnet fork |
|---|---|---|---|
| Boot deadman | `main.py` | `LIVE_TRADING_ENABLED = False` | Set `True` only after review |
| Settings assert | `core/config.py` | Refuses `BITGET_PAPER_TRADING=false` | Allow false / split Demo vs live |
| Connector refuse | `connectors/bitget_paper.py` | Requires paper + `set_sandbox_mode(True)` | Construct without sandbox / `PAPTRADING=1` |
| Chain allow-list | `connectors/arbitrum.py` | Expects `421614` | Expect `42161` (Arbitrum One) |

If you are evaluating this hackathon entry, **do not migrate**. Run Demo + Sepolia as shipped.

---

## 9. Repository map

```text
Chronos-Nexus/
├── main.py                      # Rich CIC · hourly Event-Driven Agent (v0.6.0-glasshouse)
├── requirements.txt
├── .env.example
├── agents/
│   ├── analyst.py               # ORACLE — live RSS, open universe, NONE / quota stand-down
│   ├── risk_manager.py          # SENTINEL — veto + L2 spread > 1.5% + dark-book fail-safe
│   └── executive.py             # CHAIRMAN — memory, attest, execute
├── core/
│   ├── config.py                # dotenv, paper lock, Bitget hackathon Qwen 3.8 Max, Telegram
│   ├── llm.py                   # openai → hackathon.bitgetops.com/v1 · 45s timeout · 503/429/parser-fault stand-down
│   ├── memory.py                # last-5 ring buffer → data/history.json
│   ├── retry.py                 # exponential backoff for every rail
│   └── schemas.py               # Pydantic board contracts (side includes none)
├── connectors/
│   ├── bitget_paper.py          # Demo · GitBook paper log · SL/TP · long+short lock
│   └── arbitrum.py              # Sepolia Proof of Thought
├── utils/
│   └── notifier.py              # Glasshouse Telegram (wire / stay-away / execute / quota)
├── data/
│   ├── history.json
│   └── logs/trades.json         # timestamp, instrument, direction, quantity, price, Δ
└── tests/
    ├── test_phase1_live.py
    └── test_phase2_safety.py
```

---

## Disclaimer

Chronos-Nexus is research / hackathon software. The wire is **live RSS**, not a licensed news terminal. Tokenized equity products and Demo listings differ by region and account. Stop-loss / take-profit placement depends on Bitget Demo order-type support; prices are always recorded even if a venue rejects the protective ticket. Qwen quota is a hard platform limit — the desk stands down until it resets. Nothing here is investment advice. Do not point this process at live keys unless you have lifted the documented gates and accept the loss.

---

## License

MIT. Use, fork, and modify with attribution. Keep Demo keys, Telegram tokens, and mainnet keys out of git.

---

**CHRONOS-NEXUS** — *Bitget AI Base Camp Hackathon S2 · Agentic Trading · Event-Driven Agent*  
News-driven directional trading. ORACLE · SENTINEL · CHAIRMAN  
**Hourly daemon. Open universe. GitBook paper log. Glasshouse Telegram. Free-Tier safe. Paper only.**
