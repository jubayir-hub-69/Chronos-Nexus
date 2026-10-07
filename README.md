# Chronos-Nexus

**One-liner.** An hourly Python desk where Qwen 3.8 Max reads the wire, argues the trade, and sends a Bitget Demo USDT-M order, with a Telegram desk and a process that stays up.

| | |
|---|---|
| **Name** | Chronos-Nexus |
| **Track** | Agentic Trading |
| **Version** | `1.3.0-desk` (`VERSION` in `main.py`) |
| **Default contract** | `rNVDA/USDT:USDT` |
| **Cycle** | First pass at boot, then every 3600 seconds. A cycle fault waits 300 seconds and continues. |
| **Repository** | [github.com/Jubayir-hub-69/Chronos-Nexus](https://github.com/Jubayir-hub-69/Chronos-Nexus) |

The console status line is **LIVE AUTONOMOUS EXECUTION**. That is the daemon: it is awake, it decides, and it sends the order. The order itself is a Bitget Demo order. `LIVE_TRADING_ENABLED` in `main.py` is `False`. `BITGET_PAPER_TRADING` must stay `true` or the process refuses to boot. Prices and the book come from Bitget. Nothing in this tree is a local fill simulator.

---

## Thesis

News on equity and stock-token perps is usable only if someone is awake when the headline hits, and only if the order is small enough to survive a bad read. A notebook strategy does neither. It runs when a person opens it.

Chronos-Nexus is the other shape. One process wakes every hour, pulls a live RSS wire, asks Qwen for one unoccupied contract, measures the tape, and either sends a Bitget Demo market order or stands down. The same process then watches the open book: margin stop, first scale-out at +25% PnL, trail, and a close when the thesis breaks. The operator does not sit in a browser tab. Telegram is the terminal.

The bet is narrow. A model is good at mapping a headline to a listed name and writing down why. It is bad at being the only risk system. Python keeps the hard numbers: RSI, spread, mark distance, order size, and the daily budget. Qwen does the reading and the judgment inside those numbers.

---

## Target user and product logic

The user is a single operator who wants an agent running while they are away from the machine. Guests can look. They cannot trade.

There is no web front end to deploy, log into, or keep warm. `main.py` is the product. It is a normal Python process with an HTTP health check on `PORT` (default 8080) so a host such as Render can see that it is alive. The trading loop is a `while True` in that same process. It is not a request handler that dies when the HTTP call ends. That is the practical 24/7 shape: one daemon, one Telegram bot, one Bitget Demo key.

### Sense

Each cycle fetches live headlines (Yahoo Finance, CNBC, MarketWatch, CoinTelegraph) and the Bitget Demo universe from `load_markets()`. A keyless mainnet Bitget client supplies the ticker, the L2 book, and 15m / 1h / 4h candles. The private client, with `set_sandbox_mode(True)` and `PAPTRADING=1`, is the one that can order.

ORACLE (Qwen) must copy one symbol from that universe or emit `NONE`. Occupied names are removed before the model sees them. Crypto majors are not in the AI universe. The hourly path trades stock and rToken USDT-M contracts, not cash spot.

### Judge

SENTINEL scores the setup and can veto. CHAIRMAN writes the board minute. Python then locks the action. A locked `EXECUTE` is what gets sent. A locked `STAND_DOWN` does not.

The setup score is a weighted tape, not a news score:

`0.10` news + `0.22` higher-timeframe + `0.18` entry + `0.18` volume + `0.16` book + `0.10` extension + `0.06` conviction.

The floor is **40** (`SETUP_THRESHOLD` in `core/ta.py`). If 1h and 4h were not measured, that floor is skipped. A quiet US session (pre-market, overnight, weekend, after-hours) or a neutral wire can still promote a liquid name through `core/neutral_lane.py` when the score is at least 70 and 24h quote volume is at least 250,000 USDT. That scan runs only after a real stand-down, not after an API failure.

Three tape conditions are recorded and do **not** cancel the entry while `RELAX_TAPE_VETOES` is true:

- `VETO: Higher-timeframe trend disagrees`
- `VETO: Candle structure contradicts news thesis`
- `VETO: Choppy/downtrending tape — daily capital halt`

If Qwen returns a veto and none of the hard rails below fired, and the measured setup score is at least 40, the desk clears the entry and says so in one line. The model still wrote the thesis and the risk note. Python did not let those three tape notes be the reason the order died.

These still cancel a new entry:

| Rail | Rule |
|---|---|
| RSI(14) | BUY at RSI ≥ 70, or SELL at RSI ≤ 30. Unmeasured RSI does not veto. |
| Spread | Bid/ask wider than 1.5%, or a crossed book. |
| Mark | Contract BBO more than 2% from mark price. |
| Book | Opposing wall at least 3× the median size within 0.8% of last. |
| Volume | A break outside the value area with relative volume under 1.1. |
| News | BUY when sentiment ≤ 35, or SELL when sentiment ≥ 65. |
| Setup | Measured 1h/4h score under 40. |
| Day | 4 entries per UTC day, 3 winning closes, or any stop. |
| Budget | New margin stays inside 6% of futures equity. |
| Model | Timeout or Qwen quota. The desk stands down. It does not invent a trade. |
| Book | A name that already has a position is not re-opened. |

Stops are 50% to 100% of invested margin, from the risk score. The first target scales out half at +25% PnL and trails the rest. Default order size is 15 USDT notional (`paper_notional_usdt`, capped at 100). Swap leverage is 5x. A manual Telegram spot order has no leverage and no stop.

### Execute

CHAIRMAN hashes the minute. If an Arbitrum Sepolia key is set, the desk sends a **0-value** transaction on chain 421614 whose calldata starts with `CHRONOS-NEXUS/v1:` and the hash. No key means the proof is skipped and the reason is logged. The order does not wait on a failed proof.

The Bitget order is a Demo USDT-M market order. The price stamped on it is the live mainnet best ask for a buy and the best bid for a sell. It is not a sandbox last and not a mid. An order id with no fill is not treated as a fill.

After the fill, the position sits in `data/desk.json`. Later cycles can close it. Telegram gets the alert. `/status` and `/pnl` read the same files the cycle just wrote.

### Telegram and the cloud process

`utils/commands.py` is the command desk for both the local `nexus>` prompt and Telegram. `TELEGRAM_CHAT_ID` is the operator. Anyone else is a guest.

| Who | May |
|---|---|
| Operator | `/menu`, `/positions`, `/close`, `/closeall`, `/price`, `/balance`, `/pnl`, `/status`, and a sized spot message such as `BUY 10 USDT BTC` |
| Guest | `/help`, `/price`, `/status`, `/positions` |
| Guest trying to trade | The reply is exactly `Access Denied: Operator command only.` |

Run **one** process for a given bot token. Telegram allows a single `getUpdates` poller. A second poller is ignored by this process and is not printed. It also means one of the two desks is not actually receiving commands.

On Render, create a **Web Service**, not a serverless function. Start command: `python main.py`. Health check path: `/` (any GET returns `OK`). Set the environment variables below on the service. The free or paid instance has to stay running. Spinning the service down stops the agent. That is the point of a daemon.

---

## Role of the LLM

Qwen is not a caption on a Python signal. Three calls share one client, `QwenCortex` in `core/llm.py`.

| Agent | Job | What Qwen decides |
|---|---|---|
| **ORACLE** | Analyst | Which listed contract, which side, the thesis, and whether the wire is too toxic to touch. `NONE` is a valid answer. |
| **SENTINEL** | Risk | Fake-news risk, a 0–100 asset-risk score, and a verdict of `CLEAR`, `REDUCE`, or `VETO`, with a written reason. |
| **CHAIRMAN** | Executive | The minute for the action Python already locked. It does not get to overrule a hard rail. |

The client is the OpenAI SDK pointed at `https://hackathon.bitgetops.com/v1/chat/completions`. The SDK base URL is the `/v1` prefix, because the SDK appends `/chat/completions` itself. The model id is `qwen3.8-max`. `auto`, `default`, and `qwen-max` resolve to that id. Thinking is off. Each call allows 90 seconds and 8192 tokens. A timeout or a quota error is a stand-down with a fixed reason string, not a guessed trade.

Conviction on the brief is clipped to the Python news score in `core/news.py`. The model does not get to print a 90 on a neutral wire. Headlines are scored from a neutral 50, then pulled back toward 50 by source weight and age. A conflicted tape caps conviction at 20.

So the split is: Qwen senses and judges. Python executes only when the numbered rails above agree, and it will still send the order when Qwen's objection is only the three advisory tape notes and the score clears 40.

---

## Tech stack

| Piece | What it is in this repo |
|---|---|
| Language | Python 3.10+ (developed on 3.12) |
| LLM | Qwen 3.8 Max through `openai>=1.68.0` and the Bitget hackathon proxy |
| Exchange | Bitget Demo via `ccxt`. Private orders on the sandbox client. Public ticker, book, and candles from a keyless mainnet client. |
| Chain | Arbitrum Sepolia, chain id 421614. 0-value attestation. `web3`. |
| Operator UI | Telegram long-poll plus a local `nexus>` prompt. `rich` for the terminal. |
| Host | Any long-lived process. Render Web Service fits. A serverless function does not. |
| Config | `pydantic-settings`, `.env` via `python-dotenv` |
| Wire | `feedparser` for RSS |
| State | `data/history.json`, `data/desk.json`, `data/logs/trades.json`, `data/telegram_offset.json` |

Dependencies are pinned as ranges in `requirements.txt`.

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

On macOS or Linux, use `source .venv/bin/activate` and `cp .env.example .env`.

Create `.env` next to `main.py`. Do not commit it. `core/config.py` loads that file with `override=False`, so a variable already set in the process wins.

```dotenv
QWEN_API_KEY=your_qwen_api_key
QWEN_MODEL=qwen3.8-max

BITGET_API_KEY=your_demo_api_key
BITGET_API_SECRET=your_demo_secret
BITGET_PASSPHRASE=your_demo_passphrase
BITGET_PAPER_TRADING=true
BITGET_SYMBOL=rNVDA/USDT:USDT

ARBITRUM_SEPOLIA_RPC=https://sepolia-rollup.arbitrum.io/rpc
ARBITRUM_SEPOLIA_CHAIN_ID=421614
ARBITRUM_PRIVATE_KEY=0xyour_sepolia_private_key

TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHAT_ID=123456789
```

| Variable | Required | Role |
|---|---|---|
| `QWEN_API_KEY` | For any decision | Empty key: the cortex is offline and the desk stands down. |
| `QWEN_MODEL` | No | Default `qwen3.8-max`. |
| `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE` | For orders | Create these in Bitget **Demo** mode. |
| `BITGET_PAPER_TRADING` | Yes | Must be `true`. `false` / `0` / `off` refuses boot. |
| `BITGET_SYMBOL` | No | Preferred contract. Default `rNVDA/USDT:USDT`. |
| `ARBITRUM_SEPOLIA_RPC` | No | Default `https://sepolia-rollup.arbitrum.io/rpc`. |
| `ARBITRUM_SEPOLIA_CHAIN_ID` | No | Default `421614`. A mismatch fails the chain ping. |
| `ARBITRUM_PRIVATE_KEY` | No | Testnet key only. Missing key skips the proof. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | For Telegram | Both empty: alerts and the command loop stay off. The cycle still runs. |

**Telegram user id.** Put the bot token in `.env`. Set `TELEGRAM_CHAT_ID` to a placeholder that is not yours. Start the process, open the bot, send `/start`. The guest card prints your user id. Stop, put that id in `TELEGRAM_CHAT_ID`, start again, send `/menu`. You should see the operator dashboard.

**Bitget key.** The account has to be in Demo mode when the key is created. The connector still forces sandbox mode. Do not point this process at a live key.

Boot order: compliance, Qwen bind, Bitget ping, Arbitrum ping, Telegram menu, then the first cycle. Stop with Ctrl+C. Exit code 2 is a compliance refusal. Exit code 1 is an unhandled fault outside the loop.

```bash
python -m unittest discover -s tests -v
```

The tests cover the connector, safety rails, positions, RSI, stops, daily limits, spot chat, mainnet BBO, analytics, the setup score, the Telegram desk, close chunking, and guest versus operator access.

### Render

1. New Web Service from this repo. Environment: Python.
2. Build: `pip install -r requirements.txt`
3. Start: `python main.py`
4. Health check path: `/`
5. Add every variable from the table above. `BITGET_PAPER_TRADING=true`.
6. One instance. A second instance will fight the first for Telegram updates.

The trading loop does not need an inbound request. The HTTP server exists so the platform does not kill a quiet process.

---

## Commands

Native menu text is `BOT_MENU` in `utils/commands.py`.

| Command | What it reads or does |
|---|---|
| `/menu`, `/dashboard` | Operator dashboard. Buttons edit the same message: status, PnL, positions, close-all confirm. |
| `/positions` | Open USDT-M contracts only. Spot coins are balances, not positions. |
| `/close SYMBOL` | `BTC/USDT:USDT` reduces the perp. `BTC/USDT` market-sells spot. |
| `/closeall` | Flattens open USDT-M positions. It does not sell the spot wallet. |
| `/price SYMBOL` | Last, bid, ask, 24h high/low, quote volume, market cap or book notional. Missing fields are `n/a`. |
| `/balance` | Spot and USDT-M free/used/total on separate lines. They are not added together. |
| `/pnl` | Today's realized PnL from `data/history.json`, plus live unrealized on the open book. |
| `/status` | Last ORACLE / SENTINEL snapshot. `NO SCAN YET` until the first cycle finishes. |
| `BUY 10 USDT BTC` | Operator only. Demo spot market buy. No stop, no leverage. A bare `/buy` with no size is refused. |

An empty book prints `FLAT — no open USDT-M positions.` A desk that has not scanned prints `NO SCAN YET` and `+0.00 USDT` realized. That zero is an empty ledger.

---

## Logs

| File | Contents |
|---|---|
| `data/logs/trades.json` | Append-only order record. `sandbox: true` on Demo orders. |
| `data/history.json` | Recent cycles, the UTC-day ledger, the latest snapshot. |
| `data/desk.json` | Open-book metadata: entry, stop, target, thesis, trail. |
| `data/telegram_offset.json` | Telegram `getUpdates` offset. |
| `data/spot_paper_wallet.json` | Spot base that Bitget Demo debited USDT for but did not credit. `/balance` adds it to the spot line only. |

There is no published Sharpe, win rate, or equity curve in this document. Read the log if you want a number. A row with an order id and a null fill is not a fill.
