from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


def _normalize_network(value: str) -> str:
    network = (value or "mainnet").strip().lower()
    if network not in {"mainnet", "testnet"}:
        raise ValueError(f"Unsupported Hyperliquid network '{value}' (expected mainnet or testnet)")
    return network


def _normalize_tif(value: str) -> str:
    mapping = {"GTC": "Gtc", "IOC": "Ioc", "ALO": "Alo"}
    key = (value or "GTC").strip().upper()
    if key not in mapping:
        raise ValueError(f"Unsupported time-in-force '{value}'")
    return mapping[key]


def _symbol_to_coin(symbol: str) -> str:
    name = symbol.upper()
    return name[:-5] if name.endswith("-PERP") else name


def _coin_to_symbol(coin: str) -> str:
    base = coin.split(":")[-1]
    return f"{base}-PERP"


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class HyperliquidConfig:
    network: str = _normalize_network(os.getenv("HYPERLIQUID_NETWORK", "mainnet"))
    api_base: Optional[str] = os.getenv("HYPERLIQUID_API_BASE")
    wallet_address: str = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "")
    private_key: str = os.getenv("HYPERLIQUID_PRIVATE_KEY", "")
    dex: str = os.getenv("HYPERLIQUID_DEX", "")
    request_timeout: float = float(os.getenv("HYPERLIQUID_HTTP_TIMEOUT", "30"))

    def resolve_base_url(self) -> str:
        if self.api_base:
            return self.api_base.rstrip("/")
        if self.network == "testnet":
            return constants.TESTNET_API_URL
        return constants.MAINNET_API_URL


class HyperliquidClient:
    """Thin wrapper around the public Hyperliquid API via the official Python SDK."""

    def __init__(self, config: Optional[HyperliquidConfig] = None):
        self.config = config or HyperliquidConfig()
        base_url = self.config.resolve_base_url()
        self.base_url = base_url
        self.dex = self.config.dex
        self._private_key = (self.config.private_key or "").strip()
        wallet_address = (self.config.wallet_address or "").strip()
        self.wallet: Optional[LocalAccount] = None
        if self._private_key:
            self.wallet = Account.from_key(self._private_key)
            if not wallet_address:
                wallet_address = self.wallet.address
        if not wallet_address:
            raise ValueError("HYPERLIQUID_WALLET_ADDRESS must be set when HYPERLIQUID_PRIVATE_KEY is missing")
        self.wallet_address = wallet_address
        self.info = Info(base_url=base_url, skip_ws=True, timeout=self.config.request_timeout)
        self.exchange: Optional[Exchange] = None
        if self.wallet is not None:
            self.exchange = Exchange(
                wallet=self.wallet,
                base_url=base_url,
                account_address=self.wallet_address,
                timeout=self.config.request_timeout,
            )

    def _require_exchange(self) -> Exchange:
        if self.exchange is None:
            raise ValueError("HYPERLIQUID_PRIVATE_KEY must be configured to submit orders")
        return self.exchange

    def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        mids = self.info.all_mids(self.dex)
        tickers: Dict[str, Dict[str, Any]] = {}
        for coin, price in mids.items():
            symbol = _coin_to_symbol(coin)
            asset = self.info.coin_to_asset.get(coin)
            sz_decimals = self.info.asset_to_sz_decimals.get(asset, 5) if asset is not None else 5
            tickers[symbol] = {"price": _safe_float(price), "szDecimals": sz_decimals}
        return tickers

    def get_positions(self) -> List[Dict[str, Any]]:
        state = self.info.user_state(self.wallet_address, dex=self.dex)
        output: List[Dict[str, Any]] = []
        for item in state.get("assetPositions", []):
            position = item.get("position") or {}
            coin = position.get("coin")
            if not coin:
                continue
            qty = _safe_float(position.get("szi"))
            symbol = _coin_to_symbol(coin)
            output.append(
                {
                    "coin": coin,
                    "symbol": symbol,
                    "szi": qty,
                    "raw": position,
                }
            )
        return output

    def get_open_orders(self) -> List[Dict[str, Any]]:
        orders = self.info.open_orders(self.wallet_address, dex=self.dex)
        output: List[Dict[str, Any]] = []
        for order in orders:
            symbol = _coin_to_symbol(order.get("coin", ""))
            output.append({**order, "symbol": symbol})
        return output

    def close_positions(self, dry_run: bool = False, slippage: float = 0.02) -> Dict[str, Any]:
        positions = [pos for pos in self.get_positions() if abs(pos.get("szi", 0)) > 0]
        if not positions:
            return {"status": "ok", "orders": []}

        tickers = self.get_tickers()
        orders_summary: List[Dict[str, Any]] = []
        exchange: Optional[Exchange] = None
        if not dry_run:
            exchange = self._require_exchange()
        for pos in positions:
            qty = _safe_float(pos.get("szi"))
            if qty == 0:
                continue
            symbol = pos.get("symbol") or ""
            ticker = tickers.get(symbol)
            mid_price = ticker.get("price") if ticker else None
            if not mid_price:
                orders_summary.append(
                    {"symbol": symbol, "side": "BUY" if qty < 0 else "SELL", "size": abs(qty), "status": "skipped"}
                )
                continue

            is_buy = qty < 0
            limit_price = mid_price * (1 + slippage if is_buy else 1 - slippage)
            order_payload = {
                "symbol": symbol,
                "side": "BUY" if is_buy else "SELL",
                "size": abs(qty),
                "limit_price": limit_price,
            }
            if dry_run:
                order_payload["status"] = "dry-run"
                orders_summary.append(order_payload)
                continue

            coin = _symbol_to_coin(symbol)
            try:
                response = exchange.order(
                    coin,
                    is_buy,
                    abs(qty),
                    limit_price,
                    {"limit": {"tif": "Ioc"}},
                    reduce_only=True,
                )
                order_payload["status"] = "submitted"
                order_payload["response"] = response
            except Exception as exc:  # noqa: BLE001
                order_payload["status"] = "error"
                order_payload["error"] = str(exc)
            orders_summary.append(order_payload)

        return {"status": "ok", "orders": orders_summary}

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: float,
        order_type: str,
        limit_price: Optional[float] = None,
        tif: str = "GTC",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        order_type = order_type.upper()
        coin = _symbol_to_coin(symbol)
        is_buy = action.upper() == "BUY"

        if order_type not in {"LMT", "LIMIT"}:
            raise ValueError(f"Unsupported order type '{order_type}' for Hyperliquid exchange API")
        if limit_price is None:
            raise ValueError("limit_price is required for limit orders")

        exchange = self._require_exchange()
        response = exchange.order(
            coin,
            is_buy,
            quantity,
            limit_price,
            {"limit": {"tif": _normalize_tif(tif)}},
            reduce_only=reduce_only,
        )
        return response
