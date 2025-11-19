from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


@dataclass
class SymbolSnapshot:
    """Minimal snapshot for each market sent to the agent provider."""

    symbol: str
    mid_price: float
    sz_decimals: int
    inventory: float
    change_24h: Optional[float] = None
    notional_liquidity: Optional[float] = None

    def as_prompt_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Rename fields to a friendlier naming scheme for the LLM prompt.
        data["midPrice"] = data.pop("mid_price")
        data["sizeDecimals"] = data.pop("sz_decimals")
        data["inventory"] = data.pop("inventory")
        data["change24h"] = data.pop("change_24h")
        data["notionalLiquidity"] = data.pop("notional_liquidity")
        return data
