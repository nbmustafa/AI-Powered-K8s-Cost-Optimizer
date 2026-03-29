"""
MetricsCollector — fetches historical metrics from Prometheus and/or CloudWatch.

Priority: Prometheus → CloudWatch → metrics-server (fallback).
Returns p50/p95/p99 CPU & memory over the configured lookback window.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import boto3
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


class MetricsCollector:
    def __init__(
        self,
        prometheus_url: Optional[str] = None,
        cloudwatch_region: str = "us-east-1",
    ):
        self.prometheus_url = prometheus_url
        self.cloudwatch_region = cloudwatch_region
        self._cw_client = None

    @property
    def cw(self):
        if self._cw_client is None:
            self._cw_client = boto3.client(
                "cloudwatch", region_name=self.cloudwatch_region
            )
        return self._cw_client

    # ── Prometheus ────────────────────────────────────────────────────────────

    async def _prom_query(
        self, query: str, session: aiohttp.ClientSession
    ) -> Optional[Any]:
        """Execute an instant PromQL query."""
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            async with session.get(url, params={"query": query}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("data", {}).get("result", [])
        except Exception as e:
            logger.warning(f"Prometheus query failed [{query[:60]}]: {e}")
            return None

    async def get_node_metrics_prometheus(self) -> Dict[str, Dict]:
        """
        Fetch per-node CPU & memory utilization from Prometheus.
        Returns:
            {node_name: {cpu_p50, cpu_p95, mem_p50, mem_p95, ...}}
        """
        if not self.prometheus_url:
            return {}

        lookback = f"{settings.metrics_lookback_hours}h"
        queries = {
            "cpu_p50": f'quantile_over_time(0.5, node:node_cpu_utilization:avg1m[{lookback}])',
            "cpu_p95": f'quantile_over_time(0.95, node:node_cpu_utilization:avg1m[{lookback}])',
            "mem_p50": f'quantile_over_time(0.5, node:node_memory_utilization:avg1m[{lookback}])',
            "mem_p95": f'quantile_over_time(0.95, node:node_memory_utilization:avg1m[{lookback}])',
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

        return result

    async def get_pod_metrics_prometheus(self) -> Dict[str, Dict]:
        """
        Fetch per-pod CPU & memory from Prometheus.
        Returns:
            {"namespace/pod": {cpu_avg, cpu_p95, mem_avg, mem_p95}}
        """
        if not self.prometheus_url:
            return {}

        lookback = f"{settings.metrics_lookback_hours}h"
        queries = {
            "cpu_avg": (
                f'avg_over_time(sum(rate(container_cpu_usage_seconds_total'
                f'{{container!="",container!="POD"}}[5m])) by (namespace,pod)[{lookback}:])'
            ),
            "cpu_p95": (
                f'quantile_over_time(0.95, sum(rate(container_cpu_usage_seconds_total'
                f'{{container!="",container!="POD"}}[5m])) by (namespace,pod)[{lookback}:])'
            ),
            "mem_avg": (
                f'avg_over_time(sum(container_memory_working_set_bytes'
                f'{{container!="",container!="POD"}}) by (namespace,pod)[{lookback}:])'
            ),
            "mem_p95": (
                f'quantile_over_time(0.95, sum(container_memory_working_set_bytes'
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
                    ns = item["metric"].get("namespace", "default")
                    pod = item["metric"].get("pod", "unknown")
                    key = f"{ns}/{pod}"
                    val = float(item["value"][1]) if item.get("value") else 0.0

                    # Convert bytes → GiB for memory metrics
                    if "mem" in metric_name:
                        val = val / (1024 ** 3)

                    result.setdefault(key, {})[metric_name] = round(val, 6)

        return result

    # ── CloudWatch (EKS Container Insights) ───────────────────────────────────

    async def get_node_metrics_cloudwatch(self, node_names: List[str]) -> Dict[str, Dict]:
        """
        Fetch node metrics from CloudWatch Container Insights.
        Fallback when Prometheus is unavailable.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._fetch_cw_node_metrics, node_names
        )

    def _fetch_cw_node_metrics(self, node_names: List[str]) -> Dict[str, Dict]:
        result = {}
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=settings.metrics_lookback_hours)

        for node in node_names:
            try:
                cpu_resp = self.cw.get_metric_statistics(
                    Namespace="ContainerInsights",
                    MetricName="node_cpu_utilization",
                    Dimensions=[
                        {"Name": "ClusterName", "Value": settings.cluster_name},
                        {"Name": "NodeName", "Value": node},
                    ],
                    StartTime=start,
                    EndTime=end,
                    Period=3600,
                    Statistics=["Average", "Maximum"],
                )
                mem_resp = self.cw.get_metric_statistics(
                    Namespace="ContainerInsights",
                    MetricName="node_memory_utilization",
                    Dimensions=[
                        {"Name": "ClusterName", "Value": settings.cluster_name},
                        {"Name": "NodeName", "Value": node},
                    ],
                    StartTime=start,
                    EndTime=end,
                    Period=3600,
                    Statistics=["Average", "Maximum"],
                )

                cpu_points = cpu_resp.get("Datapoints", [])
                mem_points = mem_resp.get("Datapoints", [])

                result[node] = {
                    "cpu_avg": round(
                        sum(p["Average"] for p in cpu_points) / max(len(cpu_points), 1) / 100, 4
                    ),
                    "cpu_p95": round(
                        max((p["Maximum"] for p in cpu_points), default=0) / 100, 4
                    ),
                    "mem_avg": round(
                        sum(p["Average"] for p in mem_points) / max(len(mem_points), 1) / 100, 4
                    ),
                    "mem_p95": round(
                        max((p["Maximum"] for p in mem_points), default=0) / 100, 4
                    ),
                    "source": "cloudwatch",
                }

            except ClientError as e:
                logger.warning(f"CloudWatch error for node {node}: {e}")

        return result

    # ── Combined ──────────────────────────────────────────────────────────────

    async def get_combined_metrics(
        self,
        node_names: List[str],
    ) -> Dict[str, Any]:
        """
        Return best-available metrics from Prometheus → CloudWatch fallback.
        """
        prom_nodes, prom_pods = await asyncio.gather(
            self.get_node_metrics_prometheus(),
            self.get_pod_metrics_prometheus(),
        )

        # Identify nodes missing from Prometheus data
        missing_nodes = [n for n in node_names if n not in prom_nodes]
        cw_nodes: Dict[str, Dict] = {}
        if missing_nodes:
            logger.info(
                f"{len(missing_nodes)} nodes missing from Prometheus, trying CloudWatch"
            )
            cw_nodes = await self.get_node_metrics_cloudwatch(missing_nodes)

        node_metrics = {**cw_nodes, **prom_nodes}

        return {
            "node_metrics": node_metrics,
            "pod_metrics": prom_pods,
            "sources": {
                "prometheus": bool(prom_nodes),
                "cloudwatch": bool(cw_nodes),
            },
        }
