from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .models import SymbolSnapshot

logger = logging.getLogger(__name__)


@dataclass
class AgentMarketSettings:
    api_key: str = os.getenv("AGENT_MARKET_API_KEY", "")
    base_url: str = os.getenv("AGENT_MARKET_BASE_URL", "https://api.agent.market")
    max_credit_per_instance: float = float(os.getenv("AGENT_MARKET_MAX_CREDIT", "0.05"))
    instance_timeout: int = int(os.getenv("AGENT_MARKET_INSTANCE_TIMEOUT", "90"))
    gen_reward_timeout: int = int(os.getenv("AGENT_MARKET_REWARD_TIMEOUT", str(48 * 3600)))
    poll_interval: float = float(os.getenv("AGENT_MARKET_POLL_INTERVAL", "5"))
    max_polls: int = int(os.getenv("AGENT_MARKET_MAX_POLLS", "18"))
    percentage_reward: float = float(os.getenv("AGENT_MARKET_PERCENTAGE_REWARD", "0.5"))
    side_effect_free: bool = os.getenv("AGENT_MARKET_SIDE_EFFECT_FREE", "false").lower() == "true"
    max_providers: int = int(os.getenv("AGENT_MARKET_MAX_PROVIDERS", "1"))
    contest_mode: bool = os.getenv("AGENT_MARKET_CONTEST_MODE", "false").lower() == "true"


class AgentMarketError(RuntimeError):
    pass


class AgentMarketClient:
    def __init__(self, settings: Optional[AgentMarketSettings] = None):
        self.settings = settings or AgentMarketSettings()
        if not self.settings.api_key:
            raise ValueError("AGENT_MARKET_API_KEY is required")
        self._base = self.settings.base_url.rstrip("/")

    def create_instance(self, background: str) -> Dict[str, Any]:
        payload = {
            "background": background,
            "max_credit_per_instance": self.settings.max_credit_per_instance,
            "instance_timeout": self.settings.instance_timeout,
            "gen_reward_timeout": self.settings.gen_reward_timeout,
            "percentage_reward": self.settings.percentage_reward,
            "side_effect_free": self.settings.side_effect_free,
            "max_providers": self.settings.max_providers,
            "contest_mode": self.settings.contest_mode,
        }
        url = f"{self._base}/v1/instances"
        response = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.settings.api_key,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        logger.info("Created agent.market instance", extra={"instance_id": data.get("id")})
        return data

    def fetch_chat_messages(self, instance_id: str) -> List[Dict[str, Any]]:
        url = f"{self._base}/v1/chat/{instance_id}"
        response = requests.get(url, headers={"x-api-key": self.settings.api_key}, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise AgentMarketError(f"Unexpected chat payload for {instance_id}: {data}")
        return data

    def poll_provider_message(self, instance_id: str) -> Optional[str]:
        for attempt in range(self.settings.max_polls):
            if attempt:
                time.sleep(self.settings.poll_interval)
            try:
                messages = self.fetch_chat_messages(instance_id)
            except requests.RequestException as exc:
                logger.warning("Failed to poll agent.market", extra={"error": str(exc)})
                continue

            provider_messages = [
                msg for msg in messages if msg.get("sender") == "provider" and msg.get("message")
            ]
            if provider_messages:
                provider_messages.sort(
                    key=lambda msg: msg.get("timestamp", ""),
                    reverse=True,
                )
                message = provider_messages[0]["message"]
                logger.info(
                    "Received provider response",
                    extra={"instance_id": instance_id, "attempt": attempt + 1},
                )
                return message

        return None


class AgentMarketAnalysisProvider:
    """Fetch Avellaneda parameters by delegating to agent.market providers."""

    def __init__(self, client: AgentMarketClient):
        self.client = client

    def fetch_analysis(self, snapshots: List[SymbolSnapshot]) -> Dict[str, Any]:
        if not snapshots:
            raise AgentMarketError("No symbol snapshots available for analysis")

        background = self._build_background_prompt(snapshots)
        instance = self.client.create_instance(background)
        instance_id = instance.get("id")
        if not instance_id:
            raise AgentMarketError("agent.market did not return an instance id")

        provider_message = self.client.poll_provider_message(instance_id)
        if not provider_message:
            raise AgentMarketError("Timed out waiting for agent.market provider response")

        return self._parse_analysis(provider_message)

    def _build_background_prompt(self, snapshots: List[SymbolSnapshot]) -> str:
        snapshot_dicts = [snap.as_prompt_dict() for snap in snapshots]
        snapshot_json = json.dumps(snapshot_dicts, indent=2)
        markets = ", ".join(snap.symbol for snap in snapshots)
        return (
            "# Hyperliquid Avellaneda Parameters\n\n"
            "You run a market-neutral strategy that refreshes Avellaneda-Stoikov parameters before each cycle. "
            "Generate realistic parameters for the current session based on the portfolio inputs below.\n\n"
            "## Inputs\n"
            f"Markets: {markets}\n"
            f"Snapshot (JSON):\n{snapshot_json}\n\n"
            "## Output Requirements\n"
            "Respond with **only** valid JSON using this structure:\n"
            "{\n"
            "  \"marketAnalysis\": {\"volatility\": str, \"liquidity\": str, \"fundingRate\": str, \"trend\": str, \"summary\": str},\n"
            "  \"parameters\": {\"gamma\": float, \"kappa\": float, \"sigma\": float, \"timeHorizon\": int, \"targetInventory\": float, \"inventoryRiskWeight\": float},\n"
            "  \"riskAssessment\": {\"level\": \"LOW|MEDIUM|HIGH\", \"factors\": [str, ...]},\n"
            "  \"strategyRecommendations\": {\"minSpread\": float, \"maxSpread\": float, \"maxPosition\": int, \"notes\": str},\n"
            "  \"reasoning\": str\n"
            "}\n\n"
            "Constraints: gamma 0.05-1.0, sigma 0.01-1.0, timeHorizon in minutes (15-180), spreads between 0.001 and 0.05, "
            "maxPosition 1-10 contracts. Tune these values using the snapshot data and risk intuition."
        )

    def _parse_analysis(self, raw_message: str) -> Dict[str, Any]:
        cleaned = raw_message.strip()
        if "```" in cleaned:
            cleaned = "\n".join(line for line in cleaned.splitlines() if "```" not in line)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            raise AgentMarketError("Provider response did not include JSON payload")

        json_payload = cleaned[start : end + 1]
        analysis = json.loads(json_payload)
        required_keys = [
            "marketAnalysis",
            "parameters",
            "strategyRecommendations",
            "riskAssessment",
            "reasoning",
        ]
        for key in required_keys:
            if key not in analysis:
                raise AgentMarketError(f"agent.market response missing '{key}'")
        return analysis
