# Options-Selling Playbook: High Win Rate, No Assignments, Far From Margin

Owner's rules (spec R7–R9):
1. **No assignments.** Roll when needed.
2. **No cash. Everything is invested.** Short puts are backed by margin on the stock portfolio.
3. **Manage the risk and always stay far from the margin limit.**

## First, the honest part: there is no "always win" strategy

Every options-selling strategy has losing trades. What sellers actually get
is a **high win rate** (a 0.15-delta option expires worthless roughly 85% of
the time), **small capped wins with occasional large losses**, and **long calm
stretches broken by sudden crashes** (Feb 2018, Mar 2020, 2022, Aug 2024).

Two consequences of the owner's rules to understand up front:

- **Rolling postpones a loss, it doesn't erase it.** A roll for a credit buys time for the stock to come back, and most of the time it does. When it doesn't, the bot closes the position at a loss (§5.11 caps rolls at 4, and losses at 3× the original credit). That's the price of never being assigned.
- **A fully invested account takes a double hit in a crash.** Stocks fall **and** short puts lose money, and both reduce Excess Liquidity at once. That's why "far from margin" here means surviving a **−20% market drop** with IV +50%, not just today's numbers.

So the goal is **"win most months, never get assigned, and never come near a
margin call."**

---

## The strategy: covered calls + small short puts, rolled not assigned, with a crash hedge

```
 Stock portfolio (fully invested)
   │
   ├── Covered calls on holdings (0.08–0.15 Δ, 21–45 DTE)
   │      ├─ expires OTM → keep premium, sell the next one
   │      └─ tested → roll UP and OUT for a credit (shares are never called away)
   │
   ├── Short puts on margin (0.10–0.20 Δ, 30–45 DTE), sized small
   │      ├─ expires OTM → keep premium, sell the next one
   │      └─ tested → roll DOWN and OUT for a credit (never assigned)
   │             └─ no good roll / 4 rolls / loss > 3× credit → close
   │
   └── Crash hedge: far-OTM SPY puts bought with 5–10% of premium
```

### Rule 1: Underlyings you'd be comfortable holding through a drop
Large, liquid, profitable companies and broad ETFs. No meme stocks, no
biotech before trial results, no recent IPOs. Even without assignment, a short
put on a stock that keeps falling has to be rolled again and again, and is
finally closed at a loss. Picking strong stocks is what keeps rolls working.

### Rule 2: Sell when premium is rich
- IV rank ≥ 30 to enter, and ≥ 50 preferred.
- Sell **calls after up moves** and **puts after down moves** (spec §5.10).

### Rule 3: Low delta, so rolls are rare
- **Covered calls: 0.08–0.15 delta.** Low enough that the stock rarely reaches the strike. You keep your shares and the upside up to the strike.
- **Short puts: 0.10–0.20 delta.**
- 30–45 DTE, monthly expirations (fewer transactions).

### Rule 4: Stay far from margin. These are hard limits, not targets.
| Limit | Value |
|---|---|
| Excess Liquidity | ≥ $100k always; ≥ $125k to open anything |
| Cushion (EL ÷ net liquidation) | ≥ 40% to open; automatic closing below 30% |
| After a −20% market drop + IV +50% (stocks and options together) | EL still ≥ $100k |
| Total short-put notional (strike × 100 × contracts) | ≤ 50% of net liquidation |
| Per stock / per sector | ≤ 10% / 30% of net liquidation |

The short-put cap is the key one. With no cash in the account, every short put
is extra leverage on top of a fully invested portfolio. **50% means that even if
every put were assigned at once, the margin loan would stay manageable.** The
bot still rolls them, so that's a worst-case bound, not the plan.

### Rule 5: Never let assignment happen
The Assignment Guard (spec §5.11) checks every 5 minutes and rolls when:
- the option is ITM with < $0.15 of extrinsic value left (early-assignment zone)
- delta reaches 0.50
- ≤ 5 days to expiry and the stock is within 2% of the strike
- a covered call is ITM before an ex-dividend date (roll the day before)
- on expiration day anything is ITM or within 1% of the strike: rolled or closed by 15:00 ET

**Roll discipline:**
- Always one combo order, for a **net credit**, to the same or a better strike.
- **Never add contracts.**
- Max 4 rolls, max 90 DTE.
- Otherwise close and take the loss.

### Rule 6: Avoid known landmines
- No new positions across earnings.
- Pause new entries when VIX > 35 or SPY is down > 3% intraday.
- Tested positions are still managed and rolled during a pause.

### Rule 7: Buy crash insurance
Each month, spend **5–10% of the premium collected** on far-OTM SPY puts
(10–15% below the market, 60–90 DTE). That's one order a month. In a crash
they gain sharply, which offsets the double hit and props up Excess Liquidity
at the exact moment the margin limits matter. For a fully invested account
selling puts, this rule matters most.

### Rule 8: Hold to expiration by default
Options that expire worthless cost no commission. Close early only when ≥ 80%
of the premium is captured with ≥ 10 DTE left, or when Rules 4–5 require it.

---

## What to expect (illustrative, to be confirmed by backtest)

| Metric | Typical range for this style |
|---|---|
| Trades expiring or closed profitably (per roll chain) | ~80–90% |
| Positions needing at least one roll | ~10–20% |
| Rolled positions eventually closed at a loss | a minority, but they're the main source of losses |
| Months with positive option P&L | ~70–85% |
| Extra yield from options on top of the stock portfolio | ~4–10% a year on net liquidation, depending on how much of the limits is used |
| Cost of the rules | Covered calls cap upside in strong rallies; the hedge costs 5–10% of premium |

The backtest (plan T37) must cover 2020 and 2022 and report: floor breaches,
how many roll chains ended in a loss, worst month, and whether any assignment
would have slipped through.

---

## How this maps to the bot

| Playbook rule | Spec section |
|---|---|
| 1. Underlyings | Watchlist (§10), §5.1 filters |
| 2. Rich premium timing | Opportunity Scanner and Tracker (§5.9–5.10) |
| 3. Delta / DTE | `entry` config (§10), §5.2–5.3 |
| 4. Far from margin | Risk Gate (§6.1–6.2), margin monitor (§6.5) |
| 5. No assignments | Assignment Guard (§5.11) |
| 6. Landmines | §5.1 blackouts |
| 7. Crash hedge | `tail_hedge` strategy (plan T41) |
| 8. Hold to expiry | Fee-aware rules (§5.8) |
