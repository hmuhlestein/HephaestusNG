"""Cost tracking via LiteLLM proxy.

Queries LiteLLM spend endpoints to track costs per feature/user.
Requires LiteLLM proxy to be running with a database configured.

Usage:
    tracker = CostTracker(
        proxy_url="http://deneb-server:4000",
        api_key="sk-master-key",
    )

    # Get spend for a specific feature
    cost = await tracker.get_feature_cost("auth-system")

    # Get daily breakdown for a feature
    breakdown = await tracker.get_daily_breakdown("auth-system", days=7)

    # Get spend report for all features
    report = await tracker.get_all_features_report(days=30)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger(__name__)


class CostTracker:
    """Track LLM costs per feature via LiteLLM proxy spend endpoints."""

    def __init__(
        self,
        proxy_url: str,
        api_key: str,
    ):
        """Initialize cost tracker.

        Args:
            proxy_url: LiteLLM proxy URL (e.g., "http://deneb-server:4000")
            api_key: Admin/master key for querying spend endpoints
        """
        self.proxy_url = proxy_url.rstrip("/")
        self.api_key = api_key

    async def _get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make authenticated GET request to LiteLLM proxy."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.proxy_url}{endpoint}",
                    headers=headers,
                    params=params or {},
                )

                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(f"LiteLLM API error: {response.status_code} - {response.text[:200]}")
                    return None

        except Exception as e:
            logger.error(f"Failed to query LiteLLM: {e}")
            return None

    async def get_feature_cost(self, feature_name: str) -> Dict[str, Any]:
        """Get total spend for a specific feature (user).

        Args:
            feature_name: The feature name (matches 'user' field in LLM requests)

        Returns:
            Dict with spend info: {spend, total_tokens, prompt_tokens, completion_tokens}
        """
        data = await self._get(
            "/user/info",
            params={"user_id": feature_name},
        )

        if not data:
            return {"spend": 0, "total_tokens": 0, "error": "Could not fetch spend data"}

        user_info = data.get("user_info", {})
        return {
            "spend": user_info.get("spend", 0),
            "total_tokens": 0,  # Not directly available from /user/info
            "feature_name": feature_name,
        }

    async def get_daily_breakdown(
        self,
        feature_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 7,
    ) -> Dict[str, Any]:
        """Get daily spend breakdown for a feature.

        Args:
            feature_name: The feature name
            start_date: Start date (YYYY-MM-DD), defaults to `days` ago
            end_date: End date (YYYY-MM-DD), defaults to today
            days: Number of days to look back (used if start_date not provided)

        Returns:
            Dict with daily breakdown and totals
        """
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        data = await self._get(
            "/user/daily/activity",
            params={
                "start_date": start_date,
                "end_date": end_date,
            },
        )

        if not data:
            return {"results": [], "metadata": {}, "error": "Could not fetch daily activity"}

        # Filter results for the specific feature/user
        results = data.get("results", [])
        metadata = data.get("metadata", {})

        # Note: LiteLLM's /user/daily/activity returns data for the authenticated user
        # To filter by the 'user' field (our feature), we'd need to use /spend/logs
        # or rely on the fact that our proxy key maps to our features

        return {
            "results": results,
            "metadata": metadata,
            "start_date": start_date,
            "end_date": end_date,
        }

    async def get_spend_report(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 30,
    ) -> List[Dict[str, Any]]:
        """Get spend report grouped by customer (feature).

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            days: Number of days to look back

        Returns:
            List of daily spend entries grouped by customer/feature
        """
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        data = await self._get(
            "/global/spend/report",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "group_by": "customer",
            },
        )

        if not data:
            return []

        return data if isinstance(data, list) else []

    async def get_all_features_report(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get aggregated spend for all features over a time period.

        Args:
            days: Number of days to look back

        Returns:
            List of feature spend summaries sorted by total spend
        """
        report = await self.get_spend_report(days=days)

        feature_totals: Dict[str, Dict[str, Any]] = {}

        for day_entry in report:
            customers = day_entry.get("customers", [])
            for customer in customers:
                name = customer.get("customer", "unknown")
                spend = customer.get("total_spend", 0)

                if name not in feature_totals:
                    feature_totals[name] = {
                        "feature_name": name,
                        "total_spend": 0,
                        "days_active": 0,
                        "model_details": [],
                    }

                feature_totals[name]["total_spend"] += spend
                feature_totals[name]["days_active"] += 1

                for model_info in customer.get("metadata", []):
                    feature_totals[name]["model_details"].append(model_info)

        # Sort by spend descending
        sorted_features = sorted(
            feature_totals.values(),
            key=lambda x: x["total_spend"],
            reverse=True,
        )

        return sorted_features

    async def get_feature_cost_from_response(self, response: Dict[str, Any]) -> Optional[float]:
        """Extract cost from an LLM response.

        Args:
            response: Response dict from OpenRouterClient.generate()

        Returns:
            Cost in dollars, or None if not available
        """
        return response.get("cost")
