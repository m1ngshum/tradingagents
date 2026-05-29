# Safety Architecture — Trade Automation

This documents the safety state of the automation as a coherent whole. The
design goal: **the system cannot run away, drain the bankroll, trade on a
false view of its positions, or fail silently — regardless of whether the
strategy has edge.**

Safety ≠ profitability. These guards protect capital; they do not create
returns. See STRATEGY.md for the (negative) edge finding and GO-LIVE-CHECKLIST.md
for the full path to live.

## The guard stack (every live order passes through all of these)

| # | Guard | Trips on | Failure mode | Module | PR |
|---|-------|----------|--------------|--------|-----|
| 1 | Kill switch | Manual env flag | n/a (manual) | run_polymarket | #25 |
| 2 | Rate limiter | >100 LLM calls/UTC-day | fail-open (cheap) | rate_limiter.py | — |
| 3 | Cost ceiling | LLM $ spend/day | fail-open (cheap) | cost_tracker.py | #18 |
| 4 | **Loss breaker** | Daily realized loss / drawdown | **fail-CLOSED** | loss_breaker.py | #30 |
| 5 | **Balance recon** | Real USDC < intended capital | **fail-CLOSED** | reconciliation.py | #33 |
| 6 | Min-confidence | conf < 0.85 (script + executor) | block | run_polymarket / executor | #17/#25 |
| 7 | Cluster cap | >1 position per negRisk group | block | scoring.py | #18 |
| 8 | Max-orders/fire | >N orders in one fire | block | run_polymarket | #25 |
| 9 | Kelly + 20% cap | position > 20% capital | clamp | polymarket_executor.py | — |
| 10 | **Fill reconciliation** | Unconfirmed/killed order | **fail-CLOSED** (never assume fill) | polymarket_executor.py | #31 |
| 11 | **Alerting** | fill / trip / halt / error | fail-safe (silence, never crash) | notifier.py | #34 |

## The two design principles

**1. Capital-touching guards fail CLOSED.** Loss breaker, balance recon, and
fill reconciliation all default to the *safe* state on any ambiguity: corrupt
breaker state → tripped; unreadable balance → halt; unrecognised order status →
not-a-fill. A safety device that fails open is not a safety device. (Cheap
guards — LLM rate/cost — fail open because over-spending tokens is recoverable;
over-losing capital is not.)

**2. Nothing fails silently.** The notifier alerts a human on every
money-moving or safety event. A broken notifier degrades to silence, never to a
crashed fire — but when configured, the operator hears about every fill, trip,
and halt.

## What "downgrade to paper" means

Guards 4 and 5 don't crash the fire — they set `live_executor = None`, so the
fire continues in paper mode (decisions logged, no real orders). The bot keeps
producing data; it just stops risking money until a human reviews and clears
the trip (`LossBreaker.reset()` / fixes the balance). This is deliberate: a
tripped safety should not also blind you to what the strategy *would* have done.

## Configuration (all env vars)

```
TRADINGAGENTS_AUTOTRADE_KILL_SWITCH   # any truthy => force paper
TRADINGAGENTS_DAILY_LOSS_LIMIT_USD    # default 30; 0 disables
TRADINGAGENTS_MAX_DRAWDOWN_USD        # default 50; 0 disables
TRADINGAGENTS_ALERT_WEBHOOK           # Slack incoming webhook; unset => no-op
POLYMARKET_DAILY_CALL_LIMIT           # default 100; 0 disables
TRADINGAGENTS_POLYMARKET_DAILY_BUDGET_USD  # LLM $ ceiling
```

## Remaining (not runaway-safety gates; required before SCALING beyond canary)

- **Exchange-side position-drift detection** — balance leg done; on-chain
  holdings fetch not yet wired. For the canary (max ~2 positions/day) positions
  are manually verifiable. Required before larger size.
- **Slippage logging** — `filled_usd` captured; not yet diffed vs decision price.
- **UMA settlement gating** — don't book P&L until finalized. Affects P&L
  accuracy, not runaway risk.
- **Capital laddering** — programmatic ramp from $100 only after N profitable
  fires. Currently manual via `--capital`.

## Verdict

For **canary-scale** live trading ($100 capital, ≤2 orders/fire), the runaway-
safety surface is closed: the bot cannot lose more than the daily/drawdown
limits, cannot trade against funds it lacks, cannot assume phantom fills, and
cannot fail without alerting. The open items above gate *scaling*, not initial
safe operation.

This does not mean go live — Gate 0 (demonstrated edge, see GO-LIVE-CHECKLIST.md)
is still open and the strategy is −EV. It means: IF you go live at canary scale,
the brakes are real and tested.
