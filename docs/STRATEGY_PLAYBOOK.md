# Options-Selling Playbook: High Win Rate, Survivable Losses

## First, the honest part: there is no "always win" strategy

Every options-selling strategy has losing trades. If one didn't, it would be
arbitrage, and the market would price it away within days. What option
sellers actually get is:

- **A high win rate.** A short option at 0.15 delta expires worthless roughly 85% of the time.
- **Small, capped wins and occasional large losses.** You collect $1, and sometimes you lose $5–$10.
- **Long calm stretches, broken by sudden crashes.** For example: Feb 2018 (VIX spike), Mar 2020, 2022, and Aug 2024. Accounts that sold too much premium with too much margin were wiped out in those weeks.

So the realistic goal is **"win most months, and never let a loss end the
game."** The playbook below aims for that, and every rule exists for one of
those two purposes. It fits the spec's requirements: the $100k Excess
Liquidity floor, minimal transactions, covered calls with low delta and high
premium, and put sales.

---

## The strategy: a disciplined wheel, with a tail hedge

### Core loop

```
            ┌─────────────────────────────────────────┐
            ▼                                         │
  Sell cash-secured put (0.15–0.25 Δ, 30–45 DTE)      │
            │                                         │
     expires OTM ──── keep premium, repeat ───────────┘
            │
     assigned → own 100 shares at (strike − premium)
            │
  Sell covered call (0.10–0.20 Δ, strike ≥ cost basis)
            │
     expires OTM ──── keep premium, sell another call
            │
     called away → profit on shares + premiums → back to puts
```

The wheel works because **assignment is not a loss event**: on a stock you
chose to own, it's the plan. That turns many would-be losing trades into a
cheaper entry into a stock you wanted anyway.

### Rule 1: Only trade stocks you would hold through a 30% drop
- Large, profitable, liquid companies and broad ETFs (SPY, QQQ, IWM). No meme stocks, no biotech before trial results, no recent IPOs.
- **Biggest single source of permanent loss:** a put on a stock that falls and never recovers. Stock selection is the main risk control, before any Greek.

### Rule 2: Sell when premium is rich, not on a fixed schedule
- IV rank ≥ 30 to enter at all, and ≥ 50 preferred (the spec's Opportunity Tracker, §5.10).
- Sell **puts after a down move** and **calls after an up move**, when premium is higher for the same delta.
- Selling when implied volatility is above realized volatility is where the long-run edge for sellers comes from.

### Rule 3: Low delta, monthly expirations
- Puts at 0.15–0.25 delta; covered calls at 0.10–0.20 delta.
- 30–45 DTE. Time decay speeds up over this window, and monthly cycles mean about 12 trades a year per position instead of about 50 with weeklies (fees, R2).

### Rule 4: Size so that the worst case is boring
- **Excess Liquidity ≥ $100k at all times**, plus a $25k buffer before any new trade (R1).
- Max **10% of NAV** of assignment value per stock, and **30%** per sector.
- **Total put assignment value ≤ 100% of NAV.** If every put were assigned at once, the account would own stocks it can afford, without a margin call.
- Stress test: after a −15% market move with IV +50%, losses stay below 25% of NAV and EL stays above $100k.

### Rule 5: Avoid known landmines
- **No new positions across earnings** unless the expiry is chosen deliberately after the IV crush and the stock passes Rule 1.
- **No covered calls through an ex-dividend date** when the call's extrinsic value is less than the dividend, because of the early-assignment risk.
- Close any short option near the strike on expiration day (pin risk).
- **Pause new entries** when VIX > 35 or SPY is down > 3% intraday.

### Rule 6: Manage losers with rolls and assignment, not panic
| Situation | Action |
|---|---|
| Put tested (delta > 0.50), > 7 DTE | Roll down and out for a **net credit**. If no credit roll exists, accept assignment (Rule 1 says you want the stock). |
| Put deep ITM at expiry | Take assignment, then start selling covered calls. |
| Covered call ITM | Roll up and out for a credit, or let the shares go at a profit (strike ≥ cost basis). |
| Stock breaks thesis (fraud, guidance collapse) | **Exit fully**, both stock and options. This is the one case where you take the loss. |
| EL < $100k | The bot closes positions automatically (spec §6.5). Not optional. |

### Rule 7: Buy cheap crash insurance
- Each month, spend **5–10% of the premium collected** on far-OTM SPY puts (about 10–15% below the market, 60–90 DTE). That's one extra order a month.
- In normal months this costs a little return. In a crash, these puts gain sharply, which offsets assignments and props up Excess Liquidity at the exact moment the $100k floor is under pressure.
- This is the closest you can get to "always survive." It doesn't make the strategy always win; it stops the bad months from being fatal.

### Rule 8: Hold to expiration by default
- Most options expire worthless, which costs no commission.
- Close early only when ≥ 80% of the premium is captured with ≥ 10 DTE left (so the margin can be re-used), or when a rule above says to.

---

## What to expect (illustrative, not a promise)

| Metric | Typical range for this style |
|---|---|
| Trades that expire or close profitably | ~80–90% |
| Months with positive P&L | ~70–85% |
| Annual return on capital used | ~8–15%, before taxes. In strong bull markets this lags simply owning the stocks, because covered calls cap the upside. |
| Worst month | A loss of 1–3× a typical month's income, possibly more in a 2020-style crash, even with the hedge |
| Fees | Target < 3% of premium collected |

These ranges are **assumptions to test, not results**. The backtest (plan T37,
spec Stage 0) has to confirm or correct them on real history, including 2020
and 2022, before any live money is used.

---

## How this maps to the bot

| Playbook rule | Spec section |
|---|---|
| 1. Stock selection | Watchlist with `willing_to_own` (§10), §5.1 filters |
| 2. Rich premium timing | Opportunity Scanner and Tracker (§5.9–5.10) |
| 3. Delta / DTE | `entry` config (§10) |
| 4. Sizing / $100k floor | Risk Gate (§6.1–6.2), margin monitor (§6.5) |
| 5. Landmines | §5.1 blackouts, pin-risk sweep (§8) |
| 6. Managing losers | Position Manager (§5.2–5.3, §5.7) |
| 7. Tail hedge | **New:** add a `tail_hedge` strategy (plan follow-up after T33) |
| 8. Hold to expiry | Fee-aware rules (§5.8) |
