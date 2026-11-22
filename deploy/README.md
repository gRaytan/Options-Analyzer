# Server deployment

Runs IB Gateway, the trading engine and the web UI with Docker Compose on an
always-on machine (spec §15). For a first test on your own computer, the
simpler setup in `docs/IB_CONNECTION_GUIDE.md` is enough.

## 1. Server
- Small Linux VM (2 vCPU, 4 GB RAM), ideally in US-East, close to IB.
- Install Docker and the Compose plugin.
- Install **Tailscale** (or another VPN) so you can reach the web UI privately.
  Don't open ports 5000 or 5900 to the internet.

## 2. Configure
```bash
git clone https://github.com/gRaytan/Options-Analyzer.git && cd Options-Analyzer
cp .env.example .env                              # TRADING_ACCOUNT_ID=DU...
cp deploy/gateway.env.example deploy/gateway.env  # IB paper username/password
chmod 600 .env deploy/gateway.env
```

## 3. Start
```bash
docker compose up -d --build
docker compose logs -f trading-engine     # expect "NLV $... EL $... cushion ...%"
docker compose ps                         # trading-engine should become "healthy"
```

On first login and about weekly afterwards, IB asks for **2FA**: approve it in
the IB Key app on your phone. To watch the Gateway screen, open a VNC client
to `127.0.0.1:5900` through the VPN or an SSH tunnel
(`ssh -L 5900:127.0.0.1:5900 server`).

## 4. Ports and safety
| Port | Where | Purpose |
|---|---|---|
| 4004 / 4003 | compose network only | API relay to Gateway (paper / live) |
| 5000 | 127.0.0.1 on the server | Web UI |
| 5900 | 127.0.0.1 on the server | Gateway VNC screen |

- `READ_ONLY_API: "yes"` makes IB block orders at the source. It stays on through Stage 2 (spec §11).
- The engine also connects read-only, and refuses to trade a live account until mode gating (plan T28).
- Secrets live only in `.env` and `deploy/gateway.env` on the server (both gitignored).

## 5. Switching to live (later, still read-only)
In `deploy/gateway.env`, set `TRADING_MODE=live` and the live username and password. In
`docker-compose.yml`, set `IB_PORT: "4003"` for both services. In `.env`, set the live `U...` account.

## Updating the Gateway image
The image tag is pinned. To update it, choose a newer tag from
`ghcr.io/gnzsnz/ib-gateway` that's at least two weeks old, change it in
`docker-compose.yml`, and redeploy outside market hours.
