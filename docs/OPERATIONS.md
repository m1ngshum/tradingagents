# Operations

How the live system runs day-to-day: what fires when, what each job does, where output goes, and how to inspect or change it.

## At a glance

| Job | Schedule (HKT) | Schedule (UTC, what cron actually sees) | Wrapper |
|---|---|---|---|
| Polymarket discovery | 08:00 daily | `0 0 * * *` | `~/.tradingagents/discover_polymarket_daily.sh` |
| Stock paper run | 09:35 weekdays | `35 1 * * 1-5` | `~/.tradingagents/run_stocks_daily.sh` |

Both wrappers `cd` into `/Users/mingshum/repo/m1ngshum/TradingAgents` and invoke `.venv/bin/python` directly, so they don't depend on the user's shell environment.

## 1. Polymarket discovery — `discover_polymarket_daily.sh`

**Fires:** every day at 08:00 HKT (00:00 UTC).

**What it does:**
1. Fetches up to 600 active Polymarket markets sorted by liquidity.
2. Keeps only markets ending within 180 days, liquidity ≥ $5,000, price 0.05-0.95.
3. Pattern-classifies each market via `tradingagents/dataflows/market_classifier.py`:
   - Skips `bad-fit` categories: crypto/stock/commodity prices, weather, sports games, esports, talent shows, celebrity moves.
   - Keeps `good-fit` and `neutral` categories: elections, tournament participation, geopolitical, regulatory, concrete event deadlines, appointment outcomes.
4. Ranks remaining candidates by a pre-analysis score (price midpoint distance × liquidity × time-to-resolve).
5. Runs the full bull/bear/trader LLM pipeline on the top **15**.
6. Outputs to `~/.tradingagents/polymarket/discoveries-YYYY-MM-DD.jsonl` (one decision per line, with Kelly edge precomputed).

**Note on the date in the filename:** the script uses UTC. At cron fire time (00:00 UTC), HKT is already 08:00 the next morning, but the file is named with the UTC date. So a run on the HKT morning of May 14 produces `discoveries-2026-05-13.jsonl`.

**Output log:** `~/.tradingagents/logs/polymarket-discovery-YYYY-MM-DD.log` (with `date` evaluated at cron fire time — usually the upcoming-HKT date).

**Typical runtime:** 30-45 minutes (15 markets × ~2 min each through bull/bear/trader).

## 2. Stock paper run — `run_stocks_daily.sh`

**Fires:** Monday-Friday at 09:35 HKT (01:35 UTC). Weekends skipped (US markets closed).

**What it does:**
1. Builds today's watchlist via `scripts/build_stock_watchlist.py`:
   - **CORE_WATCHLIST (5 mega-caps)** — always included: `AAPL MSFT NVDA GOOGL AMZN`.
   - **CATALYST_CANDIDATES (~95 large/mid caps)** — included only if they report earnings within the next 10 trading days. Earnings dates pulled per-ticker from yfinance with a 16-thread pool.
   - Combined list capped at **20 tickers**.
   - Fallback if yfinance is down: a fixed 10-ticker list (`AAPL MSFT NVDA GOOGL AMZN META TSLA JPM XOM UNH`).
2. Runs `scripts/run_stocks.py` on the watchlist with:
   - `--capital 10000` — base capital for half-Kelly sizing.
   - `--model anthropic/claude-sonnet-4-6` via OpenRouter.
   - Default `--min-confidence 0.55` gate.
3. For each ticker: bull researcher → bear researcher → trader synthesis → `StockDecision`.
4. If `LONG` or `SHORT` with confidence ≥ 0.55 and positive Kelly edge: submits a paper market order via Alpaca.

**Output:**
- Decisions: `~/.tradingagents/stocks/decisions-YYYY-MM-DD.jsonl`
- Paper orders: `~/.tradingagents/stocks/paper-orders-YYYY-MM-DD.jsonl`
- Log: `~/.tradingagents/logs/stocks-YYYY-MM-DD.log`

**Typical runtime:** 20-40 minutes (15-20 tickers × ~1-2 min each).

## On-demand commands

| Command | What it does |
|---|---|
| `.venv/bin/python scripts/freshen_signals.py --max-age-hours 24 --min-edge 0.10` | Re-fetches current prices for recent Polymarket discoveries and recomputes Kelly edge. Run before placing a trade to catch stale signals. |
| `.venv/bin/python scripts/score_stocks.py --date YYYY-MM-DD` | Mark-to-market the stock paper orders from a given date. Uses Alpaca for current prices. |
| `.venv/bin/python scripts/score_fills.py --date YYYY-MM-DD` | Score Polymarket paper fills from a given date against gamma-api current prices / resolutions. |
| `.venv/bin/python scripts/build_stock_watchlist.py --verbose` | Print today's earnings-rotated watchlist (also what the cron uses). |
| `.venv/bin/python scripts/discover_polymarket.py --analyse N` | Run discovery manually (same as the cron) but with custom limits. |

## Inspecting cron state

```bash
# Current crons
crontab -l

# Today's stock decisions
cat ~/.tradingagents/stocks/decisions-$(date -u +%Y-%m-%d).jsonl

# Today's Polymarket discoveries (UTC date)
cat ~/.tradingagents/polymarket/discoveries-$(date -u +%Y-%m-%d).jsonl

# Recent log files
ls -la ~/.tradingagents/logs/
```

## Changing the schedule or scope

Both wrappers are short shell scripts in `~/.tradingagents/`. To edit:

```bash
# Change stock watchlist size or capital
nano ~/.tradingagents/run_stocks_daily.sh

# Change Polymarket discovery scope or filters
nano ~/.tradingagents/discover_polymarket_daily.sh

# Change schedule
crontab -e
```

After editing the wrappers, the next cron tick uses the new version (no reload needed).

## Pre-deploy / pre-test trade checklist

Before placing a real trade based on a signal:

1. Run `freshen_signals.py` — confirm edge survives at current price (not just at analysis time).
2. Cross-check the trade URL — verify it routes to the parent event (the freshener does this via market-id verification, but eyeball it).
3. Size by Kelly — `~/.tradingagents/polymarket/discoveries-*.jsonl` has the bot's recommended fraction. Cap at 5-10% of trading capital per single market regardless of what Kelly says.
4. Note resolution date — short-horizon markets give faster feedback for accuracy tracking.

## Known gaps

- **Polymarket live execution:** blocked. Account is a custodial deposit wallet — CLOB API rejects external signatures. See `polymarket_executor.py` docstring for the credentials path (when a self-custody wallet is set up).
- **Alpaca live execution:** infrastructure is in place but `ALPACA_PAPER=true` is hardcoded as default. Setting `ALPACA_PAPER=false` plus real-money keys would enable it — explicitly out of scope for now.
- **Discovery filename quirk:** Polymarket discovery names files by UTC date (`discoveries-2026-05-13.jsonl`) while the wrapper log uses local date (`polymarket-discovery-2026-05-14.log`). Cosmetic, but watch for it when correlating files.
