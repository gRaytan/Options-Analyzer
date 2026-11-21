# Auto-Trading Implementation Plan

Companion to [AUTO_TRADING_SPEC.md](AUTO_TRADING_SPEC.md). Work is split into
**small tasks, one pull request each**. Section references (§) point to the spec.

## Ground rules for every PR

- **One task = one PR**, aiming for ≤ ~400 changed lines excluding tests and fixtures. If a task grows larger, split it.
- **Branch name:** `auto/T<NN>-<short-name>` (e.g. `auto/T06-ib-connection`), based on the latest `main`, or on the previous task's branch when it depends on an unmerged PR.
- **Tests required.** Unit tests run without IB, using `FakeAdapter` from T05 onward. CI (T01) must pass.
- **Existing app keeps working.** CSV upload, dashboard and recommendations must not change behavior unless the task says so.
- **No order can reach a live account until T28** (mode gating) is merged. Before that, order code is exercised only against `FakeAdapter` and the IB **paper** account.
- **PR description:** what changed, how it was tested, the spec sections covered, and follow-ups.

## Critical path

```
T01 → T02 → T03 → T05 → T06 → T07 → T08 → T12          (read-only live dashboard)
                     └→ T04 → T13                        (journal + reconcile)
T07 → T15 → T16 → T20                                    (margin floor alerts)
T09 → T17 → T18 → T19 → T21 → T22 → T26 → T28            (paper trading closes)
T10 → T29 → T30 → T31 → T33                              (paper trading entries)
                                    → Stage 1 (paper) can start
```

---

## Phase A: Foundations

| # | Task | Scope | Done when |
|---|---|---|---|
| **T01** | Scaffolding + CI | Create the `src/trading/` package, `tests/trading/`, a `pytest.ini`, and a GitHub Actions workflow that runs pytest and lint. Add deps: `ib_async`, `pydantic`, `exchange_calendars`. | CI runs green on an empty test; existing `scripts/test_*.py` collected or left untouched |
| **T02** | Config schema | `data/config/trading.json` + pydantic model (§10). Unknown keys are rejected; defaults match the spec; secrets come from `.env`. | Tests cover valid config, invalid values, and missing file → safe defaults (`mode: paper`) |
| **T03** | Domain types | Dataclasses: `OptionContract`, `Quote`, `AccountSnapshot`, `TradeIntent`, `OrderRequest`, `Fill`, `HaltState`. No IB imports. | Types round-trip to and from dicts; tests |
| **T04** | Journal | SQLite `data/trading/journal.db` with the §9.1 tables, append-only writer, simple migration versioning. | Writing and reading every table is tested; DB file is gitignored |
| **T05** | Broker interface + FakeAdapter | `BrokerAdapter` abstract class (positions, account, chains, quotes, whatIf, place, cancel, events). `FakeAdapter` with scriptable prices and fills. | FakeAdapter passes a shared interface test suite |

## Phase B: IB read-only connection (M1, R6)

| # | Task | Scope | Done when |
|---|---|---|---|
| **T06** | IB connection manager | `IBAdapter.connect()`: host, port, client ID, reconnect with 1→60s backoff, connection-state events, refusal to start if the account ID doesn't match config (§4.3). | Manual test against IB paper; unit tests with a mocked `ib_async` |
| **T07** | Account snapshot stream | Subscribe to the account-summary tags (EL, NLV, MaintMargin, InitMargin, AvailableFunds, Cushion) and publish `AccountSnapshot` events. | Live values appear in logs from paper; staleness is flagged after 30s with no update |
| **T08** | Live positions → Portfolio | Map IB positions and portfolio items onto the existing `Portfolio` / `OptionPosition` / `StockPosition` models. | Same `Portfolio` analysis output from a CSV and from a live paper account holding the same positions |
| **T09** | Chains + quotes | `reqSecDefOptParams`, contract qualification, snapshot and streaming quotes with Greeks, market-data-type check (refuse orders on delayed data), pacing limiter. | Can pull a full 30–45 DTE chain for 10 symbols within pacing limits |
| **T10** | History fetcher | Daily bars + IV history via `reqHistoricalData`, cached on disk. | Returns 1 year of bars and IV for a symbol; cache hit on second call |
| **T11** | Docker deployment | `docker-compose.yml`: `ib-gateway` (IBC image), `trading-engine`, `web-ui`; `.env.example`; restart policies (§15). | `docker compose up` on a clean VM connects to paper |
| **T12** | Bot Status page | Flask page: connection state, EL vs. the $100k floor, NLV, margin, live positions (read-only). | Visible on the existing nav; works with the engine down (shows "disconnected") |
| **T13** | Reconciliation | Compare journal-expected positions with IB; write a mismatch event; manual and legacy positions tagged "unmanaged" (§7.4). | FakeAdapter tests: match, extra position, missing position, quantity drift |

## Phase C: Safety (M2, R1)

| # | Task | Scope | Done when |
|---|---|---|---|
| **T14** | Calendars | NYSE hours, holidays and early closes (`exchange_calendars`); earnings and ex-dividend dates (source chosen in this PR). | Tests for holidays, early closes, DST |
| **T15** | Kill switch + halts | `HALT` file, UI button, CLI; halt reasons persisted; restart does not clear a halt (§6.4). | Tests for each trigger and for persistence across restart |
| **T16** | Margin floor monitor (alert-only) | 60s loop with normal / warning / breach / emergency states and hysteresis (§6.5). **Alerts only; no closing yet.** | State-machine tests over EL time series |
| **T17** | Risk gate: sanity checks | §6.3: quote freshness, price within bid/ask, no position flip, no duplicates, contract match. | One test per check, plus boundary cases |
| **T18** | Risk gate: hard limits | §6.1 limits, including post-trade EL ≥ $125k via `whatIfOrder`; risk-reducing intents skip allocation limits. | Property tests: no approved opening breaches any limit |
| **T19** | Stress test | §6.2 scenarios repriced with py-vollib; EL ≥ $100k at −8%. | Known-portfolio fixtures with hand-checked numbers |
| **T20** | Notifier | Pluggable channels (email + one push service); alert levels; rate limiting. | Breach state from T16 sends an alert in a paper test |

## Phase D: Orders and position management (M3, R2, R5)

| # | Task | Scope | Done when |
|---|---|---|---|
| **T21** | Fee model | §7.5 commission and fee estimates; calibrate from `commissionReport`. | Estimates within ±$0.05/contract of paper fills |
| **T22** | Order manager (single leg) | Limit at mid, stepped price ladder, worst-price cap, cancel/replace, `orderRef` idempotency, partial fills (§7.1–7.3). **Paper only.** | FakeAdapter scenarios: fill, partial, timeout, reject, restart mid-order |
| **T23** | Combo orders | BAG orders for spreads and for rolls as one order (§7.1). | Paper: roll a short put as a single combo |
| **T24** | `risk.py` upgrades | Short-put delta flag, `EX_DIVIDEND_ASSIGNMENT_RISK`, `PIN_RISK`, loss flag relative to credit (§5.7). | Existing recommendation page still renders; new flags tested |
| **T25** | Position manager | Hold-to-expiry default, fee-aware early close, never buy back ≤ $0.05, pin-risk sweep, stops (§5.2–5.8) → `TradeIntent`s. | Table-driven tests: position state → expected intent or none |
| **T26** | Margin restoration | Close selection ranked by margin freed ÷ cost, flagged positions first, fewest orders, re-check after each fill (§6.5). Wired to T16. | Simulated breach on FakeAdapter restores EL ≥ $110k with the fewest closes |
| **T27** | Approval queue | Pending Approvals page + approve from notification; 10-minute expiry. | Approve and reject flows tested; expired intents are never sent |
| **T28** | Mode gating | `paper` / `shadow` / `approve` / `auto` per §11, with per-strategy open/manage switches; live account requires an explicit, separate flag. | Test matrix: every mode × intent type → sent / queued / logged-only |

## Phase E: Opportunities and entries (M4, R3, R4)

| # | Task | Scope | Done when |
|---|---|---|---|
| **T29** | Indicators | MA 20/50/200, RSI, ATR, IV rank and percentile, realized vol, term structure, skew (§5.10). | Tests against reference values |
| **T30** | Scanner | Candidate generation for covered calls + puts, hard filters, score formula (§5.9). | Ranked output for a recorded chain fixture matches hand calculation |
| **T31** | Tracker | Store scan history; timing score; 21 DTE fallback (§5.10). | Replay test: fires at the top-quartile moment, not the first acceptable one |
| **T32** | Opportunities page | UI listing best candidates per underlying with score breakdown and trend state. | Renders from journal data |
| **T33** | Entry strategies: CSP + covered calls | Turn tracker signals into opening intents, coverage limit, cash ledger (§5.2–5.3). | Paper: opens one CSP and one covered call through the full pipeline |
| **T34** | Spreads + condors | §5.4–5.5 entries and exits via combo orders. | Paper round trip for each |
| **T35** | Naked options module | §5.6 rules, allowlist, extra stress, 3× stop. Disabled by default. | Tests; it cannot be enabled outside `auto` mode with explicit config |

## Phase F: Backtest and operations

| # | Task | Scope | Done when |
|---|---|---|---|
| **T36** | Historical data loader | Import from the chosen vendor (spec open question 1) into a local store. | Loads 2 years for 5 symbols |
| **T37** | Backtester | Replays history through the strategies, risk gate and FakeAdapter; report on win rate, P&L, max drawdown, fees, and floor breaches. | Report generated for the playbook strategy |
| **T38** | Scheduler | All §8 jobs wired on the market calendar. | Dry-run day on paper with correct job timing |
| **T39** | Watchdog + backups | Engine heartbeat, alert when down in market hours, nightly journal backup (§15). | Killing the engine triggers an alert |
| **T40** | Metrics | Fees % of premium, win rate, P&L by strategy, slippage on the Charts page (§9.4). | Charts render from the journal |

---

## Milestone checkpoints

| After | You can |
|---|---|
| T12 | See live positions and Excess Liquidity from IB in the dashboard (read-only) |
| T20 | Get alerted when EL approaches or breaks $100k |
| T28 | Run Stage 1 **paper**: automatic margin restoration and closes |
| T33 | Run Stage 1 **paper**: full covered-call + put-selling loop |
| T37 | Validate the strategy on history before any live money (Stage 0 sign-off) |
