"""
MetricsCollector — fetches historical metrics from Prometheus.

Prometheus is the sole metrics source. If Prometheus is unavailable,
the collector returns empty dicts and the cost analyzer falls back to
using Kubernetes resource requests as a utilization proxy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


class MetricsCollector:
    def __init__(self, prometheus_url: str | None = None):
        self.prometheus_url = prometheus_url

    # ── Prometheus ─────────────────────────────────────────────────────────────

    async def _prom_query(
        self, query: str, session: aiohttp.ClientSession
    ) -> list | None:
        """Execute an instant PromQL query. Returns result list or None on error."""
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            async with session.get(
                url,
                params={"query": query},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Prometheus returned {resp.status} for query: {query[:80]}")
                    return None
                data = await resp.json()
                return data.get("data", {}).get("result", [])
        except Exception as e:
            logger.warning(f"Prometheus query failed [{query[:80]}]: {e}")
            return None

    async def get_node_metrics_prometheus(self) -> Dict[str, Dict]:
        """
        Fetch per-node CPU & memory utilization percentiles from Prometheus.

        Returns:
            {node_name: {cpu_p50, cpu_p95, mem_p50, mem_p95}}
            Empty dict if Prometheus is unreachable.
        """
        if not self.prometheus_url:
            logger.info("PROMETHEUS_URL not configured — skipping node metrics")
            return {}

        lookback = f"{settings.metrics_lookback_hours}h"
        queries = {
            "cpu_p50": f"quantile_over_time(0.5,  node:node_cpu_utilization:avg1m[{lookback}])",
            "cpu_p95": f"quantile_over_time(0.95, node:node_cpu_utilization:avg1m[{lookback}])",
            "mem_p50": f"quantile_over_time(0.5,  node:node_memory_utilization:avg1m[{lookback}])",
            "mem_p95": f"quantile_over_time(0.95, node:node_memory_utilization:avg1m[{lookback}])",
        }

        result: Dict[str, Dict] = {}
        async with aiohttp.ClientSession() as session:
            for metric_name, query in queries.items():
                data = await self._prom_query(query, session)
                if not data:
                    continue
                for item in data:
                    node = item["metric"].get("node", item["metric"].get("instance", "unknown"))
                    val = float(item["value"][1]) if item.get("value") else 0.0
                    result.setdefault(node, {})[metric_name] = round(val, 4)

        logger.info(f"Prometheus node metrics: {len(result)} nodes")
        return result

    async def get_pod_metrics_prometheus(self) -> Dict[str, Dict]:
        """
        Fetch per-pod CPU & memory from Prometheus.

        Returns:
            {"namespace/pod": {cpu_avg, cpu_p95, mem_avg_gi, mem_p95_gi}}
            Empty dict if Prometheus is unreachable.
        """
        if not self.prometheus_url:
            logger.info("PROMETHEUS_URL not configured — skipping pod metrics")
            return {}

        lookback = f"{settings.metrics_lookback_hours}h"
        queries = {
            "cpu_avg": (
                f"avg_over_time(sum(rate(container_cpu_usage_seconds_total"
                f'{{container!="",container!="POD"}}[5m])) by (namespace,pod)[{lookback}:])'
            ),
            "cpu_p95": (
                f"quantile_over_time(0.95, sum(rate(container_cpu_usage_seconds_total"
                f'{{container!="",container!="POD"}}[5m])) by (namespace,pod)[{lookback}:])'
            ),
            "mem_avg": (
                f"avg_over_time(sum(container_memory_working_set_bytes"
                f'{{container!="",container!="POD"}}) by (namespace,pod)[{lookback}:])'
            ),
            "mem_p95": (
                f"quantile_over_time(0.95, sum(container_memory_working_set_bytes"
                f'{{container!="",container!="POD"}}) by (namespace,pod)[{lookback}:])'
            ),
        }

        result: Dict[str, Dict] = {}
        async with aiohttp.ClientSession() as session:
            for metric_name, query in queries.items():
                data = await self._prom_query(query, session)
                if not data:
                    continue
                for item in data:
                    ns  = item["metric"].get("namespace", "default")
                    pod = item["metric"].get("pod", "unknown")
                    key = f"{ns}/{pod}"
                    val = float(item["value"][1]) if item.get("value") else 0.0

                    # Convert bytes → GiB for memory metrics
                    if "mem" in metric_name:
                        val = val / (1024 ** 3)

                    result.setdefault(key, {})[metric_name] = round(val, 6)

        logger.info(f"Prometheus pod metrics: {len(result)} pods")
        return result

    # ── Combined ───────────────────────────────────────────────────────────────

    async def get_combined_metrics(self, node_names: List[str]) -> Dict[str, Any]:
        """
        Collect node and pod metrics from Prometheus.

        If Prometheus is unreachable the dicts will be empty; the cost
        analyzer will fall back to using K8s resource requests as a proxy.
        """
        node_metrics, pod_metrics = await asyncio.gather(
            self.get_node_metrics_prometheus(),
            self.get_pod_metrics_prometheus(),
        )

        missing = [n for n in node_names if n not in node_metrics]
        if missing:
            logger.warning(
                f"{len(missing)} node(s) have no Prometheus data — "
                "cost analyzer will use request-based utilization proxy for them"
            )

        return {
            "node_metrics": node_metrics,
            "pod_metrics": pod_metrics,
            "sources": {"prometheus": bool(node_metrics)},
        }
