# Connecting the Bot to Interactive Brokers

This guide connects the trading engine to your IB account through **IB
Gateway**, IB's lightweight login program for the API. Start with the
**paper** account. Everything here is **read-only**: no order can be sent.

> IB occasionally renames menu items. If a name below doesn't match
> exactly, look for the closest equivalent.

---

## 1. Turn on your paper trading account (one time)

1. Log in to **Client Portal** (ibkr.com → Log In).
2. Go to **Settings → Account Settings → Paper Trading Account** and create or reset it.
3. Note the **paper account number** (starts with **DU**, e.g. `DU1234567`) and the **paper username**. Paper has its own username and password, separate from your live login.
4. On the same page, turn on **sharing of real-time market data with the paper account**. Without it, paper gets delayed data, and the bot refuses to act on delayed data.

Use a paper account with a similar size and holdings to the live one
(fully invested). That's what Stage 1 of the rollout tests against (spec §11).

## 2. Install IB Gateway

1. Download **IB Gateway (Stable)** from ibkr.com → Trading → Platforms → IB Gateway.
2. Install it on the computer that will run the bot: your own computer for testing, the cloud server later (spec §15).

## 3. Log in and configure the API

1. Start IB Gateway, select **IB API** (not FIX), and select **Paper Trading**.
2. Log in with the **paper** username.
3. Open **Configure → Settings → API → Settings** and set:

| Setting | Value | Why |
|---|---|---|
| Socket port | **4002** | Paper port for Gateway (live is 4001) |
| **Read-Only API** | **✔ checked** | IB itself blocks orders. Keep this on through Stages 0–2 |
| Allow connections from localhost only | ✔ checked | Nothing outside this computer can connect |
| Trusted IPs | `127.0.0.1` | Lets the bot connect without a confirmation pop-up |
| Master API client ID | leave empty | — |

4. Under **Configure → Settings → Lock and Exit**, set the **auto restart**
   time to a quiet hour (e.g. 23:45 local). IB restarts the Gateway every day,
   and the bot reconnects automatically afterwards.
5. Click **Apply / OK**.

## 4. Set up the project on that computer (one time)

Requires **Python 3.12**. The pinned pandas version doesn't support 3.13 yet.

```bash
git clone https://github.com/gRaytan/Options-Analyzer.git
cd Options-Analyzer
git checkout main && git pull
python3.12 -m venv venv
source venv/bin/activate                  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                      # then edit .env:
#   TRADING_ACCOUNT_ID=DU1234567
#   IB_PORT=4002
```

## 5. Run the connection check

With IB Gateway running and logged in:

```bash
python scripts/ib_check.py --account DU1234567
```

Expected output:

```
Connecting to 127.0.0.1:4002 as client 3 (read-only)...
OK: connected
  Accounts on this login: DU1234567
  Account checked:       DU1234567 (paper)
  Server version:        176
  IB server time:        2026-11-23 15:04:11+00:00

Account
  Net liquidation:       $1,000,000
  Excess liquidity:      $450,000   (floor $100,000; new trades need $125,000) OK
  Cushion:               45.0%   (new trades need 40%) OK
  Maintenance margin:    $300,000
  Initial margin:        $380,000
  Available funds:       $420,000
  Cash:                  $-5,000

Positions: 3 stock/ETF, 4 option
  QQQM 2026-12-18 C 240        qty    -1  cost     1.25  price     0.80  American
  ...

Market data
  SPY    bid 671.2  ask 671.25  (live)
  QQQM   bid 251.1  ask 251.14  (live)
  XSP options (XSP): 48 expiries, 210 strikes, next 2026-11-23
```

(Your numbers will differ. If you see `WARNING: no account values received
yet`, run it again a minute later; IB sends account values shortly after login.)

**If it fails:**

| Message | Fix |
|---|---|
| `Connect call failed ... 4002` | Gateway isn't running or logged in, or the port isn't 4002 (check API settings) |
| `account DU... is not managed by this login` | The account number doesn't match the login. Use the paper number with the paper login |
| Connects, then drops right away | Another program is using the same client ID. Use `--client-id 5` |
| Pop-up in Gateway asking to accept a connection | Add `127.0.0.1` to Trusted IPs |
| Hangs, then times out | A firewall or antivirus is blocking localhost port 4002 |
| `DELAYED` next to quotes | Paper isn't sharing your live market data (step 1.4), or the subscription is missing (US equity + OPRA options + CBOE indexes) |

Send me the output, whether it's OK or an error, and I'll take it from there.

## 6. Later: the live account (still read-only)

Same steps, but log in to Gateway in **Live** mode. The port is **4001**, and **Read-Only
API stays checked**:

```bash
python scripts/ib_check.py --account U1234567 --port 4001
```

The bot also enforces this itself: a live account (`U…`, or a live port) can
only be opened read-only until the mode-gating task (plan T28) is built and you
explicitly enable it.

## Why this session can't connect directly

The cloud session I work in can't reach IB Gateway on your computer, because
Gateway only accepts connections from its own machine (by design, and the
safe setting). So:

- **Now:** you run `scripts/ib_check.py` (and the later check scripts) on your computer and send me the output.
- **Later:** the always-on cloud server (spec §15) runs Gateway and the bot side by side.

## Security notes
- Never put your IB password in the repository or in `.env` files you share. The IB login stays inside IB Gateway (and later IBC on the server).
- The account number goes in `.env`, which git ignores.
- Weekly 2FA: IB asks you to re-approve the login on IB Key about once a week.
