# Auto-Trading Specification
## Options Analyzer — Automated Options Trading (Phase 3)

| | |
|---|---|
| **Status** | Draft v0.4 (review: minimum risk, maximum return) |
| **Date** | 2026-11-21 |
| **Broker** | Interactive Brokers (TWS / IB Gateway API) |
| **Scope** | Manage existing positions **and** open new ones |
| **Core strategies** | (1) Covered calls on holdings. (2) Short puts on **cash-settled index options (XSP / SPXW)** as the main put engine. (3) Single-stock short puts as a secondary engine. (4) Index put credit spreads. (5) A permanent tail hedge. |
| **Not in v1** | Naked calls, iron condors (§1.3) |
| **Autonomy** | Staged rollout: paper → shadow → approve-each → auto-manage → auto-open |

---

## 1. Summary

Options Analyzer today reads IB CSV exports, computes Greeks, margin, and risk
flags, and shows recommendations (`src/analysis/risk.py`,
`src/analysis/margin.py`, `data/config/settings.json`). It never sends an
order.

This spec adds a trading engine that:

1. Connects to IB live (positions, quotes, account values) instead of CSV.
2. Sells option premium against a **fully invested stock portfolio**, using a fixed **risk budget**, and spends that budget on the trades with the best premium for the risk they add.
3. Rolls positions instead of letting them be assigned, and prefers instruments that **cannot be assigned at all**.
4. Keeps the account far from margin at all times, and de-risks automatically when it isn't.
5. Passes every order through a **risk gate** that strategy code cannot bypass, and records why each order was placed.
6. Gains autonomy in stages, each with measurable exit criteria.

Guiding principles:
- **Size by stress loss, not by margin.** Margin is what IB charges today; stress loss is what the account loses in a crash. The bot budgets the second.
- **Risk-reducing actions get autonomy before risk-adding ones.**
- **Prefer structural fixes over procedural ones.** An index option that can't be assigned beats a guard that tries to prevent assignment.

### 1.1 User requirements

These come directly from the account owner. Where they conflict with defaults elsewhere in this document, **they win**.

| # | Requirement | Where it is implemented |
|---|---|---|
| **R1** | Keep a margin cushion of **at least $100,000 at all times**. | §6.1, §6.5 |
| **R2** | **Minimize transactions** to cut fees. | §5.8, §7.5 |
| **R3** | Always look for **covered calls with high premium and low delta**, and for **puts to sell**. | §5.9 |
| **R4** | **Track** options to find the right moment to sell. | §5.10 |
| **R5** | When margin requirements are not met, or a position should be closed, **close it automatically**. | §6.5, §5.7 |
| **R6** | **Connect to the Interactive Brokers account through its API.** | §4.3 |
| **R7** | **No assignments.** Roll when needed. | §5.2 (index puts can't be assigned), §5.11 |
| **R8** | **No cash; everything is invested.** Puts are backed by margin on the stock portfolio. | §5.2, §6.2 |
| **R9** | **Manage the risk and always stay far from the margin limit.** | §6.1, §6.2, §6.5, §6.6 |

**R1 definition (confirmed):** "margin" = IB **Excess Liquidity** (EL). The floor is **$100,000**.

**R9 definition:** "far from margin" means all of these hold:
- **Cushion** (EL ÷ Net Liquidation) ≥ 40% for any new trade.
- Under a −20% market drop with IV +50% and margin requirements expanded by 1.5×, **EL stays ≥ $100k**.
- The options book's loss in that scenario stays within the **risk budget** (§6.2).

### 1.2 What changed in v0.4 and why

| # | Change | Why | Effect |
|---|---|---|---|
| 1 | **Fixed limits that would have blocked or halted everything.** The stress-loss cap, beta-delta cap, daily-loss halt, and drawdown halt were measured on the whole account. A fully invested portfolio breaches them from stock moves alone. They now apply to the **options book** (§6.1, §6.4). | v0.3 would have rejected every trade on a −25% stress (stocks alone lose 25%), and halted on any normal −3% day. | Bug fix |
| 2 | **Index options (XSP / SPXW) become the main put engine** (§5.2). They're European-style and cash-settled. | They **can't be assigned**, so R7 holds structurally. A broad index carries less single-name gap risk. Using PM-settled series avoids the Friday-open settlement gap. (The US 60/40 tax benefit doesn't apply: the owner is taxed in Israel.) | Less risk, more after-tax return |
| 3 | **Risk-budget allocation** (§6.2): the options book may lose at most **10% of NLV** in the stress scenario, and the scanner ranks trades by **edge per unit of stress loss** (§5.9). | Stress capacity is the scarce resource. Spending it where premium per unit of crash risk is highest maximizes return for a fixed level of risk. | More return at the same risk |
| 4 | **Only sell rich volatility:** require IV ≥ 1.1× realized volatility, and compute expected value under realized volatility (§5.9). | Sellers earn the gap between implied and realized volatility. When there's no gap, there's no edge, only risk. | Fewer, better trades |
| 5 | **Volatility-regime sizing** (§6.6): smaller size when VIX is very low (thin premium, underpriced crash risk). New: **sell after a volatility spike starts to fade**, instead of only blocking when VIX > 35. | Premium and edge are highest right after spikes. Low-VIX periods are when sellers get hurt. | More return, less tail risk |
| 6 | **Combined exposure per name** (§6.1): stock + short puts + call deltas count together against the per-name cap. Single-stock puts aren't sold on names already at the cap. | v0.3 allowed selling puts on a stock you're already fully invested in, which doubles the exposure. | Less concentration |
| 7 | **Earlier, cheaper rolls** (§5.11): roll puts at delta 0.35 and covered calls at 0.40, not 0.50. | Rolling earlier still gets a credit **and** a better strike. Waiting until 0.50 often leaves only "close at a loss." | Fewer losing closes |
| 8 | **Smarter early closes** (§5.8): a "replacement test" closes a position when its remaining premium per day is far below what a new trade would earn on the same risk, net of fees. A "gamma exit" closes near-expiry positions where little premium is left but risk is rising. | Holding the last few cents of premium ties up risk budget and carries the most gamma risk. R2 is respected: a close happens only when it pays for itself after fees. | More return per unit of risk |
| 9 | **Covered calls on ≤ 50% of each holding by default**, skipped in strong uptrends, and **no forced sale at 21 DTE** (§5.3, §5.10). | Calls cap upside, and rolling calls in a rally costs money. Partial coverage keeps upside and halves roll risk. R3 asks to *identify* opportunities, not to always sell. | Better total return |
| 10 | **Tail hedge is now in the spec** (§5.12), counted in the stress test, and never closed by margin restoration. | It turns crash losses from fatal into survivable, and it supports EL when R1 is under the most pressure. | Much less tail risk |
| 11 | **Expiration ladder** (§6.1): ≤ 35% of short-put notional in any one expiration. | It spreads timing risk. One bad Friday can't hit the whole book. | Less risk |
| 12 | **Event guard for open positions** (§5.11): short options with earnings before expiry are rolled past the event or closed if delta > 0.25. Rolls avoid expiries that span earnings. | v0.3 only blocked new entries across earnings. | Less gap risk |
| 13 | **Margin expansion in the stress test** (×1.5) (§6.2). | IB raises requirements in volatile markets, especially under Portfolio Margin. | More realistic R1 |
| 14 | **Never sell core stock holdings automatically** (§6.5). Margin restoration closes options only; the only exception is shares that came from an assignment. | R8: the portfolio stays invested. | Matches owner intent |
| 15 | **Margin interest counted as a cost** (§7.5). | With no cash, a debit (loss, roll debit) goes onto margin and costs interest. | Correct net returns |
| 16 | **Naked calls and iron condors removed from v1** (§1.3). | They add risk the portfolio doesn't need. The stock portfolio is long, covered calls already supply the call-side income, and naked calls are the only unlimited-loss trade. | Less risk and less code |
| 17 | **Stage 0 acceptance metrics** (§11): the overlay must beat buy-and-hold on a risk-adjusted basis, out of sample. | It stops the bot going live on curve-fitted parameters. | Less model risk |

### 1.3 Out of scope for v1
- **Naked calls.** Unlimited loss; not needed for a long-biased income overlay.
- **Iron condors.** The call side duplicates covered calls, and the put side is already covered by index puts and spreads.
- Both can come back in a later version, behind their own paper stage.

---

## 2. Goals and Non-Goals

### Goals
- G1: Earn option income on a fully invested portfolio while keeping the account **far from margin** (R9) and **never assigned** (R7).
- G2: Maximize **premium earned per unit of stress loss**, net of fees and margin interest.
- G3: Make every action explainable. Each order links to its signal, inputs, and risk checks.
- G4: Fail safe. On any unknown state (disconnect, stale data, reconcile mismatch), stop opening and keep only protective actions.
- G5: Reuse the existing analysis code. Do not fork the logic.

### Non-Goals (v1)
- High-frequency or intraday scalping.
- Directional long-option speculation (the tail hedge is insurance, not speculation), futures options, non-US underlyings.
- Multi-user operation.
- Machine-learning signals. Rules are explicit and configurable.
- Tax optimization. The bot logs the data; it doesn't act on it.

---

## 3. Prerequisites

| Requirement | Why |
|---|---|
| IB margin account with options **Level 4** | With no cash (R8), short puts are naked puts backed by margin. |
| **Margin type: Reg-T** (owner's current understanding; the bot verifies it at startup, §4.3) | Reg-T naked put margin: broad index ≈ max(15% of underlying − OTM amount, 10% of strike) + premium; stocks ≈ max(20% − OTM, 10%) + premium. Under Reg-T, the binding limits are Cushion and the risk budget, not the put margin itself. Portfolio Margin can be revisited later. |
| OPRA, US equity, and **CBOE index (SPX/XSP/VIX)** real-time market data | Live data is needed to price orders and to read VIX for regime sizing. |
| IB Gateway + IBC on an always-on host (§15) | IB forces a daily restart; the bot must survive it. |
| Separate IB **paper** account, set up fully invested like the live one | Stage 1. |

---

## 4. Architecture

```
                         ┌──────────────────────────┐
                         │   IB Gateway / TWS API   │
                         └────────────┬─────────────┘
                                      │ ib_async
                   ┌──────────────────▼──────────────────┐
                   │          Broker Adapter              │
                   │  positions · quotes · chains ·       │
                   │  account values · orders · fills ·   │
                   │  whatIf margin                       │
                   └──┬────────────────┬──────────────┬──┘
                      │                │              │
          ┌───────────▼───┐   ┌────────▼───────┐  ┌───▼──────────────┐
          │ Market State  │   │ Portfolio State│  │ Order Manager    │
          │ quotes, IV/RV,│   │ existing       │  │ place, walk,     │
          │ chains, VIX,  │   │ Portfolio      │  │ cancel, fills,   │
          │ calendars     │   │ model, live    │  │ reconcile        │
          └───────┬───────┘   └────────┬───────┘  └───▲──────────────┘
                  │                    │              │ approved orders
          ┌───────▼────────────────────▼───────┐      │
          │          Strategy Engine            │      │
          │  Position Manager · Assignment &    │      │
          │  Event Guard · Scanner/Tracker ·    │      │
          │  Tail Hedge                         │      │
          └─────────────────┬───────────────────┘      │
                            │ TradeIntents             │
          ┌─────────────────▼───────────────────┐      │
          │ Risk Gate: hard limits · stress &   │──────┘
          │ risk budget · regime sizing ·       │
          │ whatIf margin · kill switch         │
          └─────────────────┬───────────────────┘
          ┌─────────────────▼───────────────────────────┐
          │ Journal (SQLite) → Flask UI + alerts        │
          └─────────────────────────────────────────────┘
```

### 4.1 Module layout (`src/trading/`)

| Module | Responsibility | Reuses |
|---|---|---|
| `broker/ib_adapter.py` | The only module that imports the IB library: connection, contracts, chains, quotes, `whatIfOrder`, orders, events. | — |
| `broker/fake_adapter.py` | In-memory broker for tests and backtests. | — |
| `market_state.py` | Quote cache and staleness, IV/RV, IV rank, VIX, earnings / ex-div calendar, market hours. | `analysis/greeks.py` |
| `portfolio_state.py` | Live IB data → existing `Portfolio` / `OptionPosition` models. | `models/` |
| `indicators.py` | MAs, RSI, ATR, IV rank/percentile, RV20/60, term structure, skew. | — |
| `strategies/covered_call.py`, `index_put.py`, `stock_put.py`, `put_spread.py`, `tail_hedge.py` | Strategy rules (§5). | `analysis/risk.py` |
| `guards/assignment_guard.py`, `guards/event_guard.py` | Roll / close triggers and roll builder (§5.11). | — |
| `scanner.py`, `tracker.py` | Opportunity scoring and timing (§5.9–5.10). | — |
| `risk/stress.py`, `risk/budget.py`, `risk/gate.py`, `risk/regime.py` | §6. | `analysis/margin.py` |
| `order_manager.py`, `fees.py` | §7. | — |
| `scheduler.py`, `journal.py`, `kill_switch.py`, `notifier.py` | §8, §9, §6.4. | `data/storage.py` |

The engine runs as its own process (`python -m src.trading.run`), separate
from the Flask app. They share only the journal and the kill-switch file.
New UI pages: **Bot Status, Opportunities, Pending Approvals, Order Log, Risk Budget, Config**.

### 4.2 Library choice
**`ib_async`** (the maintained successor of `ib_insync`). IB types stay inside the adapter.

### 4.3 IB API connection (R6)

| Item | Spec |
|---|---|
| Endpoint | IB Gateway on `127.0.0.1`. Live 4001, paper 4002. |
| Supervision | IBC handles login and the daily restart. 2FA via IB Key about once a week; the bot sends a high-priority alert when it's needed. |
| Client IDs | `1` engine (the only one that places orders), `2` web UI (read-only), `3+` scripts. |
| Read-only first | IB's "Read-Only API" setting stays **on** in Stages 0–2. That's a broker-side guarantee that no order can be placed. |
| Data | `reqPositions`, `reqAccountSummary` (`NetLiquidation`, `ExcessLiquidity`, `MaintMarginReq`, `InitMarginReq`, `AvailableFunds`, `Cushion`), `reqPnL`, `reqSecDefOptParams`, `reqMktData` (generic ticks 100/101/104/106), `reqExecutions`, `reqHistoricalData`. |
| Pre-trade margin | `whatIfOrder` for every opening order and roll. |
| Margin-type check | At startup, compare `whatIfOrder` margin on a reference short put with the Reg-T and PM formulas, and refuse to trade if the result doesn't match `margin_type` in config. |
| Market data | Live only. On delayed data the bot runs read-only. |
| Reconnect | Backoff 1s → 60s. On reconnect: reload positions, orders and executions, and reconcile before acting. |
| Pacing | ≤ 50 msg/s; respect the market-data line limit; batch and snapshot wide scans. |

---

## 5. Strategy Rules

All numbers are **defaults** in `data/config/trading.json` (§10).

### 5.1 Universe and common entry filters

| Filter | Default |
|---|---|
| Single stocks | Watchlist only: large, liquid, profitable names you'd hold through a 30% drop |
| Underlying avg daily volume | ≥ 1M shares (stocks) |
| Option open interest at strike | ≥ 500 (stocks), ≥ 100 (XSP) |
| Bid-ask spread | ≤ 10% of mid, and ≤ $0.10 when mid < $1.00 |
| **Volatility edge** | IV ÷ max(RV20, RV60) ≥ **1.10** |
| IV rank (52-week) | ≥ 25 for index, ≥ 30 for stocks |
| Earnings | No new single-stock position whose expiry spans earnings |
| Ex-dividend | No covered call whose expiry spans an ex-date while extrinsic < dividend |
| Time of day | 09:45–15:45 ET |
| Regime | Sizing per §6.6 |

### 5.2 Short puts: index engine (primary) and single-stock engine (secondary)

**Index puts (XSP, or SPXW for larger size):**
- European-style and cash-settled: **no early assignment and no shares**, so R7 holds by construction. Use **PM-settled** series (XSP, SPXW) and avoid AM-settled SPX monthlies.
- Entry: 30–45 DTE, delta **0.10–0.16**, chosen by the scanner score (§5.9).
- An ITM index put at expiry settles in cash (a realized loss, not an assignment). The guard (§5.11) still rolls or closes it beforehand, so losses are taken under control rather than at the settlement price.
- XSP is 1/10 of SPX (one contract ≈ $60k notional at SPX 6000), which allows fine-grained sizing. Switch to SPXW once the size reaches 10+ XSP contracts per expiry, for tighter spreads and lower fees per dollar of notional.

**Single-stock puts (secondary):**
- Only on watchlist names where combined exposure (§6.1) is under the per-name cap. Typically names you'd like to own **more** of.
- Entry: 30–45 DTE, delta **0.10–0.20**, no earnings inside the expiry.
- Defended by the Assignment Guard (§5.11).

**Both:** no cash collateral (R8). Every put counts toward short-put notional, the risk budget, and the expiry ladder (§6.1–6.2).

### 5.3 Covered calls
- **Coverage:** set **per symbol**. Index ETFs (SPY, QQQ, QQQM) default to **50%** (owner-confirmed). Single stocks default to 50% until set per symbol (`cc_max_coverage` in the watchlist), and never more than `floor(shares / 100)` contracts. Uses the existing coverage logic (commit `2eb3ff0`).
- **Entry:** 21–45 DTE, **delta 0.08–0.15**, strike ≥ cost basis (configurable).
- **QQQM (owner holds it; covered calls wanted):** QQQM options are thinly traded, so they get their own rules:
  - **Liquidity override:** OI ≥ 50 and spread ≤ 20% of mid (instead of the §5.1 defaults).
  - **Fair value from QQQ:** QQQM tracks the same index as QQQ. The bot prices each QQQM option with QQQ's implied vol at the same moneyness and DTE, and quotes limit orders around that fair value rather than QQQM's wide displayed mid. Orders are worked patiently (§7.1 steps every 5 min instead of 60 s, for up to 1 hour).
  - **Lower delta and earlier rolls:** delta ≤ 0.10 and roll at delta 0.30, because rolling an illiquid option late is expensive.
  - **Calls on QQQ never stand in for QQQM coverage.** IB would margin them as naked calls.
- **Skip in strong uptrends** unless IV rank ≥ 60. A strong uptrend means price above a rising 50-day MA **and** a 20-day return > +8%. Selling calls into momentum mostly produces costly rolls.
- **Exit / defend:** hold to expiry unless §5.8 says to close, and roll up and out per §5.11. **Shares are never called away** (R7). If no acceptable roll exists, buy back the call. The shares' gain offsets the loss on the call.

### 5.4 Index put credit spreads
- Used **instead of naked index puts** when (a) Cushion is 40–50%, so naked margin is too expensive, or (b) VIX > 30 (§6.6). That gives defined risk when risk matters most.
- Short leg delta 0.10–0.16; width set so max loss ≤ 1% of NLV per spread position; credit ≥ 20% of width.
- Always one combo order. Same exit, roll, and guard rules as §5.2.

### 5.5 (removed in v0.4: iron condors, see §1.3)

### 5.6 (removed in v0.4: naked calls, see §1.3)

### 5.7 Position management loop
Every 5 minutes, each open position runs through the existing
`RiskAnalyzer.analyze_position`, then the guards (§5.11), then the
strategy's `manage()`.

| Flag | Action |
|---|---|
| `PROFIT_TARGET_REACHED` | Close only if the replacement test (§5.8) passes |
| `EXPIRING_SOON` | Gamma exit or guard rules (§5.8, §5.11) |
| `HIGH_DELTA_SHORT_*` | Roll per §5.11 |
| `ITM_OPTION` | Roll or close per §5.11. Never accept assignment |
| `LARGE_LOSS` | Chain loss rule (§5.11) |

`risk.py` upgrades: a high-delta check for **short puts**, plus
`EX_DIVIDEND_ASSIGNMENT_RISK`, `PIN_RISK`, and `EARNINGS_BEFORE_EXPIRY` flags, and
`LARGE_LOSS` measured relative to the credit received.

Legacy and manual positions are monitored and alerted, but traded only if you opt them in.
**Exception:** margin restoration (§6.5) may close any **option** in the account.

### 5.8 Fee-aware exits (R2)

Costs: about **$0.65/contract + exchange/regulatory fees** for stock and ETF
options (index options carry an extra CBOE index fee, modeled in `fees.py`),
plus the bid-ask spread, plus **margin interest** on any debit (§7.5).
Expiration costs nothing.

| Rule | Default |
|---|---|
| **Hold to expiration** is the default. | on |
| **Replacement test:** close early when (a) ≥ 50% of the credit is captured, **and** (b) the position's remaining premium per day per unit of stress loss is < 50% of the best available candidate's, **and** (c) closing + reopening fees are < 10% of the new candidate's credit. The freed budget is redeployed right away. | on |
| **Gamma exit:** DTE ≤ 10, remaining extrinsic < 20% of the original credit, and |delta| > 0.25 → close. Little premium is left, and risk is rising. | on |
| Never buy back an option at mid ≤ $0.05, except for the guards or margin restoration | on |
| Min net credit per new trade | **$20/contract** and ≥ 5× round-trip fees (v0.3's $50 blocked most low-delta covered calls) |
| Monthly cycles preferred; laddered across expirations (§6.1) | on |
| Rolls only as one combo order for a net credit | on |
| Max new opening orders per day | 5 |

The journal records actual commissions; the dashboard shows fees and margin
interest as a percentage of premium (target < 4% combined).

### 5.9 Opportunity Scanner (R3)

Runs every 15 minutes in market hours over three queues: **covered calls**
(holdings with uncovered shares under the coverage cap), **index puts**, and
**single-stock puts** (watchlist names under their cap).

For each candidate (underlying × expiry × strike in the delta band):

```
credit_net    = mid − round-trip fee estimate
σ_real        = max(RV20, RV60) × 1.10                 # conservative realized vol
EV            = credit_net − E_real[ max(0, intrinsic at expiry) ]
                # expected payout under a realized-vol (not implied-vol) distribution
                # → positive only when implied vol is rich vs. realized
stress_loss   = loss of this position in the §6.2 budget scenario
                # (−20%, IV +50%; single stocks scaled by beta, floor 1.0)
edge_score    = EV / stress_loss × 365 / DTE           # annualized edge per unit of crash risk
timing_mult   = from the tracker (§5.10), 0.8–1.3
score         = edge_score × timing_mult × liquidity_factor
```

- **Hard filters:** EV > 0; vol edge ≥ 1.10 (§5.1); delta bands per strategy; credit ≥ min net credit.
- **Allocation:** the risk budget (§6.2) is filled greedily by score, subject to every §6.1 limit. Higher-scoring trades get the budget first.
- **R3 "high premium" check (display):** annualized premium yield ≥ 15% for covered calls (on share value) and ≥ 20% for puts (on whatIf margin).

### 5.10 Tracking: when to sell (R4)

**Opportunity Tracker.** Stores every scan result, so the bot sees how the
premium for a given delta moves over hours and weeks. `timing_mult` rises when:

| Signal | Covered calls | Puts |
|---|---|---|
| IV rank ≥ 50, or IV in the top third of its 30-day range | ✓ | ✓ |
| Underlying move | after an up move (≥ +1 ATR in 1–3 days, or near the 20-day high) | after a down move (≤ −1 ATR, or near support) |
| Score in the top quartile of its own 20-day history | ✓ | ✓ |
| VIX spike fading (§6.6) | — | ✓ (strongest signal for index puts) |

**No forced sales.** If timing never fires in a cycle, nothing is sold.
Unused risk budget is safer than budget spent on thin premium.

**Trend & regime indicators** (stored with every scan, used by §5.3, §6.6):
- **20/50/200-day MAs and their slopes:** trend direction. Uptrend → skip or lower the covered-call delta; downtrend → move puts further OTM or skip single-stock puts.
- **RSI(14), distance from the 20-day high/low in ATRs:** sell calls into strength, puts into weakness.
- **IV rank and percentile, IV ÷ RV:** whether premium is rich.
- **Term structure (front vs. second month):** inversion means stress, so use the regime rules.
- **Put/call skew:** rich puts favor put sales.
- **VIX level and change:** regime (§6.6).

**Decay Tracker.** For each open position: captured premium vs. the theoretical
theta curve, delta, extrinsic value, and remaining premium per unit of stress
loss. Feeds the replacement test and gamma exit (§5.8).

### 5.11 Assignment & Event Guard (R7)

Checks every short option every 5 minutes. Index options (§5.2) can't be
assigned, but the guard still manages them (expiry settlement and loss control).

**Roll triggers (any one fires):**

| Trigger | Puts | Covered calls |
|---|---|---|
| **Early roll** while it's still cheap | delta ≥ **0.35** | delta ≥ **0.40** |
| ITM and extrinsic < $0.15 (or < 0.5% of strike), the early-assignment zone (single stocks) | ✓ | ✓ |
| ≤ 5 DTE and underlying within 2% of the strike | ✓ | ✓ |
| Ex-dividend before expiry while ITM and extrinsic < dividend + $0.05: roll by 15:30 ET the day before | — | ✓ |
| **Earnings before expiry** (positions opened before an earnings date was set, or rolled into one) and delta > 0.25: roll past the event, or close, the day before | ✓ | ✓ |
| Expiration day, ITM or within 1% of the strike: roll or close by 15:00 ET | ✓ | ✓ |

**Roll builder:**
1. One combo order: buy back the current option and sell the new one. Next expirations 30–60 DTE, max 90, that **don't span earnings**.
2. Strike the same or better (puts lower, calls higher). Choose the best strike improvement with **net credit ≥ $0.05/contract**.
3. **Never add contracts.**
4. The roll passes the Risk Gate like a new trade, and it must not increase the position's stress loss.
5. Max **4 rolls** per chain. A chain whose cumulative loss exceeds **3× the original credit** is closed. If no roll qualifies, close.

**If a single-stock assignment still happens:** a put assignment → sell the shares at the next open; a call assignment → buy the shares back at the next open (R8). Alert immediately, and log it on the chain.

### 5.12 Tail hedge

| Item | Default |
|---|---|
| Instrument | SPY puts (or XSP / SPXW puts), **10–15% OTM**, 60–120 DTE |
| Budget | **5–10% of premium collected** in the trailing month, bought monthly in one order |
| Roll | When DTE < 30, or when the hedge has gained ≥ 3× its cost (take part of the profit, then re-strike) |
| In the stress test | Counted. It offsets the options book's stress loss, which frees risk budget, so the hedge partly pays for itself in allowed size |
| Margin restoration | **Never closed** by §6.5. It's the position that protects EL in a crash |
| Monetization rule | After a crash (VIX > 40), sell half the hedge once it's worth ≥ 5× cost, and use the gain to support EL and the cushion |

---

## 6. Risk Gate

A pure function of (intent, portfolio, market, config), unit-tested exhaustively.

### 6.1 Hard limits (every opening intent and roll)

| Limit | Default |
|---|---|
| **EL after the trade** (whatIf) | ≥ **$125,000** (R1 floor $100k + $25k buffer) |
| **Cushion after the trade** | ≥ **40%** (R9) |
| **EL in the §6.2 scenario** (stocks + options + hedge, margin ×1.5) | ≥ **$100,000** |
| **Options-book stress loss** (§6.2) | ≤ the **risk budget**: 10% of NLV, scaled by regime (§6.6) |
| **Total short-put notional** | ≤ **50% of NLV** |
| **Per-name combined exposure**: stock value + short-put notional × delta-adjusted, − covered-call delta | ≤ **15% of NLV** (the existing stock weights may already exceed this; the rule then blocks **adding** to the name) |
| Index put notional (XSP/SPX) | ≤ 40% of NLV (within the 50% total) |
| Per-sector combined exposure | ≤ 35% of NLV, same rule as per-name |
| **Expiry ladder**: short-put notional in any one expiration | ≤ **35%** of total short-put notional |
| **Options-book beta-weighted delta** (excluding stocks) | ≤ +0.35 × NLV in SPY-dollar terms |
| Max contracts per order | 10 (index: 5 SPXW / 20 XSP) |
| Max new opening orders per day | 5 |

Closing and risk-reducing intents skip the allocation limits (sanity checks still apply).

### 6.2 Stress test and risk budget

**Scenarios** (stocks + options + hedge revalued together, single stocks scaled by beta):
- Market −25%, −20%, −15%, −8%, +8%, +15%, +25%
- IV +50% (relative) together with the −15%, −20%, and −25% moves
- **Margin requirements ×1.5** in every down scenario (IB raises requirements in stress)
- A single-stock **−35% gap** on each name with a short put (idiosyncratic risk)

**Budget scenario:** −20% with IV +50%.

**Checks:**
1. **Options-book loss** (short options + spreads + hedge, excluding stock P&L) in the budget scenario ≤ **risk budget** (10% NLV × regime multiplier).
2. **Account EL** in the −25%/IV +50%/margin ×1.5 scenario ≥ $100k.
3. Each single-stock −35% gap: the loss on that name's short options ≤ 2% of NLV.

Repricing uses `py-vollib`. The Risk Budget page shows the budget used and free.

### 6.3 Sanity checks (every order)
Quote < 5s old; limit price within the bid/ask; quantity ≤ the position being closed; contract matches the intent; no side flip; no duplicate working order.

### 6.4 Kill switch and circuit breakers

All loss-based triggers use **options-book P&L**, because stock moves alone would trip them on normal days.

| Trigger | Action |
|---|---|
| Manual: UI button, `touch data/trading/HALT`, or CLI | Cancel opening orders, stop new entries, keep guards and closes (unless `HALT_ALL`) |
| Options-book daily loss > **1.5% of NLV** | Halt opening for the day |
| Options-book drawdown from its high-water mark > **5% of NLV** | Risk budget halved until recovered to −2% |
| Options-book drawdown > **8% of NLV** | Halt opening until a human resets it |
| Broker disconnect > 60s | No new orders; reconcile on reconnect |
| Reconciliation mismatch | Halt all trading; alert |
| Unhandled engine exception | Halt opening; alert |

Halts persist across restarts; only a human clears them.

### 6.5 Margin floor monitor and automatic restoration (R1, R5)

Every **60 s** during market hours, on EL and Cushion.

**IB refreshes account values only about every 3 minutes.** Between
refreshes, the monitor estimates EL from the last IB value plus the change
in real-time P&L (`reqPnL`, about 1 s updates) since that refresh. The
estimate is **conservative**: P&L losses reduce estimated EL in full, and
gains are ignored until IB confirms them. A margin state can therefore turn
worse between refreshes, but only IB's own numbers can make it better. If
the account values are older than 240 s, the monitor treats the state as at
least **Warning**.

| State | Condition | Action |
|---|---|---|
| Normal | EL ≥ $125k and Cushion ≥ 40% | — |
| Warning | EL $100–125k, or Cushion 30–40% | No new openings; new put exposure only as spreads (§5.4) that don't reduce EL. Alert. |
| **Breach** | EL < $100k, or Cushion < 30% | Within 2 min, **auto-close options** until EL ≥ $110k and Cushion ≥ 35% |
| Emergency | EL < $50k, or Cushion < 15% | Same as breach, priced aggressively (up to the natural price); alert on every channel |

**Close order:**
1. Positions already flagged (ITM, high delta, chain-loss).
2. Then by **margin freed ÷ cost to close**, the smallest set that reaches the target. Whole positions are preferred.
3. **Never closed:** the tail hedge; and stock holdings (R8). Exception: shares that came from an assignment.
4. Re-check after each fill; stop when the target is reached.

Out-of-hours breach: alert immediately and act at 09:31 ET.
Restoration closes are automatic from **Stage 3**, with a notification for each.

### 6.6 Volatility-regime sizing

The risk budget (10% NLV) is multiplied by:

| Regime | Multiplier | Entry style |
|---|---|---|
| VIX < 13 (thin premium, complacency) | **0.5** | Lowest deltas only |
| VIX 13–20 | 0.8 | Normal |
| VIX 20–30 | 1.0 | Normal, best edge |
| VIX > 30 and **rising** (above its 5-day average) | **0** (no new risk) | Guards and hedge only |
| VIX > 30 and **≥ 15% below its 5-day high** (spike fading) | **0.6** | **Spreads only** (§5.4), delta ≤ 0.10, nearest ladder slots first |
| Options-book drawdown > 5% (§6.4) | × 0.5 | — |

---

## 7. Order Management

### 7.1 Pricing
- Limit orders only. Start at mid; after 60s, step 10% of the spread toward the natural price; at most 4 steps; never worse than mid ± 40% of the spread (emergency: up to natural).
- If unfilled, retry next cycle. After 3 failed cycles, alert and skip until the next day.
- Multi-leg trades and rolls go out as **one combo order**.

### 7.2 Idempotency
A deterministic intent ID goes in `orderRef`. On startup, open orders and today's executions are matched to intents before anything else runs.

### 7.3 Fills and assignment
Partial fills update positions immediately. Assignments and exercises (position changes not caused by a bot order) are logged and handled per §5.11.

### 7.4 Reconciliation
Every cycle, journal-expected positions are compared with IB. Any unexplained difference halts trading until a human acknowledges it. Manual positions are tagged "unmanaged."

### 7.5 Fees and financing
`fees.py` models commission, exchange (including CBOE index fees), ORF, OCC and TAF fees, calibrated from `commissionReport`, plus **margin interest** on negative cash at the account's IB benchmark rate. All decisions use **net** figures.

---

## 8. Scheduling

| Time (ET) | Job |
|---|---|
| 09:00 | Load calendars (earnings, ex-div, holidays), refresh indicators, verify connection and permissions |
| 09:31–16:00, every 60 s | Margin floor monitor (§6.5) |
| 09:45–15:45, every 5 min | Management cycle + guards |
| 09:45–15:45, every 15 min | Scanner + tracker; orders only when timing fires and budget is free |
| 15:00, expiration days | Expiry sweep (§5.11) |
| Monthly, first trading day after the standard expiration | Tail hedge purchase / roll (§5.12) |
| 16:15 | Reconcile, snapshot P&L / Greeks / budget, daily report |

NYSE calendar, including holidays and early closes.

---

## 9. Persistence and Observability

- **Journal** (SQLite, append-only): `intents`, `risk_checks`, `orders`, `order_events`, `fills`, `chains`, `positions_snapshot`, `account_snapshot`, `budget_snapshot`, `scan_results`, `halts`, `config_versions`.
- **Every intent records** the strategy, trigger, quotes, Greeks, IV/RV, score components, every risk check, and the config version.
- **Alerts:** fills, rejects, halts, margin states, assignments, reconcile mismatches, pending approvals, IB re-login needed, and the daily report.
- **Metrics:** options-overlay P&L and its Sortino, budget used, premium per unit of stress loss, win rate per chain, roll rate, losing-chain rate, fees and interest as % of premium, hedge cost and payoff, overlay vs. buy-and-hold, and an annual **realized P&L report** (per trade, USD, with fees) for tax filing.

---

## 10. Configuration

`data/config/trading.json`, validated on load (unknown keys rejected) and versioned into the journal.

```json
{
  "mode": "paper",
  "account_id": "DU1234567",
  "margin_type": "reg_t",
  "enabled_strategies": {
    "covered_call": {"manage": true, "open": true},
    "index_put":    {"manage": true, "open": true},
    "stock_put":    {"manage": true, "open": true},
    "put_spread":   {"manage": true, "open": true},
    "tail_hedge":   {"manage": true, "open": true}
  },
  "watchlist": {
    "SPY":  {"strategies": ["covered_call"], "cc_max_coverage": 0.5},
    "QQQ":  {"strategies": ["covered_call"], "cc_max_coverage": 0.5},
    "QQQM": {"strategies": ["covered_call"], "cc_max_coverage": 0.5, "cc_delta": [0.06, 0.10],
             "call_roll_delta": 0.30, "min_oi": 50, "max_spread_pct": 0.20,
             "fair_value_proxy": "QQQ", "order_step_s": 300},
    "AAPL": {"strategies": ["covered_call", "stock_put"], "cc_max_coverage": 0.5}
  },
  "index": {"underlyings": ["XSP", "SPXW"], "settlement": "PM", "switch_to_spx_at_contracts": 10},
  "entry": {
    "dte_min": 30, "dte_max": 45, "cc_dte_min": 21,
    "cc_delta": [0.08, 0.15], "index_put_delta": [0.10, 0.16], "stock_put_delta": [0.10, 0.20],
    "cc_max_coverage": 0.5, "cc_skip_strong_uptrend": true,
    "min_vol_edge": 1.10, "ivr_min_index": 25, "ivr_min_stock": 30,
    "min_net_credit_per_contract": 20
  },
  "exit": {
    "hold_to_expiration": true,
    "replacement_test": {"min_captured": 0.5, "rel_efficiency": 0.5, "max_fee_ratio": 0.10},
    "gamma_exit": {"dte": 10, "max_remaining_extrinsic": 0.20, "min_delta": 0.25},
    "never_buy_back_below": 0.05
  },
  "guard": {
    "put_roll_delta": 0.35, "call_roll_delta": 0.40,
    "min_extrinsic": 0.15, "pin_dte": 5, "pin_pct": 0.02, "earnings_delta": 0.25,
    "roll_min_credit": 0.05, "roll_max_dte": 90, "max_rolls": 4, "chain_max_loss_x_credit": 3.0,
    "on_put_assignment": "sell_shares", "on_call_assignment": "rebuy_shares"
  },
  "margin_floor": {
    "excess_liquidity_min": 100000, "entry_buffer": 25000, "restore_target": 110000,
    "emergency_level": 50000, "cushion_min_new_trade": 0.40, "cushion_breach": 0.30,
    "cushion_restore": 0.35, "cushion_emergency": 0.15, "check_interval_s": 60, "auto": true
  },
  "risk": {
    "risk_budget_pct_nlv": 0.10,
    "short_put_notional_max": 0.50, "index_put_notional_max": 0.40,
    "per_name_max": 0.15, "per_sector_max": 0.35, "expiry_max_share": 0.35,
    "options_beta_delta_max": 0.35, "stress_margin_multiplier": 1.5, "single_name_gap": 0.35,
    "single_name_gap_max_loss": 0.02,
    "options_daily_loss_halt": 0.015, "options_dd_reduce": 0.05, "options_dd_halt": 0.08,
    "max_orders_per_day": 5
  },
  "regime": {"vix_low": 13, "vix_mid": 20, "vix_high": 30, "fade_from_high": 0.15,
             "mult": {"low": 0.5, "normal": 0.8, "elevated": 1.0, "rising": 0.0, "fading": 0.6}},
  "tail_hedge": {"underlying": "SPY", "otm": [0.10, 0.15], "dte": [60, 120],
                 "budget_pct_premium": [0.05, 0.10], "roll_dte": 30, "take_profit_x": 3.0},
  "fees": {"per_contract": 0.65, "tier": "ibkr_pro_tiered", "margin_rate_source": "ib_benchmark"}
}
```

Secrets come from `.env` only.

---

## 11. Staged Rollout

A human promotes each stage; the bot never promotes itself. Rolling back is always allowed.

| Stage | Mode | What runs | Exit criteria |
|---|---|---|---|
| **0. Backtest** | offline | **Synthetic-chain backtest** (§11.1) 2008–present, covering 2008, Feb 2018, 2020, 2022 and Aug 2024, for index puts and SPY/QQQ covered calls; single-stock legs only approximated | **Walk-forward**: parameters tuned on one period and tested on a later one. Out-of-sample: overlay Sortino > 1.0; options-book max drawdown < 8% NLV; **0 EL floor breaches**; 0 unhandled assignments; overlay + stocks beats buy-and-hold on a risk-adjusted basis |
| **1. Paper** | `paper` | Everything, on a fully invested paper account. **Real IB chains are recorded from day 1** | **8 weeks** (doubled, because there's no historical chain data); 0 reconcile mismatches; every halt and guard path triggered on purpose |
| **2. Shadow** | `shadow` | Live account, read-only API; intents logged, nothing sent | 2 weeks; ≥ 90% of intents judged "would have done this" |
| **3. Approve** | `approve` | Live orders, each approved by you (10-min expiry). Margin restoration and guard rolls **within 1 DTE** are automatic | 4 weeks; slippage within target |
| **4. Auto-manage** | `auto`, open = approval | Closes, rolls and the hedge are automatic | 4 weeks clean |
| **5. Auto-open** | `auto` | Everything automatic, starting at **25% of the risk budget**, +25% per clean month | 8 weeks clean at 100% |

### 11.1 Backtest without paid historical data

The owner has no historical options data, and IB doesn't serve expired option chains. Stage 0 therefore uses free data:

| Input | Source | Used for |
|---|---|---|
| Daily OHLC for SPY, QQQ, watchlist stocks | IB `reqHistoricalData` (or yfinance) | Underlying paths, RV, trend indicators |
| **VIX** (SPX 30-day implied vol) and **VXN** (Nasdaq-100) | CBOE (free daily history) | At-the-money IV for SPX/XSP/SPY and QQQ |
| VIX9D / VIX3M | CBOE | Term structure, used to scale IV by DTE |
| Skew | A fixed, conservative put-skew curve (OTM puts priced at higher IV than ATM, steeper when VIX is low), calibrated against the first weeks of recorded IB chains | OTM strike IV |

Options are priced with Black-Scholes at those IVs. Bid-ask is modeled as a
pessimistic spread (e.g. 5% of mid, minimum $0.05), plus fees.

**Limits:** this is accurate enough for **index puts and SPY/QQQ covered
calls**, the core of the strategy, but only rough for single stocks (no
per-stock IV history), so single-stock puts start at a reduced budget until
recorded data validates them. From Stage 1 on, the tracker records real IB
chains every scan. After ~3 months that history replaces the synthetic skew
curve, and the backtest is re-run against it.

---

## 12. Testing

- **Unit:** every risk limit and boundary; stress and budget math against hand-checked fixtures; scanner EV and score; guard triggers; roll builder; pricing ladder; fees and interest.
- **Property:** the gate never approves an opening that breaches any limit; restoration never closes the hedge or stock.
- **Integration (FakeAdapter):** partial fills, rejects, disconnects mid-order, single-stock assignment, half-filled roll attempts, gateway restart, stale quotes, VIX spike sequences.
- **Paper smoke test:** a scripted daily run.
- Existing `scripts/` tests keep passing.

---

## 13. Implementation Plan

See [AUTO_TRADING_PLAN.md](AUTO_TRADING_PLAN.md) (one PR per task).

---

## 14. Open Questions

1. ~~Historical options data~~ **Resolved:** none available. Synthetic backtest (§11.1) + recording real chains from Stage 1.
2. ~~Margin type~~ **Resolved:** Reg-T (verified automatically at startup).
3. ~~Wheel vs. stop~~ Resolved: no assignment.
4. **Legacy positions:** opt the existing ~500 positions into management, or only bot-opened ones?
5. **Hosting:** cloud VM (recommended) or home machine?
6. **Notifications:** which channel?
7. **Approval UX:** web only, or approve from a phone notification?
8. ~~R1 definition~~ Resolved: EL ≥ $100k.
9. ~~R5 scope~~ Resolved: restoration may close any option.
10. **Premium cash:** leave it, or later auto-invest it into a configured ETF?
11. ~~Tax residency~~ **Resolved:** taxed in Israel. The US 60/40 rule doesn't apply. Index options stay the primary put engine for the no-assignment reason. The journal keeps per-trade realized P&L and fees in USD for Israeli reporting (see the `tax_report` metric).
12. **Covered-call coverage:** 50% confirmed for SPY/QQQ/QQQM. For single stocks: per symbol (to be set).
13. ~~QQQM~~ **Resolved:** keep QQQM, and sell covered calls on it with the QQQM rules in §5.3.

---

## 15. Deployment: How the Bot Runs

An always-on host running `docker compose` with three services: `ib-gateway`
(IBC), `trading-engine`, `web-ui`, all with `restart: unless-stopped`.

| Item | Choice |
|---|---|
| Host | Recommended: a small cloud VM in US-East (~$10–20/mo). Alternative: a home mini-PC. |
| Secrets | `.env` / Docker secrets on the host only. |
| Access | Web UI only over Tailscale/WireGuard; never exposed publicly. |
| Login | IBC handles daily restarts; weekly 2FA via IB Key, with a high-priority alert when it's needed. |
| Health | A watchdog alerts if the engine is down in market hours. Also set an IB-side margin alert as an independent backup. |
| Updates | Deploy outside market hours only; the engine reconciles on start. |
| Backups | Nightly copy of the journal and config. |

## 16. Role of an LLM

No LLM is in the trading path. Every decision is a deterministic rule, so it's
predictable, auditable, backtestable, and works during an outage. Optional
later uses are advisory only (an LLM can never create, approve, or send an
order): plain-language daily reports, a news check that can only **veto** an
entry, a config assistant that proposes changes for a human to apply, and post-mortems.

## 17. Risk Disclosure

Selling options can lose far more than the premium collected. Gaps (earnings,
overnight news) skip past stops and guards, because they act only when the
market is open and liquid. A fully invested account selling puts takes a
double hit in a crash. The risk budget, stress tests, tail hedge, and staged
rollout reduce these risks; they don't remove them. Backtest results are not
a guarantee. Software bugs, bad data, and broker outages are additional risks.
The account owner remains responsible for every trade the system places.
