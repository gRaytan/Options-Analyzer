# Auto-Trading Specification
## Options Analyzer — Automated Options Trading (Phase 3)

| | |
|---|---|
| **Status** | Draft v0.3 (user requirements R1–R9 incorporated) |
| **Date** | 2026-11-21 |
| **Broker** | Interactive Brokers (TWS / IB Gateway API) |
| **Scope** | Manage existing positions **and** open new ones |
| **Strategies** | Covered calls and margin-backed short puts (core), credit spreads / iron condors, naked calls. **No assignments: roll instead** (R7) |
| **Autonomy** | Staged rollout: paper → shadow → approve-each → auto-manage → auto-open |

---

## 1. Summary

Options Analyzer today reads IB CSV exports, computes Greeks, margin, and risk
flags, and shows recommendations (`src/analysis/risk.py`,
`src/analysis/margin.py`, `data/config/settings.json`). It never sends an
order.

This spec adds a trading engine that:

1. Connects to IB live (positions, quotes, account values) instead of CSV.
2. Turns the existing risk flags into **exit and roll** decisions for open positions.
3. Scans a watchlist for **new entries** across four premium-selling strategies.
4. Passes every proposed order through a **risk gate** with hard limits. Strategy code cannot bypass the gate.
5. Places, works, and reconciles orders, with a full audit trail.
6. Gains autonomy in stages. Each stage has measurable exit criteria.

The guiding principle: **a component that reduces risk (closing) gets autonomy
before a component that adds risk (opening). Undefined-risk trades (naked
calls) come last.**

### 1.1 User requirements (v0.2)

These come directly from the account owner. Where they conflict with defaults elsewhere in this document, **they win**.

| # | Requirement | Where it is implemented |
|---|---|---|
| **R1** | Keep a margin cushion of **at least $100,000 at all times** to minimize risk. | §6.1 (pre-trade floor), §6.5 (continuous monitor and automatic de-risking) |
| **R2** | **Minimize transactions** to cut fees. | §5.8 (fee-aware rules), §7.5 (fee model); exits and rolls in §5 are fee-gated |
| **R3** | Always look for **covered calls with high premium and low delta**, and for **puts to sell**. | §5.9 (Opportunity Scanner and ranking) |
| **R4** | **Track** options to find the right moment to sell. | §5.10 (Opportunity Tracker for entries; Decay Tracker for open positions) |
| **R5** | When margin requirements are not met, or the bot judges a position should be closed, **close it automatically**. | §6.5 (margin restoration), §5.7 (close decisions), §11 (closing is automatic from Stage 4; margin-restoration closes are automatic from Stage 3) |
| **R6** | **Connect to the Interactive Brokers account through its API.** | §4.3 (connection spec), M1 in §13 |
| **R7** | **No assignments.** Roll when needed instead of being assigned. | §5.11 (assignment avoidance), §5.2–5.3 |
| **R8** | **No cash in the account; everything is invested.** Puts are backed by margin (the stock portfolio), not by cash. | §5.2 (margin-backed short puts), §6.1 (put notional cap), §6.2 (stress test includes the stock holdings) |
| **R9** | **Manage the risk and always stay far from the margin limit.** | §6.1 (Cushion ≥ 40% for new trades), §6.2 (−20% stress), §6.5 (Cushion floor in addition to the $100k floor) |

**Definition used for R1:** "margin" = IB **Excess Liquidity** (`ExcessLiquidity`
account tag = equity with loan value − maintenance margin). That's the cushion
IB uses before it starts liquidating positions. The floor is **$100,000**; see
open question 8 (confirmed by the owner).

**Definition used for R9 ("far from margin"):** besides the $100k floor,
the bot also watches IB **Cushion** (Excess Liquidity ÷ Net Liquidation).
New trades require Cushion ≥ 40% after the trade, and restoration starts below
30%. Whichever of the two floors ($100k or 30% Cushion) is stricter for the
current account size wins. Because the account is fully invested (R8), a
market drop hits twice: the stocks lose value **and** the short puts lose money,
and both shrink Excess Liquidity at the same time. The R9 limits are sized for that.

---

## 2. Goals and Non-Goals

### Goals
- G1: Close or roll positions automatically when they hit the configured triggers (profit target, delta, DTE, assignment risk). No short option is ever left to be assigned (R7).
- G2: Open new positions that match the strategy rules, without ever breaching the portfolio risk limits.
- G3: Make every action explainable: each order links to the signal, inputs, and risk checks that produced it.
- G4: Fail safe. On any unknown state (disconnect, stale data, reconcile mismatch) stop opening and keep only protective actions.
- G5: Reuse the existing analysis code. Do not fork the logic.

### Non-Goals (v1)
- High-frequency or intraday scalping. Decisions run on a schedule of minutes, not milliseconds.
- Directional long-option strategies, futures options, and non-US underlyings.
- Multi-user or SaaS operation. The bot trades one account.
- Machine-learning signal generation. Rules are explicit and configurable.
- Tax optimization (wash-sale avoidance). The bot logs the data; it does not act on it.

---

## 3. Prerequisites

| Requirement | Why |
|---|---|
| IB margin account, options **Level 4** (naked) permissions | Required: with no cash (R8), every short put is a naked put backed by margin. |
| Portfolio Margin (recommended) | Naked option margin under Reg-T is far higher. `MarginCalculator` already supports both. |
| OPRA + US equity real-time market data subscriptions | yfinance (`src/data/price_fetcher.py`) is delayed and rate-limited, so it is not safe for order pricing. |
| IB Gateway running headless with auto-restart (e.g. IBC) | IB forces a daily restart; the bot must survive it. |
| Separate IB **paper** account | Required for Stage 1. Paper uses port 7497/4002; live uses 7496/4001. |
| Account equity > $25k | Avoids pattern day trader restrictions on same-day open/close. |

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
          │ (quotes, IV,  │   │ (existing      │  │ (place, walk,    │
          │  chains,      │   │  Portfolio     │  │  cancel, fills,  │
          │  calendar)    │   │  model, live)  │  │  reconcile)      │
          └───────┬───────┘   └────────┬───────┘  └───▲──────────────┘
                  │                    │              │ approved orders
          ┌───────▼────────────────────▼───────┐      │
          │          Strategy Engine            │      │
          │  ┌──────────────┐ ┌──────────────┐  │      │
          │  │ Position     │ │ Entry        │  │      │
          │  │ Manager      │ │ Scanner      │  │      │
          │  │ (exit/roll)  │ │ (new trades) │  │      │
          │  └──────┬───────┘ └──────┬───────┘  │      │
          └─────────┼────────────────┼──────────┘      │
                    │  TradeIntents  │                 │
          ┌─────────▼────────────────▼──────────┐      │
          │             Risk Gate               │──────┤
          │  hard limits · stress test ·        │      │
          │  whatIf margin · kill switch        │      │
          └─────────────────┬───────────────────┘      │
                            │ (Stage 3: human approval)│
                            └──────────────────────────┘
                                      │
          ┌───────────────────────────▼─────────────────┐
          │ Journal (SQLite): intents, checks, orders,  │
          │ fills, P&L, events  →  Flask UI + alerts    │
          └─────────────────────────────────────────────┘
```

### 4.1 Module layout (new code under `src/trading/`)

| Module | Responsibility | Reuses |
|---|---|---|
| `broker/ib_adapter.py` | The only module that imports the IB library. Async connection, reconnect with backoff, contract qualification, chain fetch, quotes, `whatIfOrder`, order placement, fill and position events. | — |
| `broker/fake_adapter.py` | In-memory broker for tests and backtests. Same interface. | — |
| `market_state.py` | Quote cache with timestamps, staleness checks, IV rank, earnings/ex-div calendar, market hours. | `analysis/greeks.py` for fallback Greeks |
| `portfolio_state.py` | Builds the existing `Portfolio`/`OptionPosition` objects from live IB data instead of CSV. | `models/portfolio.py`, `models/position.py` |
| `strategies/base.py` | `Strategy` interface: `scan()` → entry intents, `manage(position)` → exit/roll intents. | — |
| `strategies/csp.py`, `covered_call.py`, `credit_spread.py`, `iron_condor.py`, `naked.py` | Strategy rules (§5). | `analysis/risk.py` flags |
| `risk_gate.py` | Approves, resizes, or rejects every intent (§6). | `analysis/margin.py` |
| `order_manager.py` | Pricing, order working, cancel/replace, timeouts, idempotency, reconciliation (§7). | — |
| `scheduler.py` | Runs the decision loop on market-hours timers (§8). | — |
| `journal.py` | SQLite persistence of all events (§9). | `data/storage.py` patterns |
| `kill_switch.py` | Global halt state, file and UI triggers (§6.4). | — |

The Flask app (`src/web/app.py`) gets new pages: **Bot Status**, **Pending
Approvals**, **Order Log**, and **Strategy Config**. The trading engine runs as a
**separate process** (`python -m src.trading.run`) so a web crash cannot stop
the engine and an engine crash cannot stop the web UI. The two communicate
only through the journal database and the kill-switch file.

### 4.2 Library choice
Use **`ib_async`** (the maintained successor of `ib_insync`). Keep all IB types
inside the adapter. The rest of the system works with internal dataclasses, so
the broker can be swapped or faked.

### 4.3 IB API connection (R6)

| Item | Spec |
|---|---|
| Endpoint | IB Gateway (preferred; lighter than TWS) on `127.0.0.1`. Ports: live 4001, paper 4002 (TWS: 7496 / 7497). |
| Process supervision | IBC to log in, handle the daily restart, and restart the Gateway after a crash. 2FA via IB Key on the owner's phone once a week (IB's re-auth cadence). |
| Client IDs | `1` = trading engine (the only one that places orders), `2` = web UI (read-only), `3+` = scripts. |
| Read-only first | In API settings, "Read-Only API" stays **on** through Stages 0–2; it is turned off only when entering Stage 3. This is a broker-side guarantee that early stages can't place orders. |
| Data pulled | `reqPositions`, `reqAccountSummary` (tags `NetLiquidation`, `ExcessLiquidity`, `MaintMarginReq`, `InitMarginReq`, `AvailableFunds`, `BuyingPower`, `Cushion`), `reqPnL`, `reqSecDefOptParams` (chains), `reqMktData` with generic ticks 100/101/104/106 (option volume, OI, historical and implied vol), `reqExecutions`, `reqHistoricalData` for IV rank. |
| Pre-trade margin | `whatIfOrder` for every opening order and every roll, giving the post-trade `ExcessLiquidity` used by the R1 check. |
| Market data type | Live (type 1). If only delayed data is available, the bot runs read-only and refuses to place orders. |
| Reconnect | Exponential backoff 1s → 60s. On reconnect: reload positions, open orders and executions, then reconcile (§7.4) before acting. |
| Pacing | Respect IB's limit of 50 messages/s and its market data line limit (100 by default). The scanner (§5.9) streams quotes in batches and uses snapshots for wide scans. |
| Replacing CSV | Live positions feed the existing `Portfolio` model. CSV upload stays available for history (trades, dividends) and as a fallback. |

---

## 5. Strategy Rules

All numbers below are **defaults** in `data/config/trading.json` (§10). Each
strategy can be enabled per stage and per symbol.

### 5.1 Universe and common entry filters
Applied before any strategy-specific rule:

| Filter | Default |
|---|---|
| Underlying in watchlist | user-maintained list |
| Underlying price | $20 – $1,000 |
| Underlying avg daily volume | ≥ 1M shares |
| Option open interest at strike | ≥ 500 |
| Option bid-ask spread | ≤ 10% of mid **and** ≤ $0.10 for mid < $1.00 |
| IV rank (52-week) | ≥ 30 |
| Earnings blackout | No entry if earnings fall before expiration (configurable to allow) |
| Ex-dividend | No short calls when ex-div falls before expiration and extrinsic < dividend |
| Time of day | 09:45 – 15:45 ET (avoid the open and close auctions) |
| Market regime | No new entries if VIX > 35 or SPY is down > 3% intraday (configurable) |

### 5.2 Short Put (margin-backed, R8)
- **Entry:** Sell puts at 30–45 DTE, **delta 0.10–0.20**, on watchlist names (§10). The best candidates come from the scanner ranking (§5.9).
- **Collateral:** None in cash. The put is margined against the stock portfolio. Its assignment value (strike × 100 × qty) counts against the **short-put notional cap** in §6.1, and its margin is checked with `whatIfOrder`.
- **Exit:** By default **hold to expiration** (R2), provided it stays OTM. Close early only under the fee-aware rules in §5.8, the assignment-avoidance rules in §5.11, or margin restoration (§6.5).
- **Defend:** Roll per §5.11 (down and out, for a net credit). **Never take assignment** (R7). If no acceptable roll exists, close.

### 5.3 Covered Call
- **Entry:** For each 100 uncovered shares held, sell 1 call at 21–45 DTE, **delta ≤ 0.15 (target 0.08–0.15)**, low enough that a roll is rarely needed, since shares must not be called away (R7), strike ≥ cost basis (configurable), choosing the strike/expiry with the best premium score (§5.9). The R3 goal is **high premium at low delta**, so the bot waits (§5.10) for elevated IV rather than selling a low-delta call for little premium.
- **Coverage:** Never sell more calls than `floor(shares / 100)`. This uses the same coverage logic the margin code uses (commit `2eb3ff0`).
- **Exit:** Hold to expiration by default (R2). Close early only under §5.8 or the defend rule.
- **Defend:** Roll **up and out** per §5.11 before the call can be assigned, including before ex-dividend dates. **Shares are never allowed to be called away** (R7). If no acceptable roll exists, buy back the call.

### 5.4 Credit Spreads (bull put / bear call)
- **Entry:** Short leg delta 0.20–0.30, 30–45 DTE, width $5 (or per-symbol), credit ≥ **1/3 of width**.
- **Order:** Always one **combo (BAG) order** for both legs. Never leg in.
- **Exit:** Hold to expiration unless §5.8 favors an early close. Spreads close before expiration when pin risk applies (below).
- **Stop:** Close if the spread's value reaches 2× the credit received.
- **Expiration:** Close any spread with the short leg within 1% of the money by 15:00 ET on expiration day (pin risk).

### 5.5 Iron Condor
- **Entry:** Short put and short call at delta ~0.16, 30–45 DTE, equal widths, total credit ≥ 1/3 of width.
- **Order:** One 4-leg combo order.
- **Exit:** Hold to expiration unless §5.8 favors an early close.
- **Defend:** No adjustments (R2). Close the whole position at the 2× credit stop.

### 5.6 Naked Short Options
Undefined risk. Short puts are now a core strategy (§5.2, because of R8). **Naked calls** (calls not covered by shares) remain gated hardest and are enabled last (Stage 5b).
- **Naked calls:** Delta ≤ 0.15, index ETFs or mega-caps only (allowlist), **never** through earnings, **never** on hard-to-borrow or high short-interest names (squeeze risk).
- **Mandatory stop:** Close if the option price reaches 3× the credit, or if the underlying crosses the strike.
- **Mandatory stress check:** each new naked position must pass the stress test in §6.2 on its own and as part of the portfolio.
- **Size cap:** Naked notional (strike × 100 × qty) ≤ 15% of NAV per underlying, ≤ 100% of NAV in total.

### 5.7 Position management loop (all strategies)
On every cycle, each open position runs through `RiskAnalyzer.analyze_position`
(existing code) and then the owning strategy's `manage()`:

| Existing flag | Default action |
|---|---|
| `PROFIT_TARGET_REACHED` | Close **only if** the fee-aware test in §5.8 says closing beats holding; otherwise hold to expiration |
| `EXPIRING_SOON` (≤ 7 DTE) | Close or roll, by strategy rule |
| `HIGH_DELTA_SHORT_CALL` | Roll or close |
| `ITM_OPTION` | Roll or close per §5.11. Never accept assignment |
| `LARGE_LOSS` | Evaluate the stop-loss rule |

Required changes to `risk.py`:
- Add a high-delta check for **short puts** (today the check is calls-only).
- Add `EX_DIVIDEND_ASSIGNMENT_RISK` and `PIN_RISK` flags.
- Make `LARGE_LOSS` relative to the credit received (e.g. 2×) rather than a fixed $500.

Positions the bot did not open (legacy or manual) are **managed only if the
user opts them in**. By default they are monitored and alerted, not traded.
**Exception (R1/R5):** margin restoration (§6.5) may close any position in the
account, because the $100k floor applies to the whole account.

### 5.8 Fee-aware trading (R2)

The goal is fewer, larger, higher-quality trades. Each order costs about
**$0.65/contract plus exchange and regulatory fees** under IBKR Pro tiered
pricing (verify against the account's own commission schedule), plus the
bid-ask spread paid on entry and exit. Expiration and assignment are
commission-free.

| Rule | Default |
|---|---|
| **Hold to expiration** is the default exit. Letting an OTM option expire costs $0. | on |
| Close early for profit only if: remaining premium ≥ **$0.10/contract** **and** ≥ 80% of max profit is captured **and** ≥ 10 DTE remain (so the freed margin can be re-used for a new sale) | `early_close` |
| **Never buy back** a short option with mid ≤ $0.05 unless it's needed for margin restoration or assignment avoidance (§5.11) | on |
| Minimum **net credit per contract** after round-trip fees for new trades | $50 (≥ $0.50 premium) |
| Minimum annualized return on margin, after fees, for new trades | 12% |
| Prefer **monthly** (30–45 DTE) over weekly expirations: about 4× fewer trades for similar premium | on |
| Rolls only as **one combo order** (one commission event, one spread crossing) and only for a **net credit** that covers the extra fees | on |
| No adjustments on iron condors (closing beats adjusting after fees) | `max_adjustments: 0` |
| Size up instead of spreading out: fewer underlyings, more contracts, within the per-underlying cap | on |
| Max new opening orders per day | 5 |
| Margin restoration (§6.5) closes the **fewest positions** that restore the floor | on |

Every intent carries an **estimated fee** and an **expected net P&L**. The
journal records the actual commissions from IB execution reports, so the
dashboard can show fees as a percentage of premium collected (target < 3%).

### 5.9 Opportunity Scanner (R3)

Runs continuously during market hours (§8). It has two queues that always run:

1. **Covered calls:** every holding with ≥ 100 uncovered shares.
2. **Put sales:** every watchlist name enabled for `short_put`.

For each candidate (underlying × expiry 21–45 DTE × strike within the delta
band), compute:

```
premium_yield   = mid / capital_at_risk × 365 / DTE
                  (capital_at_risk = whatIf margin for short puts;
                   share value for covered calls)
prob_otm        ≈ 1 − |delta|
edge            = IV − realized_vol_20d            (selling rich volatility)
score           = premium_yield × prob_otm
                  × (1 + 0.5 × clamp(IV_rank/100, 0, 1))
                  × liquidity_factor                 (spread and OI penalty)
                  − fee_drag                         (round-trip fees / capital)
```

- **Hard filters:** delta ≤ 0.15 for covered calls, ≤ 0.20 for short puts; net credit ≥ $50/contract; the common filters in §5.1.
- **Output:** a ranked list per underlying (best strike/expiry) shown on a new **Opportunities** page, and fed to the Risk Gate as intents when the tracker (§5.10) says the timing is right.
- **R3 metric:** "high premium" means `premium_yield ≥ 15%` annualized for covered calls and `≥ 20%` for puts (configurable).

### 5.10 Tracking: when to sell (R4)

**Opportunity Tracker (entries).** Every scanner result is stored with a
timestamp, so the bot sees how premium for the same delta changes over the
day and over weeks. It fires a sell when the **timing score** is high, rather
than at the first acceptable quote:

| Signal | Covered calls | Puts |
|---|---|---|
| IV rank ≥ 50, or IV percentile in the top third of its 30-day range | ✓ | ✓ |
| Underlying move | after an up move (≥ +1 ATR over 1–3 days, or near a 20-day high): call premium is richer | after a down move (≤ −1 ATR, or near support): put premium is richer |
| Score vs. its own 20-day history | top quartile | top quartile |
| IV spike before earnings | sell only if the expiry is **after** the IV crush or the earnings rule allows it | same |
| Time decay | sell at 30–45 DTE, where theta per day of margin is efficient | same |

If no signal fires for a holding, the bot keeps waiting. When 21 DTE has
passed with no sale in the current monthly cycle, it accepts the best
available score that still meets the hard filters. That way shares aren't
left uncovered for long.

**Trend & regime detection.** Computed per underlying every scan cycle from
IB historical bars and the IV history, all as deterministic indicators:

| Indicator | Used for |
|---|---|
| Price vs. 20/50/200-day moving averages; 50/200 slope | Trend direction. In an **uptrend**, favor puts and raise covered-call strikes (lower delta) so shares aren't called away cheaply. In a **downtrend**, sell puts further OTM or skip them, and favor covered calls. |
| RSI(14), distance from the 20-day high/low in ATRs | Short-term overbought/oversold timing: sell calls into strength, puts into weakness. |
| IV rank / percentile, IV vs. 20-day realized vol | Whether premium is rich (sell) or cheap (wait). |
| IV term structure (front vs. 2nd month) | Inversion signals an event or stress: be cautious, or sell the richer expiry. |
| Put/call skew | Puts unusually rich vs. calls → favor put sales. |
| Market regime (SPY trend, VIX level and its change) | Global throttle on new entries (§5.1). |

The tracker stores these with every scan result. That gives a history that
the backtest (Stage 0) can use to tune the thresholds, rather than guessing them.

**Decay Tracker (open positions).** For each open short option, track
captured premium over time against the theoretical theta curve, plus delta
and extrinsic value. This feeds §5.8: it detects when the remaining premium
is too small to be worth holding the margin, and when a roll is worth it. It
also shows each position's "sell-by" signals on the positions page.

### 5.11 Assignment avoidance: roll, never get assigned (R7)

American-style equity options can be assigned at any time, usually when an
ITM option has little extrinsic value left, or (for calls) the day before an
ex-dividend date. The **Assignment Guard** checks every short option every
management cycle (5 min).

**Roll triggers (any one fires):**

| Trigger | Default |
|---|---|
| ITM **and** extrinsic value < $0.15 (or < 0.5% of strike) | early-assignment danger zone |
| Delta ≥ 0.50 (put) / ≥ 0.50 (call) | it's becoming an ITM position |
| ≤ 5 DTE **and** underlying within 2% of the strike | pin risk / after-hours move risk |
| Short call ITM, ex-dividend date before expiry, extrinsic < dividend + $0.05 | roll **by 15:30 ET on the day before** the ex-date |
| Expiration day, any short option ITM or within 1% of the strike | **must** be rolled or closed by 15:00 ET; nothing ITM is left to expire |

**How to roll:**
1. Build candidates with **one combo order** (buy back the current option + sell the new one): next monthly expirations (30–60 DTE, up to a max of 90), with a strike that is the same or better (puts lower, calls higher).
2. Choose the candidate with the best strike improvement that still gives a **net credit ≥ $0.05/contract** (covers fees, R2).
3. **Never increase the contract count** in a roll (no doubling down).
4. The roll must pass the Risk Gate like a new trade (§6.1–6.2). Rolling may not push the account closer to the margin limit (R9).
5. A position may be rolled at most **4 times**. After that, or if no candidate meets rules 1–4, **close it** and take the loss. Losses are realized deliberately, never through an assignment.

**Roll chains:** the journal links every roll to the original trade, so P&L,
total credit, and days held are reported per chain, not per leg. A chain whose
cumulative loss passes **3× the original credit** is closed regardless of roll options.

**If an assignment still happens** (early assignment can't be fully prevented):
- **Put assigned** → shares appear on margin. Alert immediately; at the next open, **sell the shares** (default `on_put_assignment: "sell_shares"`) to restore the account to its prior state.
- **Call assigned** → shares are called away. Alert; at the next open, **buy the shares back** (default `on_call_assignment: "rebuy_shares"`) to stay fully invested (R8).
- Both are logged as an assignment event on the chain, and the Assignment Guard's thresholds are reviewed.

---

## 6. Risk Gate

Every `TradeIntent` (open, close, roll) passes through the risk gate. The gate
is a pure function of (intent, portfolio state, market state, config), so it
can be unit-tested exhaustively.

### 6.1 Hard limits (checked on every opening intent)

| Limit | Default |
|---|---|
| **Excess Liquidity after the trade ≥ $100,000 + $25,000 entry buffer (R1)** (IB `whatIfOrder` result). The buffer keeps a new trade from immediately triggering §6.5. | $125,000 |
| **Cushion (EL ÷ NLV) after the trade (R9)** | ≥ 40% |
| **Excess Liquidity under the §6.2 −20% market / IV +50% scenario (R9)** | ≥ $100,000 |
| **Total short-put notional** (strike × 100 × qty, all short puts) (R8) | ≤ 50% of NLV |
| Max margin utilization after the trade (IB `whatIfOrder` result) | 50% of NAV |
| Max allocation per underlying (notional at risk) | 10% of NAV |
| Max allocation per sector | 30% of NAV |
| Max portfolio beta-weighted delta (SPY-equivalent shares) | ±0.5 × NAV / SPY price |
| Max short vega | configurable $ per 1 IV point |
| Max new positions per day | 5 (R2) |
| Max contracts per order | 10 |
| Max loss per defined-risk trade | 2% of NAV |
| Account must have the required option permission level | — |

Closing and risk-reducing intents **skip** allocation limits, so the bot can
always get out. They are still checked for sanity (price, quantity, contract).

### 6.2 Stress test
Before any opening trade, revalue the portfolio with the new position under these scenarios:
- Underlying moves −25%, −20%, −15%, −8%, +8%, +15%, +25%, applied to **the stock holdings and the options together** (R8)
- IV +50% (relative) combined with the −15% and −20% moves
- Margin requirement recomputed under each scenario (stock margin rises as positions fall)

Reject the trade if any scenario's portfolio loss exceeds **25% of NAV** or the
scenario margin exceeds NAV. Use `py-vollib` (already a dependency) for
repricing.

### 6.3 Sanity checks (every order)
- The quote is fresher than 5 seconds.
- The limit price is within the current bid/ask.
- The quantity is not larger than the position being closed.
- The contract matches the intended expiration, strike, right, and multiplier (100).
- The order side cannot flip a position (e.g. closing −5 with a +10 buy).
- No duplicate: no working order already exists for the same position.

### 6.4 Kill switch and circuit breakers

| Trigger | Action |
|---|---|
| Manual: UI button, `touch data/trading/HALT`, or CLI | Cancel all **opening** orders; stop new entries; keep managing (closing) unless `HALT_ALL` |
| Daily realized + unrealized loss > 3% of NAV | Halt opening for the rest of the day |
| Drawdown from equity high-water mark > 10% | Halt opening until a human resets it |
| Broker disconnect > 60s | Stop all new orders; on reconnect, reconcile before resuming |
| Reconciliation mismatch (§7.4) | Halt all trading; alert |
| Unhandled exception in the strategy engine | Halt opening; alert |
| Margin call / excess liquidity < 10% NAV | Halt opening; Position Manager prioritizes closes that free the most margin |

Halt states persist in the journal. A restart **does not** clear a halt;
only a human can.

### 6.5 Margin floor monitor and automatic restoration (R1, R5)

Runs **every 60 seconds** during market hours (its own loop, independent of
the 5-minute management cycle), using streamed `ExcessLiquidity`.

| Excess Liquidity | State | Action |
|---|---|---|
| ≥ $125,000 **and** Cushion ≥ 40% | Normal | — |
| $100,000 – $125,000, **or** Cushion 30–40% | **Warning** | No new opening trades (rolls that improve margin still allowed). Alert. |
| < $100,000, **or** Cushion < 30% | **Breach** | Stop all opening. Within 2 minutes, start **automatic closing** until EL ≥ $110,000 and Cushion ≥ 35% (hysteresis, so one small move doesn't cause repeated closes). |
| < $50,000 or IB `Cushion` < 5% | **Emergency** | Same as breach but price aggressively (up to the natural price) and alert by every channel. Getting out matters more than slippage. |

**Which positions to close (fewest transactions, R2):**
1. Compute, for each short position, the margin freed by closing it (`whatIfOrder` on the closing order) and the cost to close (debit plus fees plus slippage).
2. First close positions that are also flagged for closing (ITM, high delta, loss stop), since they're high risk anyway.
3. Then rank the rest by **margin freed ÷ cost to close**, which picks the cheapest margin to release. Take the smallest set whose combined margin release reaches the target.
4. Prefer closing whole positions over partial ones (one order instead of several).
5. Don't close covered calls (they free no margin) unless required to sell the underlying shares.
6. Re-check EL after each fill. Stop as soon as the target is reached.

**Out-of-hours breach:** if EL drops below the floor while the market is
closed (e.g. a pre-market gap), the bot alerts immediately and runs
restoration at 09:31 ET, after the open, without waiting for the 09:45 window.

**Autonomy:** margin-restoration closes are risk-reducing, so they run
automatically from **Stage 3** onward. They skip per-order approval, but every
close is notified, and the user can disable this with `margin_restore.auto: false`.

---

## 7. Order Management

### 7.1 Pricing and working orders
- **Limit orders only.** No market orders on options, ever.
- Start at the **mid** price. If unfilled after 60s, step 10% of the spread toward the natural price. Repeat up to 4 steps, and never cross a configured worst price (e.g. never pay more than mid + 40% of the spread).
- If still unfilled, cancel and retry next cycle. After 3 failed cycles, alert and skip until the next day.
- Multi-leg trades use IB combo orders with a net limit price.
- Rolls go out as **one combo order** (close the old leg and open the new leg together), so a half-done roll cannot leave the account naked.

### 7.2 Idempotency
- Each intent gets a deterministic ID (strategy + position + action + date). It goes in the IB `orderRef` field.
- On startup, the bot fetches all open orders and today's executions and matches them to intents by `orderRef` before doing anything else.

### 7.3 Fills, partial fills, assignment
- Partial fills update the position immediately; the remainder keeps working within the same price limits.
- Assignment and exercise events (detected from position changes outside a bot order) are logged, attributed to their chain, and handled per §5.11 (sell assigned shares / rebuy called-away shares).

### 7.4 Reconciliation
Every cycle, compare the bot's expected positions (from the journal) to IB
positions. Any unexplained difference halts trading (§6.4) until a human
acknowledges it. Positions opened manually in TWS appear as "unmanaged."


### 7.5 Fee model (R2)
`fees.py` estimates commission plus exchange, ORF, OCC and FINRA TAF fees per
order from the account's tier, and gets calibrated against actual
`commissionReport` values. Every strategy decision uses **net-of-fee**
numbers, and the dashboard shows monthly fees vs. premium collected.

---

## 8. Scheduling

| Time (ET) | Job |
|---|---|
| 09:00 | Pre-market: load calendar (earnings, ex-div, holidays), refresh watchlist data, verify connection and permissions |
| 09:31 – 16:00, every 60 s | Margin floor monitor (§6.5) |
| 09:45 – 15:45, every 5 min | Management cycle: all open positions |
| 09:45 – 15:45, every 15 min | Opportunity scanner + tracker (§5.9–5.10); orders only when the timing score fires |
| 15:00 on expiration days | Pin-risk sweep: close near-the-money short legs expiring today |
| 16:15 | End of day: reconcile, snapshot P&L/Greeks, daily report |

The scheduler uses the NYSE calendar (holidays, early closes at 13:00 ET).

---

## 9. Persistence and Observability

### 9.1 Journal (SQLite, `data/trading/journal.db`)
Tables: `intents`, `risk_checks` (each check result and its inputs),
`orders`, `order_events`, `fills`, `positions_snapshot`, `account_snapshot`,
`halts`, `config_versions`. Records are append-only; nothing is updated in place.

### 9.2 Every intent records
The strategy, the trigger (e.g. `PROFIT_TARGET_REACHED`), the quotes and Greeks
at decision time, the risk-gate result for each check, and the config version.
This is the "why" behind every trade (goal G3).

### 9.3 Alerts
Push or email (via a pluggable notifier) for: fills, rejects, halts,
assignments, reconcile mismatches, approvals waiting (Stage 3), and the daily report.

### 9.4 Metrics
Win rate, average credit, average days held, P&L by strategy/underlying,
slippage vs. mid, fill rate, max drawdown, and theta/delta/vega over time. These
are shown on the existing Charts page.

---

## 10. Configuration

`data/config/trading.json` sits next to the existing `settings.json`. The
bot validates it on load (e.g. with pydantic) and rejects unknown keys.
Every change is versioned into the journal.

```json
{
  "mode": "paper",                      // paper | shadow | approve | auto
  "account_id": "DU1234567",
  "enabled_strategies": {
    "short_put":     {"manage": true,  "open": true},
    "covered_call":  {"manage": true,  "open": true},
    "credit_spread": {"manage": true,  "open": false},
    "iron_condor":   {"manage": true,  "open": false},
    "naked_call":    {"manage": false, "open": false}
  },
  "watchlist": {
    "AAPL": {"strategies": ["short_put", "covered_call", "credit_spread"]},
    "SPY":  {"strategies": ["short_put", "iron_condor", "naked_call"]}
  },
  "entry":  {"dte_min": 21, "dte_max": 45, "cc_delta_max": 0.15, "put_delta_max": 0.20,
             "ivr_min": 30, "min_net_credit_per_contract": 50,
             "cc_min_premium_yield": 0.15, "put_min_premium_yield": 0.20},
  "exit":   {"hold_to_expiration": true, "early_close_min_captured_pct": 80,
             "early_close_min_dte": 10, "never_buy_back_below": 0.05, "stop_multiple": 2.0},
  "margin_floor": {"excess_liquidity_min": 100000, "entry_buffer": 25000,
                   "restore_target": 110000, "emergency_level": 50000,
                   "cushion_min_new_trade": 0.40, "cushion_breach": 0.30, "cushion_restore": 0.35,
                   "short_put_notional_max": 0.50, "stress_drop": 0.20,
                   "check_interval_s": 60, "auto": true},
  "assignment_guard": {"min_extrinsic": 0.15, "max_delta": 0.50, "pin_dte": 5, "pin_pct": 0.02,
                       "roll_min_credit": 0.05, "roll_max_dte": 90, "max_rolls": 4,
                       "chain_max_loss_x_credit": 3.0,
                       "on_put_assignment": "sell_shares", "on_call_assignment": "rebuy_shares"},
  "fees":   {"per_contract": 0.65, "tier": "ibkr_pro_tiered"},
  "risk": {
    "max_margin_util": 0.50,
    "min_excess_liquidity": 0.30,
    "max_per_underlying": 0.10,
    "max_per_sector": 0.30,
    "max_trades_per_day": 5,
    "max_contracts_per_order": 10,
    "daily_loss_halt": 0.03,
    "drawdown_halt": 0.10,
    "stress_max_loss": 0.25
  },
  "naked": {"call_allowlist": ["SPY", "QQQ", "IWM"], "max_notional_per_underlying": 0.15,
            "max_notional_total": 1.0, "stop_multiple": 3.0}
}
```

Secrets (account IDs for live, notifier tokens) come from `.env` (python-dotenv
is already a dependency), never from committed config.

---

## 11. Staged Rollout

Each stage must meet its exit criteria before the next stage is enabled. A
human promotes the bot to the next stage by editing `mode`; the bot never promotes itself.

| Stage | Mode | What runs | Exit criteria |
|---|---|---|---|
| **0. Backtest / replay** | offline | Strategies + risk gate against historical chains (FakeAdapter) | ≥ 2 years of history incl. 2020 and 2022; risk gate never breached; results reviewed |
| **1. Paper** | `paper` | Full system on the IB paper account (paper account set up fully invested, like the live one), all strategies except naked calls. **Assignment Guard must roll every test case** | 4 weeks; 0 reconcile mismatches; 0 unhandled errors; every halt trigger tested on purpose |
| **2. Shadow live** | `shadow` | Reads the live account, generates intents, **sends nothing**; the user compares them with their own decisions | 2 weeks; ≥ 90% of intents judged "would have done this" |
| **3. Approve each** | `approve` | Live orders, but each intent waits in **Pending Approvals** (UI and notification), expiring after 10 min. **Exception:** margin-floor restoration closes (§6.5) run automatically (R1/R5) | 4 weeks; slippage vs. mid within target; no approved order behaved unexpectedly |
| **4. Auto-manage** | `auto` + open=false | Closes and rolls are automatic; new entries still need approval | 4 weeks clean |
| **5a. Auto-open (core)** | `auto` | Covered calls, short puts, spreads, condors fully automatic within the limits | 8 weeks clean; start with 25% of the risk limits, then raise them step by step |
| **5b. Auto-open (naked calls)** | `auto` | Naked calls, with their own 4-week paper + approval period | Explicit user sign-off |

Rolling back to an earlier stage is always allowed and takes effect on the next cycle.

---

## 12. Testing

- **Unit:** risk gate (each limit, each boundary), strategy selection (strike/delta/DTE picking), pricing ladder, idempotency key generation, coverage counting.
- **Property tests:** the gate never approves an opening trade that would breach a limit, across random portfolios.
- **Integration:** `FakeAdapter` scenarios: partial fills, rejects, disconnect mid-order, assignment, half-filled roll attempts, gateway restart, stale quotes.
- **Paper smoke test:** a scripted daily run against the IB paper account (`scripts/test_paper_trading.py`).
- **Existing tests** in `scripts/` keep passing; `RiskAnalyzer` changes are covered by new cases.

---

## 13. Implementation Plan

| Milestone | Deliverables | Est. |
|---|---|---|
| M1 Broker foundation (R6) | `ib_adapter` (read-only), `fake_adapter`, live `portfolio_state`, account-summary streaming, reconciliation, journal | 2 wks |
| M2 Risk gate + margin floor (R1, R5) | All §6 checks, $100k EL floor monitor and restoration, stress test, halt persistence, tests | 2 wks |
| M3 Management + fees (R2, R5) | Position Manager over existing flags, `risk.py` upgrades, fee model, hold-to-expiry logic, order manager with pricing ladder | 2 wks |
| M3b Assignment Guard (R7) | Roll triggers, roll builder, roll chains, assignment handling (§5.11) | 1 wk |
| M4 Entries (R3, R4) | Opportunity Scanner and Tracker, covered calls + short puts first, then spreads + condors | 2 wks |
| M5 UI + alerts | Bot Status, Pending Approvals, Order Log, config editor, notifier | 1 wk |
| M6 Backtest harness | Historical chain replay through FakeAdapter | 2 wks |
| M7 Naked module | Naked put/call rules, extra stress checks | 1 wk |

Stage 1 (paper) can start after M1–M5.

---

## 14. Open Questions

1. **Historical options data source** for Stage 0: options chain history is not free (candidates: ORATS, CBOE DataShop, Polygon). Budget?
2. **Margin type:** is the account on Portfolio Margin or Reg-T? This changes how much naked exposure the limits allow.
3. ~~Wheel vs. stop~~ **Resolved (R7):** no assignment; roll, otherwise close.
4. **Legacy positions:** should the bot manage the existing ~500 positions, or only positions it opens?
5. **Hosting:** run on a home machine or a cloud VM? IB Gateway needs a stable host and 2FA handling.
6. **Notifications:** which channel (email, Telegram, Pushover, SMS)?
7. **Approval UX in Stage 3:** web UI only, or approve from a phone notification?
8. ~~R1 definition~~ **Resolved:** the floor is **Excess Liquidity ≥ $100k**.
9. ~~R5 scope~~ **Resolved:** margin restoration **may close any position**, including manual and legacy ones.
10. **Premium cash (R8):** collected premium arrives as cash. Should the bot leave it for you to invest, or (a later feature) buy a configured ETF with it?

---

## 15. Deployment: How the Bot Runs

The bot is a long-running program on an **always-on machine**. It does not
run inside a chat session; those are temporary.

```
 Always-on host (home mini-PC or small cloud VM, US-East)
 ┌──────────────────────────────────────────────────────────┐
 │ docker compose                                           │
 │  ┌──────────────┐   API (localhost)   ┌────────────────┐ │
 │  │ IB Gateway   │◀───────────────────▶│ trading-engine │ │
 │  │ + IBC        │                     │ python -m      │ │
 │  │ (auto login, │                     │ src.trading.run│ │
 │  │  daily       │                     └───────┬────────┘ │
 │  │  restart)    │                             │ SQLite   │
 │  └──────────────┘                     ┌───────▼────────┐ │
 │                                       │ web-ui (Flask) │─┼──▶ phone / browser
 │                                       └────────────────┘ │    (VPN / Tailscale only)
 └──────────────────────────────────────────────────────────┘
```

| Item | Choice |
|---|---|
| Host | Option A: home mini-PC or always-on desktop (free; depends on home power/internet). Option B: small cloud VM (2 vCPU / 4 GB, about $10–20/mo) in US-East, close to IB's servers. Recommended: **B**, because a home outage during a margin breach is the scenario R1 exists for. |
| Packaging | `docker-compose.yml` with three services: `ib-gateway` (community image with IBC), `trading-engine`, `web-ui`. `restart: unless-stopped` on all three. |
| Secrets | IB username/password in a Docker secret or `.env` on the host only; never in git. |
| Access | The web UI is **not exposed to the internet**. Reach it through Tailscale/WireGuard. Alerts go out via the notifier (push/email). |
| Login / 2FA | IBC handles daily restarts. IB requires a full re-login with 2FA about once a week; the bot alerts you to approve it on IB Key. While logged out it can't trade or monitor margin, so this alert is high priority. |
| Health | A watchdog pings the engine every minute. If the engine is down during market hours, it alerts you (a dead bot can't enforce the $100k floor). Optionally also set an IB-side margin alert in TWS as an independent backup. |
| Updates | Deploy only outside market hours. The engine reconciles on startup (§7.4) before doing anything. |
| Backups | Nightly copy of `journal.db` and config. |

## 16. Role of an LLM

**The bot does not need an LLM, and no LLM is in the trading path.** Every
decision (scan, entry, exit, margin restoration) is a deterministic rule
in this spec, for these reasons:

- **Predictable:** the same inputs always give the same decision, and they can be backtested.
- **Auditable:** every order traces to a rule and its numbers (§9.2).
- **Reliable:** no dependence on an external API being up, fast, or consistent during a margin breach.
- **Free to run:** no per-decision cost.

Optional LLM uses later, all **advisory and outside the order path** (an
LLM can never create, approve or send an order):

| Use | How |
|---|---|
| Daily / weekly report in plain language | Reads the journal, explains what the bot did and why, and highlights anomalies. |
| News and event check | Before an entry, summarizes headlines for the underlying. It can only **veto** (skip) a trade, never add one. |
| Config assistant | "Raise the covered-call delta cap to 0.25" → proposes a config diff that a human applies. |
| Post-mortems | Explains a losing trade or a halt from the journal data. |

If any of these are added, they run as a separate service that reads the
journal. Turning them off changes no trading behavior.

## 17. Risk Disclosure

Selling options, especially naked calls, can lose more than the premium
collected, and naked call losses are theoretically unlimited. Gaps
(earnings, overnight news) bypass stop-losses, because stops only act when
the market is open and liquid. Automation adds its own risks: software bugs,
bad data, and broker outages. The limits, stress tests, and staged rollout
reduce these risks; they do not remove them. The user stays responsible for
every trade the system places.
