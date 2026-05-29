# Go-Live Checklist — Real-Money Trading

**Status as of 2026-05-29: NOT READY. Gate 0 is open for both instruments.**
Routines are disabled. Do not start any of this until Gate 0 passes.

This checklist applies to BOTH trading surfaces:
- **Polymarket** (prediction markets, CLOB execution) — proven −EV (see STRATEGY.md)
- **Stocks** (Alpaca equities) — unproven; bot is catatonic (1 directional trade in 11 days)

---

## GATE 0 — Demonstrated edge (BLOCKS EVERYTHING ELSE)

Nothing below this line matters until the strategy proves edge on the
*executable* population. This is an evidence task, not an engineering task.

### Polymarket
- [ ] ≥30 resolved trades from the EXECUTABLE population: `conf ≥ 0.85 AND buy_price ≤ 0.97`. (The old "100% win" record was all unexecutable near-certainties priced >0.97 — those do not count.)
- [ ] That subset's realized ROI beats always-NO ROI by **≥5 percentage points net of fees + slippage** (always-NO was +1.3%, so the bar is ≥~6% net).
- [ ] Brier score on the subset **< 0.25** (currently 0.312 — worse than a coin flip).
- [ ] `python scripts/analyze_performance.py` prints a positive verdict on the executable slice, not the whole population.

### Stocks
- [ ] Bot actually trades: ≥30 directional decisions (currently 1 in 11 days — the confidence anchor at 0.54-0.55 must be fixed first so it clears the fill threshold).
- [ ] A stock equivalent of `analyze_performance.py` exists (current tool is Polymarket-only) and reports realized ROI + Brier on resolved positions.
- [ ] That ROI beats buy-and-hold SPY over the same window net of fees (the honest equity baseline — beating "always-NO" has no analog here).
- [ ] Brier < 0.25 on resolved directional calls.

**Reality check:** an LLM reading day-old news has no demonstrated informational
edge over either a liquid prediction market or large-cap equities. Clearing
Gate 0 likely requires a DIFFERENT edge source (speed: sub-second news→trade;
or illiquid mispricing) — i.e. a new project, not a config flip. If you cannot
articulate *why* you have edge that the market doesn't, stop here.

---

## STAGE 1 — Execution hardening (~1-2 days, build ONLY after Gate 0)

Paper mode assumes you fill at the quoted price. Live, that assumption breaks.

- [x] **Fill reconciliation** — **DONE (PR #31):** `classify_order_response`
      maps the CLOB response to FILLED/UNFILLED/UNKNOWN and fails safe (unknown
      is never a fill). `place_order` returns outcome + filled_usd;
      run_polymarket counts a position only on confirmed FILLED and flags
      UNCONFIRMED for manual review.
- [x] **Balance reconciliation** — **DONE (PR #33):** `reconcile()` HALTs
      (downgrades to paper) before the fire if real USDC < intended capital, or
      balance unreadable. Fails closed. `get_usdc_balance()` on the executor.
- [ ] **Position-drift reconciliation** — PARTIAL. The balance leg is done;
      true exchange-side position drift (fill log vs on-chain holdings) still
      needs a holdings fetch. Open item — not safety-blocking for the canary
      (max ~2 positions, manually verifiable), but required before scaling.
- [ ] **Slippage logging** — record decision-price vs actual-fill-price per
      trade. `filled_usd` is now captured per fill; still need to diff vs
      decision price and surface in analyze_performance.py. (Analytics nicety,
      not a safety gate.)
- [ ] **UMA settlement gating** (Polymarket) — do not book P&L until
      `is_uma_finalized()` is true. The helper exists; wire it into live P&L so
      a disputed resolution can't flip a "win". (Accuracy of P&L, not a
      runaway-safety gate.)
- [x] **Alerting** — **DONE (PR #34):** `Notifier` pushes Slack alerts on fill,
      breaker trip, reconciliation halt, unconfirmed order, fatal error. Fail-safe
      (never raises), no-op when `TRADINGAGENTS_ALERT_WEBHOOK` unset.

---

## STAGE 2 — Risk circuit breakers (build alongside Stage 1)

- [x] **Daily loss limit** → **DONE (PR #30):** `LossBreaker` trips when
      cumulative realized loss today ≥ limit (env `TRADINGAGENTS_DAILY_LOSS_LIMIT_USD`,
      default $30). Wired into run_polymarket live path; downgrades to paper.
- [x] **Total drawdown breaker** → **DONE (PR #30):** `LossBreaker` trips when
      peak-to-trough equity drawdown ≥ limit (env `TRADINGAGENTS_MAX_DRAWDOWN_USD`,
      default $50). Sticky across UTC rollover; requires `reset()` to clear.
      Fails CLOSED on corrupt state.
- [ ] **Capital laddering** → start at $100, raise only after N consecutive
      profitable fires. Never jump to size.
- [ ] **Per-instrument caps** verified live: cluster cap (Polymarket negRisk),
      max-orders-per-fire, half-Kelly + 20% position cap. (All built — confirm
      they behave under real fills, not just paper.)

---

## STAGE 3 — Security (MUST be done before any real key touches the system)

- [ ] **Rotate the exposed wallet** — the canary key from the 2026-05-29 session
      is compromised (appeared in chat + briefly in routine config).
- [ ] **Dedicated trading wallet** — funded with ONLY the capital at risk, never
      your main wallet. Blast radius on a key leak = trading float, not net worth.
- [ ] **Funding ceiling** — keep the trading wallet topped to a fixed cap; don't
      connect it to a large balance.
- [ ] **Key lives in the routine environment ONLY** — never in a prompt, never
      in chat, never in a committed file. (The 2026-05-29 canary violated this;
      a real go-live must not.)
- [ ] **Rotate all API keys** shared during development (OpenRouter, Exa,
      Polymarket, Alpaca).

---

## Go-live sequence (once all boxes checked)

1. Gate 0 passes on the instrument you're enabling (Polymarket and stocks are
   independent — one passing doesn't unlock the other).
2. Stage 1 + 2 + 3 complete for that instrument.
3. Enable the routine with kill switch ON for one fire → confirm it correctly
   stays paper (proves the safety path).
4. Kill switch OFF, `--capital 100`, max 1-2 orders/fire. Watch every fill.
5. Ladder capital up only after the live executable ROI continues to beat
   baseline across ≥2 weeks of real fills.
6. Re-run analyze_performance.py weekly. If live ROI drops below baseline, kill
   switch ON and re-review. Live performance decaying vs paper is the #1 sign
   the "edge" was overfit.

---

*The brakes (Stages 1-3) are real engineering, ~2-3 days total. But building
them now would be installing brakes on a car with no engine. Sequence is:
find edge → prove it on the executable population → harden execution → live
with laddered capital. Not before.*
