# Strategy Post-Mortem — Polymarket / Stocks LLM Trading Bot

**Status: WOUND DOWN (2026-05-29).** Routines disabled, no capital deployed.
The machinery is sound and reusable; the trading *thesis* is disproven.

## The thesis we tested

> A multi-agent LLM (bull/bear/trader debate) reading recent news (Exa) can
> estimate prediction-market and equity outcomes well enough to beat the
> market price, after fees.

## The verdict (measured, not guessed)

Run `python scripts/analyze_performance.py` to reproduce. On 45 resolved
directional Polymarket decisions + 22 resolved real fills:

| Metric | Value | Meaning |
|---|---|---|
| Actual-fills realized ROI | **−42.4%** (24 positions) | Real money lost |
| Strategy ROI (all decisions, simulated) | **−23.4%** | No way to act on it profitably |
| BUY_YES | −35.8% ROI (n=29) | The bull-researcher manufactures false YES conviction |
| BUY_NO | −1.0% ROI (n=16) | Breakeven, not edge |
| Always-NO baseline | **+1.3% ROI** | Market is ~calibrated; no free NO-bias money |
| Brier score | **0.312** | Worse than always guessing 0.5 (=0.25) |

Calibration is the damning part: when the bot says **0.72 confidence it wins
8%** of the time. Confidence is anti-predictive in every actionable range.
The only band that "works" (0.85+, 100%) consists of near-certainties priced
>0.97 that the executor correctly refuses (no upside after fees) — i.e. the
"wins" were unexecutable.

## Why it fails (first principles)

1. **The market is efficient at our liquidity filter (≥$5k).** Always-NO ≈ 0%
   ROI confirms YES is not systematically overpriced here. There is no
   structural edge to harvest.
2. **An LLM on day-old news has no informational advantage** over a liquid
   market that already priced that news. The only retail edges in prediction
   markets are *speed* (sub-second news→trade, needs infra we don't have) or
   *illiquid mispricing* (which our liquidity filter excludes by design).
3. **The debate architecture actively harms.** The bull researcher's job is to
   argue YES, which fabricates conviction → BUY_YES is 64% of activity and
   bleeds −36%.

## What's worth keeping (the machinery is good)

- `scripts/analyze_performance.py` — EV/ROI/Brier/calibration analytics.
  Measure money, not hit rate. Reusable for any future strategy.
- `tradingagents/dataflows/polymarket_data.py::resolve_cluster_id` — 3-tier
  negRisk cluster resolver (negRisk → events → synthetic fail-safe).
- Safety gates: cluster cap, min-confidence (script + executor), daily cost
  ceiling, half-Kelly sizing, `--max-orders-per-fire`, kill switch env var.
  These worked — they correctly refused to deploy into a no-edge distribution.
- `scripts/derive_polymarket_creds.py` — L1→L2 CLOB cred derivation.
- Routine plumbing (RemoteTrigger config, state-repo push pattern).

## How to re-open (don't, unless the edge source changes)

Re-running prompt tweaks will NOT find edge — we tested prompts (#17/#21/#24),
gates (#18/#25), categories, both directions, both metrics. The alpha isn't a
parameter. Only re-open if you bring a *different edge source*:

- **Speed:** sub-second news ingestion → order, before the market reprices.
  Different infra (websocket feeds, colocated execution). Different project.
- **Illiquid mispricing:** systematically scan sub-$5k markets for genuine
  mispricings. Drop the LLM-prediction premise; this is a screening problem.

Before any re-enable: run `analyze_performance.py` on fresh data and require
the *executable* population (conf≥0.85 AND buy_price≤0.97) to beat always-NO
ROI by a margin that covers fees + slippage. Hit rate is not evidence.

## Cost of the lesson

~2 weeks of engineering + a few dollars/day OpenRouter. The expensive thing
most people skip is *proving* there's no edge instead of tuning forever. We
proved it. That's the deliverable.
