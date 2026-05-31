# TradingAgents v2 — Unified Pipeline: Engineering Plan

_Authored 2026-05-30. Produced by a multi-agent research + design pass (5 research streams → synthesis → 3 adversarial critics: safety / edge-honesty / simplicity → finalize). The critics materially reshaped the draft; their resolutions are noted inline so decisions aren't re-litigated later._

## Implementation status (updated 2026-05-30)

**Phases 1 and 2 are implemented and green** — full test suite 539 passed, 1 skipped. All PAPER-only; no live order path was activated.

- **Phase 1 — DONE (paper):** `tradingagents/exchange/gate.py` pure fire-gate (`evaluate_fire`: KILL_SWITCH / LOSS_BREAKER / RECONCILE); real exchange-side position read `PolymarketExecutor.get_open_position_count()` (Polymarket Data API, fail-closed) wired into `reconcile()` — **the no-op reconciliation bug is fixed**; shared `kill_switch_active()`; deleted dead `live_executor.py` + `binary_risk.py` (+ their tests). **Plan correction:** the *entire* `binary_risk` module was dead (the executor uses its own inline Kelly in `size_polymarket_order`), not just `size_order` as §1 assumed.
- **Phase 2 — DONE (paper):** `NOTIONAL_EXPOSURE` ceiling in `evaluate_fire` + `reconciliation.open_exposure_from_fills()` + `--exposure-budget`; pure `evaluate_market` (confidence floor + opt-in cost-aware edge via `--min-edge`, default 0 = off, no paper drift); `tradingagents/exchange/execution_metrics.py` (`compute_execution_metrics`: fill-rate + slippage; live realized-vs-quoted bps deferred, returned `None` not faked) wired into the analyzer's report; paper-shadow purity test.

**Deferred to Phase 3** (live-pilot wiring, gated on the OWNER DECISIONS at the bottom of this doc): live matched-price capture for realized-vs-quoted bps; frozen-snapshot shadow replay; per-market *cumulative* exposure (current ceiling is fire-level); running the forced-drift HALT test in the actual Routines runtime.

## Premise (ground truth, carried forward)

The LLM bull/bear debate has **no proven edge** (Polymarket ROI −23.4% vs always-NO +1.3%; Brier 0.312, worse than a coin flip; stocks catatonic at ~1 directional trade / 11 days). This plan does **not** assume alpha. Its deliverable is a **statistically honest, per-instrument go / no-go / inconclusive verdict**. The LLM is a non-decisional proposer; a deterministic gate is the sole path to any executor.

**Fact corrections to early drafts:** (1) `scripts/calibrate.py` already exists and `run_polymarket.py` already enforces a calibration-derived min-confidence gate (~0.85) — there is no missing "calibration layer" to build. (2) `web3` / `eth-account` are **not** present as deps; `requests` is.

---

## 1. Target architecture

**Scope discipline (simplicity critic, upheld):** Ship a **Polymarket-only pilot**. Do **not** build a 3-instrument abstraction up front — no `contracts.py`, no adapter `Protocol`, no `CryptoDexAdapter`, no `SafePipeline` class, no import-linter rule. Those are single-use ceremony for instruments 2 and 3, which are deferred (crypto custody-blocked; stocks may stay paper forever). The adapter Protocol is introduced only when a **second live instrument actually exists** (rule of three).

**Minimal real changes the pilot requires:**
- `tradingagents/exchange/gate.py` — **new.** One pure function `gate(proposal, snapshot) -> Verdict{allow, level, reason_code, checks[]}` consolidating the safety/edge checks currently scattered in `run_polymarket.py` (kill-switch, breaker, recon, confidence, Kelly>0). No clock/network reads inside; all live state injected as a frozen snapshot. `level ∈ {FIRE_HALT, MARKET_VETO}` (Section 2).
- `run_polymarket.py` — **implement the stubbed exchange-side position read** (today it echoes `actual=expected`, so reconciliation is currently a no-op — a real bug). Route safety checks through `gate()`. Stays a script; not rewritten into a pipeline class.
- `run_polymarket.py` / `run_stocks.py` — extract the duplicated kill-switch check into one shared `kill_switch_active()` helper (~30 lines), no central orchestrator.
- Pilot caps wiring (Section 5).

**Reuse unchanged:** `loss_breaker.py`, `reconciliation.py` (pure comparator), `notifier.py`, `cost_tracker.py`, `io_utils.py`, `rate_limiter.py`, `paper_fill.py`, `binary_risk.kelly_fraction`, `scripts/analyze_performance.py`, `scripts/calibrate.py` + its existing confidence gate.

**Dead code (surgical — only what our changes orphan):** Delete `live_executor.py` (dead stub) **together with** its only consumers — `tests/test_live_executor.py` and the `size_order` cases in `tests/test_binary_risk.py` (since `binary_risk.size_order` is used only by that dead path). Do **not** touch the live `polymarket_executor._half_kelly_size` path (works, tested). No "shared sizing core" refactor — that was gold-plating.

**Trust boundary (enforced by code review, not new tooling):** proposer/LLM modules never reach an executor; the gate-then-place sequence in the run script is the only path to `place_order`.

---

## 2. The gate (safety heart)

Pure function, ordered short-circuit, first failure returns its `reason_code`. **Two distinct outcome levels** (fixes the safety critic's fail-open concern):

- **FIRE_HALT** — abort the entire fire / force paper globally, applied **once at fire entry, not per-market:** `KILL_SWITCH` active, `LOSS_BREAKER` tripped, `RECONCILE` drift/None/negative, `NOTIONAL_EXPOSURE` ceiling breached, `DAILY_BUDGET` (LLM cost) exceeded.
- **MARKET_VETO** — skip one market, continue: `CONFIDENCE_FLOOR`, `EDGE_NET_OF_COST`, `POSITION/CLUSTER_CAP`, `MAX_ORDERS_PER_FIRE`, `SIZE==0`.

**Property tests (release gates):** (a) with kill-switch on or breaker tripped, `place_order` is unreachable for **every** market in the fire; (b) the live→paper downgrade is applied once at entry and a per-market VETO can never re-enable live for a later market; (c) gate never ALLOWs when `edge < cost`.

**Exposure ceiling (safety critic's biggest risk — accepted as mandatory):** `loss_breaker` acts on **realized** P&L only, but Polymarket positions don't resolve until UMA finalization (days/weeks), so the realized-loss breaker gives **zero** intra-experiment cap. The gate enforces a **NOTIONAL_EXPOSURE** ceiling: `sum(open-position cost basis from fill log + live position read) + new order < experiment budget ($49)` → else FIRE_HALT. This, not the breaker, is the real exposure cap for a slow-settling instrument.

**EDGE_NET_OF_COST — what produces `fair_prob` (fixes the edge critic's "non-functional gate"):** For a scheduled cron on an efficient market there is **no defensible source of positive-edge `fair_prob`**. So the pilot does **not** pretend to test edge. The gate uses the existing calibration-derived confidence gate (`calibrate.py` corpus, ~0.85 floor) as the direction/quality filter, and requires `calibrated_prob − ask > win_fee(2%) + min-order margin`. Sizing is **fixed min-size USD**, bypassing confidence-based Kelly entirely (neuters the poisoned `size_polymarket_order` path). If calibrated edge is ~0 by construction (efficient market), the gate simply vetoes — the correct, honest behavior.

**UNCONFIRMED handling:** an `UNCONFIRMED` fill forces the next fire to paper until a human reconciles; once the real position read lands, reconcile UNCONFIRMED records against the exchange position **set** (identity, not just count) and FIRE_HALT on any exchange position absent from the fill log.

---

## 3. Crypto / DEX leg — DEFERRED

Crypto is **out of the pilot and not designed now.** Blocked on custody (Phase 0) and execution-dominated on $49. Do **not** pre-select a router, add `web3`/`eth-account` deps, or design nonce/MEV/simulation/approval machinery until after the Polymarket pilot returns its verdict and custody is resolved.

**Pre-committed constraints for when it is built (from the safety critic):** a **dedicated fresh EOA** funded with only the test amount (MANDATORY — isolates blast radius from the Polymarket proxy); broadcast path guarded by the same kill-switch with a test proving kill-switch-on ⇒ no `eth_sendRawTransaction`; a hardcoded per-tx max-USD ceiling inside the executor itself (defense in depth below the gate); 0x-style aggregator + `eth_call` sim + private mempool + per-trade approval; HALT (never an unguarded router) if the aggregator is unavailable. Universe ETH/WBTC only.

**Cheap fact to collect now (free, no funds moved):** pull a live 0x quote for a $25 and $49 WETH↔USDC round-trip on Polygon and record cost-as-%-of-notional — converts the "execution-dominated" deferral from intuition into a number.

---

## 4. The edge experiment (honesty layer)

**The hard truth (edge critic's biggest risk — accepted, reshapes the pilot):** a scheduled-cron Polymarket pilot's expected outcome is already known ("confirm no edge"). Spending real money to confirm it buys almost no new information; software validation is free via paper shadow + forced-drift test + property tests. **So the live pilot is re-scoped to test the one thing paper cannot:** _do real CLOB FOK min-size fills systematically differ from paper assumptions (fill rate, realized vs quoted price) by more than X bps?_ This is an **execution-feasibility** hypothesis, not an alpha hypothesis. N is sized to that; the budget is justified as buying execution-cost information for future crypto/maker decisions.

**Instrumentation:**
- Generalize `analyze_performance.py` minimally (keep Brier/ROI/calibration math); add **fill-rate** and **realized-vs-quoted-bps** as first-class metrics alongside ROI.
- **Pre-registered fill-rate early-abort:** if live FILLED rate < 50% of attempted min-size orders in week 1, **HALT and declare EXECUTION-INFEASIBLE**. Verify min-order/tick feasibility against the live CLOB **before** the pilot, not by burning calendar days.
- **Paper-vs-live decay, three arms:** paper loses too → NO EDGE; paper wins, live loses → EXECUTION eats it; live never fills → EXECUTION-INFEASIBLE (abort). The paper shadow is a **pure function** over the recorded proposal + frozen price snapshot — no executor import, no network write, computed offline, never in the `place()` path. Test asserts `paper_fill` writes only the fill-log schema and never calls a `ClobClient`/`web3` method.
- **Stopping rule (resolves edge critic "underpowered" + simplicity critic "SPRT gold-plating"):** for this pilot the stopping rule is the **$49 / 30-day hard HALT plus the $10/day-$20-drawdown breaker** — no SPRT. Do **not** build a sequential-test harness. Before committing live, compute the **achievable N and minimum detectable effect** under the caps and state plainly that an alpha verdict is **underpowered by construction at $49** (so the run targets execution feasibility, not edge). SPRT is reserved for a future instrument that can actually reach sample size — and even then **no automated efficacy crossing may scale capital; human sign-off required**; the drawdown breaker remains the only automated capital action.

---

## 5. Pilot recommendation

**Go live with: POLYMARKET (passive, min-size), scoped to an EXECUTION-FEASIBILITY measurement.** Crypto and stocks stay paper.

**Rationale:** Polymarket resolves and scores cleanly; existing tooling supports it; crypto on $49 is execution-dominated and custody-blocked. The pilot's honest job is to measure real CLOB fill behavior vs paper assumptions — information that directly informs whether any execution-bound strategy (including future crypto) is worth pursuing.

**Hard preconditions before any live fire (all green, in the Routines runtime, not just locally):**
1. Exchange-side position read implemented; a **forced-drift integration test that MUST HALT** passes (release gate, not a checklist line).
2. **On-chain balance/custody verified for Polymarket too:** confirm `get_usdc_balance()` returns the proxy collateral non-None and matches Polygonscan in a dry run. Phase 0's custody check is a precondition for the Polymarket pilot, not just crypto.
3. NOTIONAL_EXPOSURE ceiling wired and unit-tested.
4. Proxy capitalized at **exactly the experiment budget** (withdraw any excess) so an adapter/sizing bug cannot drain more than $49.
5. Min-order/tick feasibility confirmed against the live CLOB.
6. Sizing forced to fixed min-size USD (Kelly-from-confidence path bypassed).

**Caps:** per-instrument `LOSS_LIMIT` $10/day, `MAX_DRAWDOWN` $20; `MAX_ORDERS_PER_FIRE` ≤ 2; hard $49 / 30-day budget auto-HALT; kill switch armed; Slack alerts live.

---

## 6. Phased roadmap

| Phase | Mode | Work | Release gate to advance |
|---|---|---|---|
| **0. Custody/balance truth** | research | On-chain check: where is the $49 (EOA vs Polymarket proxy); confirm `get_usdc_balance()` reads it correctly. Pull free 0x quote for $25/$49 WETH↔USDC cost-%. | Written balance source confirmed vs Polygonscan |
| **1. gate() + position read** | paper | New `gate.py` (pure, two-level); implement real Polymarket exchange position read; shared `kill_switch_active()` helper; delete `live_executor.py` + orphaned tests | Property suite green; forced-drift HALT test passes **in Routines runtime** |
| **2. Exposure ceiling + experiment instrumentation** | paper | NOTIONAL_EXPOSURE ceiling in gate; generalize `analyze_performance.py` for fill-rate + realized-vs-quoted-bps; pure offline paper shadow; pre-register the execution-feasibility hypothesis + fill-rate abort | Exposure-ceiling HALT test green; paper-shadow purity test green; MDE/N written up |
| **3. Polymarket pilot** | **LIVE (small)** | Run with all Section 5 caps + preconditions; paper shadow in parallel | Hits $49/30-day or drawdown HALT, or fill-rate abort; execution-feasibility verdict emitted |
| **4. Crypto (conditional)** | research → paper → maybe live | Only after Phase 3 verdict AND custody resolved AND 0x cost-% acceptable; build per Section 3 constraints | Dedicated EOA; all DEX safety gates + kill-switch-no-broadcast test green |
| **5. Stocks** | **permanently paper / no verdict reachable** | At ~33 trades/yr an alpha verdict is unreachable in any reasonable window; stays paper unless the signal/cadence is fundamentally changed | — (relabeled honestly, not a path to a verdict) |

Only ONE live instrument at a time. Live = Phase 3 only, until crypto is separately justified.

---

## 7. What we are NOT building (scope honesty)

- **No 3-instrument abstraction up front** — no `contracts.py`/adapter Protocol/`SafePipeline`/import-linter. Introduced only when a second live instrument exists.
- **No crypto leg now** — deferred, undesigned, no `web3`/`eth-account` deps until custody resolved and the Polymarket verdict is in.
- **No SPRT/sequential-test harness for the pilot** — the $49/30-day + drawdown HALT is the stopping rule; SPRT only for a future powered instrument, and never auto-scaling capital.
- **No pretense of an alpha pilot** — the live run measures **execution feasibility** (fill rate, realized-vs-quoted bps); alpha is underpowered by construction at $49 and we say so.
- **No trusting the raw LLM probability for sizing** — fixed min-size USD; the confidence-Kelly path is bypassed; the existing calibration gate is the quality filter.
- **No "calibration layer" build** — it already exists (`calibrate.py` + the live confidence gate); we reuse it.
- **No reliance on the realized-loss breaker as the exposure cap** — a notional-exposure ceiling is the real cap for a slow-settling instrument.
- **No paper shadow in the execution path** — pure offline function, no executor import, no network write.
- **No sizing-core refactor / no collapsing `size_order`** — delete only the dead `live_executor` path and its orphaned tests.
- **No stocks verdict roadmap** — relabeled permanently-paper at current cadence.
- **No multi-instrument simultaneous live trading; no Polymarket market-making/latency-arb; no altcoin/carry strategies.**

---

## Open decisions for the owner (only the human can decide before build starts)

1. **Spend real money at all?** The alpha verdict is underpowered by construction at $49, so the live Polymarket run only buys **execution-feasibility** information. Accept that framing and proceed, OR validate the whole pipeline on the **paper shadow + forced-drift test for $0** and save the money for a future instrument with a genuinely unknown verdict. (The edge critic argues hard for the $0 option; it is defensible.)
2. **Custody location of the $49** — confirm EOA vs Polymarket proxy, and agree to **capitalize the proxy at exactly $49** (withdraw any excess) so a bug cannot drain more.
3. **Define the exact execution-feasibility threshold X** — what realized-vs-quoted-bps delta or fill-rate counts as "paper assumptions are wrong"? The pre-registered hypothesis; only you set the number that makes the run worth it.
4. **Crypto go/no-go trigger** — after the pilot, what 0x cost-as-%-of-notional figure (from the free Phase 0 quote) is low enough to justify building the DEX leg at all?
5. **Dedicated fresh EOA for any future crypto** — confirm you'll fund a new wallet rather than reuse the Magic/Polymarket signing key.
