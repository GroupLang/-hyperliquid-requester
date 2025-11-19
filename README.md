# Hyperliquid Requester

Open-source reproduction of the Hyperliquid cron job we run in production. It closes existing positions, asks an agent.market provider for Avellaneda-Stoikov parameters, and then places mirrored quotes directly via the public Hyperliquid API.

## Features
- Copies the production `hyperliquid_marketmaker.py` logic (inventory-aware Avellaneda sizing, $10 minimum order enforcement, per-market sizing).
- Uses an agent.market background instance (instead of the legacy `/analysis` call) so the prompt includes the full market snapshot and providers return JSON with `parameters`, `strategyRecommendations`, and monitoring text.
- Drop-in shell wrapper (`scripts/market_maker_cycle.sh`) that matches the cron job behavior in production.

## Prerequisites
1. **Hyperliquid trading wallet** – provide the account address you want to inspect/trade via `HYPERLIQUID_WALLET_ADDRESS`. Add `HYPERLIQUID_PRIVATE_KEY` (API wallet private key) when you want to execute orders; omit it for dry runs. Optionally override `HYPERLIQUID_API_BASE` if you need a custom builder endpoint; otherwise set `HYPERLIQUID_NETWORK=mainnet|testnet`.
2. **Python 3.9+** – create a virtualenv and install the package:
   ```bash
   cd hyperliquid-requester
   python -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```
3. **Environment file** – copy `.env.example` to `.env` (or export the variables via your secrets manager) and set:
   - Hyperliquid credentials (`HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_WALLET_ADDRESS`, optional `HYPERLIQUID_NETWORK`/`HYPERLIQUID_API_BASE` overrides).
   - Portfolio sizing knobs (`HYPERLIQUID_PORTFOLIO_VALUE`, `HYPERLIQUID_MIN_ORDER_VALUE`, `HYPERLIQUID_SYMBOLS`)
   - `AGENT_MARKET_API_KEY` plus optional `AGENT_MARKET_*` budgeting flags

You can source the env file in your shell (`set -a; source .env; set +a`) or inject variables in the systemd unit/cron job that runs the script.

## Usage
- Dry run (default):
  ```bash
  python -m hyperliquid_requester.market_maker
  ```
- Live orders:
  ```bash
  python -m hyperliquid_requester.market_maker --execute
  ```
- Close-only sweep:
  ```bash
  python -m hyperliquid_requester.market_maker --close-only --execute
  ```
- Cron-friendly wrapper:
  ```bash
  ./scripts/market_maker_cycle.sh --analysis-provider agent
  ```

The module also exposes a console script, so `hyperliquid-requester --execute` works after `pip install -e .`.

## Agent Market prompt
Every cycle the requester gathers `SymbolSnapshot` data (mid price, decimals, inventory, optional change metrics when available) for each `HYPERLIQUID_SYMBOL`. It then creates an agent.market instance with a background prompt similar to the existing agent-market cron job:

- Sectioned Markdown with the snapshot JSON.
- Explicit JSON schema (marketAnalysis, parameters, strategyRecommendations, riskAssessment, reasoning).
- Constraints on γ/σ/time horizon and spread bounds.

The script polls `GET /v1/chat/{instance_id}` every `AGENT_MARKET_POLL_INTERVAL` seconds (default 5) until a provider posts a JSON response or `AGENT_MARKET_MAX_POLLS` is hit (~90 seconds).

## Repository layout
```
hyperliquid-requester/
├── LICENSE
├── README.md
├── pyproject.toml
├── .env.example
├── scripts/
│   └── market_maker_cycle.sh
└── src/hyperliquid_requester/
    ├── __init__.py
    ├── agent_market.py
    ├── hyperliquid_api.py
    ├── market_maker.py
    └── models.py
```

## Next steps
- Wire the new repo to GitHub and push when you're ready (`git remote add origin ...`).
- Drop this folder into your deployment workflow or point a cron/systemd timer at `scripts/market_maker_cycle.sh`.
