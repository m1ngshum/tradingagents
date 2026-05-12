# TradingAgents Fork — TODOS

## Current status (2026-05-11)

**Phase B is blocked. Live test showed 28.6% win rate — below random baseline.**

The Welsh/UK live paper-trading test (finding #9 in PHASE_A_FINDINGS.md) revealed
that drama-bias persists on truly post-cutoff data. All Phase A and Phase B
infrastructure is complete, but the model's live accuracy (28.6%) is too low
for real money. Need 55%+ on 30+ live markets before enabling the Kelly executor.

### What's done

1. ~~**Score the live Welsh/UK paper positions.**~~
   **DONE 2026-05-11.** 4/14 resolved = 28.6% win rate, -$928.80 P&L (-54.6% ROI).
   Drama-bias persists in production. See PHASE_A_FINDINGS #9.

2. ~~**Quote-prediction prompt fix.**~~
   **DONE 2026-05-10.** QUOTE-PREDICTION MARKETS clause added. Trump-Biden fixed.
   Trump-Allah failure traced to look-ahead backtest artifact. See #7.

3. ~~**50-market balanced backtest.**~~
   **DONE 2026-05-10.** 85.4% accuracy but 88% NO-skewed sample — always-NO bot
   beats it by 2pp. Real signal: 2 correct BUY_YES calls. See #8.

4. ~~**Phase B Kelly criterion sizing.**~~
   **DONE 2026-05-11.** `tradingagents/exchange/binary_risk.py` — half-Kelly,
   20% cap, 55% confidence gate. Wired into `live_executor.place_order()`.
   18 tests pass. Remaining blockers: py-clob-client + wallet + regulatory.

### What's next

1. ~~**Electoral base-rate calibration fix.**~~ **DONE 2026-05-11.**
   ELECTORAL MARKETS clause added to trader prompt. Spot-check: Reform UK → HOLD,
   Welsh Conservatives → BUY_NO ✓, Welsh Greens → BUY_NO ✓. All 3 markets that
   lost -$300 now correctly avoided. No regression on 10-market cross-domain set.

2. **Run 30+ live paper positions on non-electoral markets** to measure production
   accuracy before attempting electoral markets again. Use `run_polymarket.py`
   on current open markets — sports, crypto, tech releases.

3. **Phase B real execution** — only after clearing 55%+ on 30+ live markets.
   All infrastructure is ready. Edge is not yet demonstrated.

4. ~~**Phase C: Alpaca stock trading.**~~ **DONE 2026-05-12.**
   `propagate_stock()` + `AlpacaExecutor` + `run_stocks.py` + `score_stocks.py`.
   10-ticker default watchlist (AAPL/MSFT/NVDA/GOOGL/AMZN/META/TSLA/JPM/XOM/UNH),
   half-Kelly sizing, 10% max per position, 55% confidence gate.
   Needs Alpaca API keys to activate (see .env.example).

---

## Polymarket Phase A

### DONE: Polymarket backtesting harness
Shipped 2026-05-08 as `scripts/backtest.py`. Pulls resolved markets from
gamma, runs `propagate_market()` against them, compares direction to
outcome. Includes `--end-date-max` for cross-domain testing.
Findings: `docs/PHASE_A_FINDINGS.md`.

### DONE: Block negative-EV trades pre-fill
Shipped 2026-05-08 (commit `d7ae4d9`). `is_economic_when_correct(fill)`
in `tradingagents/exchange/paper_fill.py`. Wired into `run_polymarket.py`
so guaranteed-loser trades log "NEGATIVE_EV_BLOCKED" and don't persist.

---

### DONE: Gamma API retry/backoff
Shipped 2026-05-08. `_http_get_with_retry()` in
`tradingagents/dataflows/polymarket_data.py` wraps Gamma REST calls with
tenacity retry on transient failures (network errors, 429, 5xx). 4xx
client errors are not retried.

---

### DONE: Prompt-injection sanitiser + daily call rate limiter
Shipped 2026-05-08 (commit `a1e8748`). `tradingagents/agents/utils/sanitize.py`
neutralises 7 classes of prompt-injection patterns in untrusted Exa news
text before it reaches the bull/bear/trader prompts.
`tradingagents/exchange/rate_limiter.py` caps daily LLM call volume
(default 100, override via `POLYMARKET_DAILY_CALL_LIMIT`) to bound
runaway-spend risk. State persisted at `~/.tradingagents/polymarket/rate_limit.json`.

---

### DONE: io_utils migration cleanup
Shipped 2026-05-08 (commit `b750ac0`, PR #1). All three Polymarket
scripts (`run_polymarket.py`, `score_fills.py`, `backtest.py`) now use
`tradingagents.exchange.io_utils.POLYMARKET_OUTPUT_DIR` and `append_jsonl`
instead of local copies. Net -15 lines, single source of truth for the
output dir and JSONL format.

---

### DONE: Drama-bias prompt fix
Shipped 2026-05-08 (commit `b1ee146`). Added BASE-RATE SKEPTICISM clause
to the trader synthesis prompt in `propagate_market()`. A/B test on the
same 10 cross-domain markets: 67% accuracy -> 88.9% accuracy. Surgical
flip on the two drama-bias markets (Iran military action, Trump
ceasefire end), no regressions on previously-correct calls.

Implementation lives in the trader prompt rather than the bull/bear
prompts: bull's job is to find the strongest YES case; weakening the
bull would degrade input quality. The trader is the right layer for
the calibration check.

---

### DONE: Quote-prediction failure mode investigation
**Shipped 2026-05-10.** QUOTE-PREDICTION MARKETS clause (v2) added to
trader prompt in `propagate_market()`. Handles "again" keyword as
base-rate evidence toward YES, separates frequency signal from drama signal.
Trump-Biden correctly called BUY_YES. Trump-Allah failure traced to
look-ahead market price data in Exa news (backtest artifact, not a live
trading failure). See PHASE_A_FINDINGS #7 for full diagnosis.

---

## Polymarket Phase B

### DONE: Binary risk model (Kelly criterion sizing)
**Shipped 2026-05-11** (`tradingagents/exchange/binary_risk.py`).
Half-Kelly multiplier (0.5×), 20% max position per trade, 55% min-confidence
gate. Wired into `live_executor.place_order()`. 18 unit tests pass.

**Remaining execution blockers (not code problems):**
- `py-clob-client` not installed (not in pyproject.toml by default)
- Wallet key management and USDC balance check
- Polymarket account creation flow
- Regulatory review — US persons currently blocked from Polymarket
- **Live accuracy gate: model must clear 55%+ on 30+ live markets first**
