from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .agent_market import (
    AgentMarketAnalysisProvider,
    AgentMarketClient,
    AgentMarketError,
    AgentMarketSettings,
)
from .hyperliquid_api import HyperliquidClient, HyperliquidConfig
from .models import SymbolSnapshot

logger = logging.getLogger(__name__)

DEFAULT_MARKETS = ["BTC-PERP", "ETH-PERP", "SOL-PERP"]
COINGECKO_ID_MAP = {
    "BTC-PERP": "bitcoin",
    "ETH-PERP": "ethereum",
    "SOL-PERP": "solana",
    "ARB-PERP": "arbitrum",
    "AVAX-PERP": "avalanche-2",
    "OP-PERP": "optimism",
}


@dataclass
class StrategySettings:
    portfolio_value: float = float(os.getenv("HYPERLIQUID_PORTFOLIO_VALUE", "997.5"))
    min_order_value: float = float(os.getenv("HYPERLIQUID_MIN_ORDER_VALUE", "10"))
    primary_markets: Sequence[str] = field(
        default_factory=lambda: _parse_markets(os.getenv("HYPERLIQUID_SYMBOLS"))
    )

    def __post_init__(self) -> None:
        if not self.primary_markets:
            self.primary_markets = DEFAULT_MARKETS


class MarketMaker:
    def __init__(
        self,
        client: HyperliquidClient,
        analysis_provider,
        settings: StrategySettings,
        fallback_provider=None,
    ):
        self.client = client
        self.analysis_provider = analysis_provider
        self.fallback_provider = fallback_provider
        self.settings = settings

    def run_cycle(self, execute: bool) -> None:
        dry_run = not execute
        logger.info("Starting Avellaneda-Stoikov cycle | dry_run=%s", dry_run)

        positions = self.client.get_positions()
        inventory_map = self._build_inventory_map(positions)
        ticker_map = self.client.get_tickers()
        snapshots = self._build_snapshots(ticker_map, inventory_map)

        if not snapshots:
            raise RuntimeError("No market snapshots available; check tickers or config")

        analysis = self._fetch_analysis(snapshots)
        params = analysis["parameters"]
        recommendations = analysis["strategyRecommendations"]
        max_position = recommendations["maxPosition"]
        min_spread = recommendations["minSpread"]
        max_spread = recommendations["maxSpread"]

        logger.info(
            "Parameters γ=%.3f σ=%.3f κ=%.3f T=%s",
            params["gamma"],
            params["sigma"],
            params["kappa"],
            params["timeHorizon"],
        )
        logger.info(
            "Spread bounds %.3f%% - %.3f%% | max position %s",
            min_spread * 100,
            max_spread * 100,
            max_position,
        )

        orders_placed = 0
        orders_skipped = 0

        for snapshot in snapshots:
            symbol = snapshot.symbol
            ticker = ticker_map.get(symbol)
            if not ticker:
                logger.warning("Skipping %s: missing ticker", symbol)
                orders_skipped += 1
                continue

            mid_price = ticker.get("price") or ticker.get("mid")
            if not mid_price or mid_price <= 0:
                logger.warning("Skipping %s: invalid price", symbol)
                orders_skipped += 1
                continue

            sz_decimals = int(ticker.get("szDecimals", 5))
            current_inventory = snapshot.inventory

            bid_spread, ask_spread = calculate_spreads(params, current_inventory, max_position)
            bid_spread = max(min_spread, min(bid_spread, max_spread))
            ask_spread = max(min_spread, min(ask_spread, max_spread))

            bid_price = round_price(mid_price * (1 - bid_spread), symbol)
            ask_price = round_price(mid_price * (1 + ask_spread), symbol)

            bid_qty_raw = calculate_position_size(
                symbol,
                mid_price,
                max_position,
                current_inventory,
                self.settings,
                side="bid",
            )
            ask_qty_raw = calculate_position_size(
                symbol,
                mid_price,
                max_position,
                current_inventory,
                self.settings,
                side="ask",
            )

            bid_qty = round_size(bid_qty_raw, sz_decimals)
            ask_qty = round_size(ask_qty_raw, sz_decimals)

            logger.info(
                "%s | mid=%.2f inventory=%.4f szDec=%d spreads=(%.3f%%, %.3f%%)",
                symbol,
                mid_price,
                current_inventory,
                sz_decimals,
                bid_spread * 100,
                ask_spread * 100,
            )
            logger.info(
                "%s | quotes=%.2f x %.6f / %.2f x %.6f",
                symbol,
                bid_price,
                bid_qty,
                ask_price,
                ask_qty,
            )

            if bid_qty == 0 or ask_qty == 0:
                logger.warning("Skipping %s: quantities below minimum", symbol)
                orders_skipped += 1
                continue

            if dry_run:
                orders_placed += 2
                continue

            try:
                self.client.place_order(symbol, "BUY", bid_qty, order_type="LMT", limit_price=bid_price)
                self.client.place_order(
                    symbol, "SELL", ask_qty, order_type="LMT", limit_price=ask_price
                )
                orders_placed += 2
            except Exception as exc:  # noqa: BLE001
                logger.exception("Order placement failed for %s", symbol)
                continue

        logger.info(
            "Cycle completed | placed=%s skipped=%s dry_run=%s",
            orders_placed,
            orders_skipped,
            dry_run,
        )

    def close_all_positions(self, execute: bool) -> None:
        dry_run = not execute
        logger.info("Closing all positions | dry_run=%s", dry_run)
        result = self.client.close_positions(dry_run=dry_run)
        logger.info("Close result: %s", json.dumps(result, indent=2))

    def _fetch_analysis(self, snapshots: List[SymbolSnapshot]) -> Dict[str, Any]:
        try:
            return self.analysis_provider.fetch_analysis(snapshots)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Primary analysis provider failed: %s", exc)
            if self.fallback_provider is None:
                raise
            return self.fallback_provider.fetch_analysis(snapshots)

    def _build_inventory_map(self, positions: List[Dict[str, Any]]) -> Dict[str, float]:
        inventory: Dict[str, float] = {}
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                coin = pos.get("coin")
                if coin:
                    symbol = f"{coin}-PERP"
            if not symbol:
                continue
            qty = pos.get("szi", pos.get("position", 0))
            inventory[symbol] = float(qty or 0)
        return inventory

    def _build_snapshots(
        self,
        ticker_map: Dict[str, Dict[str, Any]],
        inventory_map: Dict[str, float],
    ) -> List[SymbolSnapshot]:
        changes = fetch_24h_changes(self.settings.primary_markets)
        snapshots: List[SymbolSnapshot] = []
        for symbol in self.settings.primary_markets:
            ticker = ticker_map.get(symbol)
            if not ticker:
                continue
            price = ticker.get("price") or ticker.get("mid")
            if not price:
                continue
            sz_decimals = int(ticker.get("szDecimals", 5))
            snapshots.append(
                SymbolSnapshot(
                    symbol=symbol,
                    mid_price=float(price),
                    sz_decimals=sz_decimals,
                    inventory=inventory_map.get(symbol, 0.0),
                    change_24h=changes.get(symbol),
                )
            )
        return snapshots


def calculate_spreads(params: Dict[str, float], current_inventory: float, max_inventory: float) -> Tuple[float, float]:
    gamma = params["gamma"]
    sigma = params["sigma"]
    T = params["timeHorizon"] / 60.0
    inv_weight = params.get("inventoryRiskWeight", 0.2)

    base_spread = gamma * sigma * sigma * T
    inventory_ratio = current_inventory / max_inventory if max_inventory else 0
    inventory_skew = inv_weight * inventory_ratio
    return base_spread - inventory_skew, base_spread + inventory_skew


def calculate_position_size(
    symbol: str,
    price: float,
    max_position: float,
    current_inventory: float,
    settings: StrategySettings,
    side: str,
) -> float:
    capital_per_market = settings.portfolio_value / max(len(settings.primary_markets), 1)
    max_notional = capital_per_market * 0.5
    max_qty_by_capital = max_notional / price
    if max_position <= 0:
        return 0.0
    max_qty = min(max_qty_by_capital, max_position)

    inventory_factor = 1.0
    if side == "bid" and current_inventory > 0:
        inventory_factor = max(0.3, 1 - abs(current_inventory) / max_position)
    elif side == "ask" and current_inventory < 0:
        inventory_factor = max(0.3, 1 - abs(current_inventory) / max_position)

    qty = max_qty * inventory_factor
    min_qty = settings.min_order_value / price
    if qty < min_qty:
        return 0.0
    return qty


def round_size(size: float, decimals: int) -> float:
    return round(size, decimals)


def round_price(price: float, symbol: str) -> float:
    if price >= 10000:
        return round(price / 10) * 10
    if price >= 100:
        return round(price)
    if price >= 10:
        return round(price, 1)
    if price >= 1:
        return round(price, 2)
    return round(price, 4)


def fetch_24h_changes(symbols: Sequence[str]) -> Dict[str, float]:
    ids = {COINGECKO_ID_MAP[symbol] for symbol in symbols if symbol in COINGECKO_ID_MAP}
    if not ids:
        return {}
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(ids),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch Coingecko changes: %s", exc)
        return {}

    data = response.json()
    output: Dict[str, float] = {}
    for symbol, cg_id in COINGECKO_ID_MAP.items():
        if symbol not in symbols:
            continue
        change = data.get(cg_id, {}).get("usd_24h_change")
        if change is not None:
            output[symbol] = change
    return output


def _parse_markets(value: Optional[str]) -> List[str]:
    if not value:
        return list(DEFAULT_MARKETS)
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def build_analysis_provider(mode: str, client: HyperliquidClient):
    del client  # unused; preserved for backward compatibility
    mode = mode.lower()
    if mode not in {"auto", "agent"}:
        raise ValueError(
            f"Invalid analysis provider mode: {mode}. The HTTP /analysis fallback has been removed; configure agent.market."
        )

    try:
        agent_client = AgentMarketClient(AgentMarketSettings())
        agent_provider = AgentMarketAnalysisProvider(agent_client)
        return agent_provider, None
    except ValueError as exc:  # raised when AGENT_MARKET_API_KEY missing or invalid
        raise ValueError(
            "AGENT_MARKET_API_KEY must be set to run the Avellaneda analysis now that the HTTP fallback is gone."
        ) from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Hyperliquid Avellaneda requester")
    parser.add_argument("--execute", action="store_true", help="Submit live orders instead of dry run")
    parser.add_argument("--close-only", action="store_true", help="Only flatten positions")
    parser.add_argument(
        "--analysis-provider",
        choices=["auto", "agent"],
        default=os.getenv("ANALYSIS_PROVIDER", "auto"),
        help="Choose how Avellaneda parameters are generated (agent.market required)",
    )
    parser.add_argument("--continuous", action="store_true", help="Run repeatedly (not yet implemented)")
    parser.add_argument("--interval", type=int, default=5, help="Minutes between cycles in continuous mode")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = HyperliquidClient(HyperliquidConfig())
    provider, fallback = build_analysis_provider(args.analysis_provider, client)
    settings = StrategySettings()
    maker = MarketMaker(client, provider, settings, fallback_provider=fallback)

    if args.close_only:
        maker.close_all_positions(execute=args.execute)
    else:
        maker.run_cycle(execute=args.execute)

    if args.continuous:
        logger.warning("Continuous mode not implemented; rerun manually for now")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
