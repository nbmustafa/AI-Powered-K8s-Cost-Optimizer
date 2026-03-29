"""
CostAnalyzer — translates resource utilization into dollar cost impact.

Responsibilities:
  1. Fetch EC2 on-demand pricing from AWS Pricing API
  2. Calculate current cluster cost
  3. Identify waste (idle nodes, over-provisioned pods)
  4. Generate right-sizing proposals with projected savings
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from app.collectors.k8s_collector import NodeInfo, PodInfo, ClusterSnapshot
from app.config import settings

logger = logging.getLogger(__name__)


# ── EC2 pricing table (fallback when API unavailable) ─────────────────────────
# Prices in USD/hour, us-east-1, Linux, On-Demand
FALLBACK_PRICING: Dict[str, float] = {
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416,
    "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768, "m5.8xlarge": 1.536, "m5.12xlarge": 2.304,
    "m5.16xlarge": 3.072, "m5.24xlarge": 4.608,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768, "m6i.8xlarge": 1.536,
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68, "c5.9xlarge": 1.53, "c5.18xlarge": 3.06,
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008, "r5.8xlarge": 2.016,
    "r6i.large": 0.126, "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
}

# Recommended instance families for each workload type
COMPUTE_OPTIMIZED = ["c5", "c6i", "c6a"]
MEMORY_OPTIMIZED = ["r5", "r6i", "r6a"]
GENERAL_PURPOSE = ["m5", "m6i", "m6a", "t3"]


@dataclass
class NodeSizing:
    node_name: str
    current_instance: str
    current_cost_monthly: float
    current_cpu_util_pct: float
    current_mem_util_pct: float
    recommended_instance: Optional[str]
    recommended_cost_monthly: float
    savings_monthly: float
    savings_pct: float
    action: str           # "downsize" | "terminate" | "keep" | "upsize"
    reason: str
    confidence: str       # "high" | "medium" | "low"


@dataclass
class PodSizing:
    pod_name: str
    namespace: str
    owner_kind: str
    owner_name: str
    current_request_cpu: float
    current_limit_cpu: float
    current_request_mem_gi: float
    current_limit_mem_gi: float
    actual_cpu_p95: float
    actual_mem_p95_gi: float
    recommended_request_cpu: float
    recommended_limit_cpu: float
    recommended_request_mem_gi: float
    recommended_limit_mem_gi: float
    cpu_waste_pct: float
    mem_waste_pct: float
    annual_savings_usd: float
    action: str           # "rightsize" | "keep" | "evict"


@dataclass
class CostReport:
    cluster_name: str
    generated_at: str
    total_nodes: int
    total_pods: int
    current_monthly_cost: float
    optimized_monthly_cost: float
    potential_monthly_savings: float
    potential_annual_savings: float
    savings_percentage: float
    idle_nodes: List[str]
    node_recommendations: List[NodeSizing]
    pod_recommendations: List[PodSizing]
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class CostAnalyzer:
    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self._pricing_cache: Dict[str, float] = {}
        self._init_pricing_client()

    def _init_pricing_client(self):
        try:
            # Pricing API is only in us-east-1
            self._pricing = boto3.client("pricing", region_name="us-east-1")
        except Exception:
            self._pricing = None
            logger.warning("Could not initialize AWS Pricing client — using fallback prices")

    # ── Pricing ───────────────────────────────────────────────────────────────

    def get_instance_hourly_cost(self, instance_type: str) -> float:
        if instance_type in self._pricing_cache:
            return self._pricing_cache[instance_type]

        if self._pricing:
            try:
                resp = self._pricing.get_products(
                    ServiceCode="AmazonEC2",
                    Filters=[
                        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": self.region},
                        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    ],
                    MaxResults=1,
                )
                products = resp.get("PriceList", [])
                if products:
                    import json
                    p = json.loads(products[0])
                    od = p["terms"]["OnDemand"]
                    price_dim = next(iter(next(iter(od.values()))["priceDimensions"].values()))
                    price = float(price_dim["pricePerUnit"]["USD"])
                    self._pricing_cache[instance_type] = price
                    return price
            except (ClientError, StopIteration, KeyError, Exception) as e:
                logger.warning(f"Pricing API error for {instance_type}: {e}")

        # Fallback
        price = FALLBACK_PRICING.get(instance_type, settings.default_cpu_cost_per_core_hour * 4)
        self._pricing_cache[instance_type] = price
        return price

    def _monthly(self, hourly: float) -> float:
        return round(hourly * 24 * 30.44, 2)

    # ── Node analysis ─────────────────────────────────────────────────────────

    def _analyze_node(
        self,
        node: NodeInfo,
        node_metrics: Dict[str, Dict],
    ) -> NodeSizing:
        hourly_cost = self.get_instance_hourly_cost(node.instance_type)
        monthly_cost = self._monthly(hourly_cost)

        metrics = node_metrics.get(node.name, {})
        cpu_util = metrics.get("cpu_p95", metrics.get("cpu_avg",
            node.requested_cpu / max(node.allocatable_cpu, 0.001)
        ))
        mem_util = metrics.get("mem_p95", metrics.get("mem_avg",
            node.requested_memory_gi / max(node.allocatable_memory_gi, 0.001)
        ))

        cpu_pct = round(cpu_util * 100, 1)
        mem_pct = round(mem_util * 100, 1)

        # Decision logic
        if cpu_pct < 10 and mem_pct < 15 and node.pod_count == 0:
            action = "terminate"
            reason = f"Node is fully idle (CPU {cpu_pct}%, MEM {mem_pct}%, {node.pod_count} pods)"
            recommended = None
            rec_monthly = 0.0
            confidence = "high"
        elif cpu_pct < 20 and mem_pct < 30:
            action = "downsize"
            recommended = self._recommend_smaller_instance(node.instance_type)
            rec_monthly = self._monthly(self.get_instance_hourly_cost(recommended)) if recommended else monthly_cost
            reason = f"Under-utilized (CPU p95={cpu_pct}%, MEM p95={mem_pct}%). Downsize to {recommended}."
            confidence = "high" if cpu_pct < 15 else "medium"
        elif cpu_pct > 85 or mem_pct > 85:
            action = "upsize"
            recommended = self._recommend_larger_instance(node.instance_type)
            rec_monthly = self._monthly(self.get_instance_hourly_cost(recommended)) if recommended else monthly_cost * 1.5
            reason = f"Over-utilized (CPU p95={cpu_pct}%, MEM p95={mem_pct}%). Risk of resource pressure."
            confidence = "high"
        else:
            action = "keep"
            recommended = node.instance_type
            rec_monthly = monthly_cost
            reason = f"Well-utilized (CPU p95={cpu_pct}%, MEM p95={mem_pct}%)."
            confidence = "high"

        savings = round(monthly_cost - rec_monthly, 2)
        savings_pct = round((savings / max(monthly_cost, 0.01)) * 100, 1)

        return NodeSizing(
            node_name=node.name,
            current_instance=node.instance_type,
            current_cost_monthly=monthly_cost,
            current_cpu_util_pct=cpu_pct,
            current_mem_util_pct=mem_pct,
            recommended_instance=recommended,
            recommended_cost_monthly=rec_monthly,
            savings_monthly=max(savings, 0),
            savings_pct=max(savings_pct, 0),
            action=action,
            reason=reason,
            confidence=confidence,
        )

    def _recommend_smaller_instance(self, instance_type: str) -> Optional[str]:
        """Return the next smaller instance type in the same family."""
        SIZE_ORDER = ["nano", "micro", "small", "medium", "large", "xlarge",
                      "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge"]
        parts = instance_type.split(".")
        if len(parts) != 2:
            return None
        family, size = parts
        try:
            idx = SIZE_ORDER.index(size)
            if idx == 0:
                return None
            return f"{family}.{SIZE_ORDER[idx - 1]}"
        except ValueError:
            return None

    def _recommend_larger_instance(self, instance_type: str) -> Optional[str]:
        SIZE_ORDER = ["nano", "micro", "small", "medium", "large", "xlarge",
                      "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge"]
        parts = instance_type.split(".")
        if len(parts) != 2:
            return None
        family, size = parts
        try:
            idx = SIZE_ORDER.index(size)
            if idx >= len(SIZE_ORDER) - 1:
                return None
            return f"{family}.{SIZE_ORDER[idx + 1]}"
        except ValueError:
            return None

    # ── Pod analysis ──────────────────────────────────────────────────────────

    def _analyze_pod(
        self,
        pod: PodInfo,
        pod_metrics: Dict[str, Dict],
    ) -> Optional[PodSizing]:
        if pod.phase != "Running":
            return None
        if pod.request_cpu == 0 and pod.request_memory_gi == 0:
            return None  # BestEffort — no requests set

        key = f"{pod.namespace}/{pod.name}"
        metrics = pod_metrics.get(key, {})

        actual_cpu = metrics.get("cpu_p95", metrics.get("cpu_avg", pod.request_cpu * 0.3))
        actual_mem = metrics.get("mem_p95", metrics.get("mem_avg", pod.request_memory_gi * 0.5))

        # Right-size with headroom buffer (25% overhead for CPU, 20% for memory)
        rec_cpu_req = round(max(actual_cpu * 1.25, 0.01), 3)
        rec_mem_req = round(max(actual_mem * 1.20, 0.064), 3)  # min 64MiB

        # Limit = 2× request for burstable, or same as request for guaranteed
        rec_cpu_lim = round(rec_cpu_req * 2, 3) if pod.qos_class != "Guaranteed" else rec_cpu_req
        rec_mem_lim = round(rec_mem_req * 1.5, 3)

        cpu_waste = max(pod.request_cpu - rec_cpu_req, 0)
        mem_waste = max(pod.request_memory_gi - rec_mem_req, 0)

        cpu_waste_pct = round((cpu_waste / max(pod.request_cpu, 0.001)) * 100, 1)
        mem_waste_pct = round((mem_waste / max(pod.request_memory_gi, 0.001)) * 100, 1)

        # Annualized savings
        annual_cpu_savings = cpu_waste * settings.default_cpu_cost_per_core_hour * 8760
        annual_mem_savings = mem_waste * settings.default_mem_cost_per_gb_hour * 8760
        annual_savings = round(annual_cpu_savings + annual_mem_savings, 2)

        if cpu_waste_pct < 5 and mem_waste_pct < 10:
            action = "keep"
        else:
            action = "rightsize"

        return PodSizing(
            pod_name=pod.name,
            namespace=pod.namespace,
            owner_kind=pod.owner_kind,
            owner_name=pod.owner_name,
            current_request_cpu=pod.request_cpu,
            current_limit_cpu=pod.limit_cpu,
            current_request_mem_gi=pod.request_memory_gi,
            current_limit_mem_gi=pod.limit_memory_gi,
            actual_cpu_p95=round(actual_cpu, 4),
            actual_mem_p95_gi=round(actual_mem, 4),
            recommended_request_cpu=rec_cpu_req,
            recommended_limit_cpu=rec_cpu_lim,
            recommended_request_mem_gi=rec_mem_req,
            recommended_limit_mem_gi=rec_mem_lim,
            cpu_waste_pct=cpu_waste_pct,
            mem_waste_pct=mem_waste_pct,
            annual_savings_usd=annual_savings,
            action=action,
        )

    # ── Full report ───────────────────────────────────────────────────────────

    def generate_report(
        self,
        snapshot: ClusterSnapshot,
        combined_metrics: Dict[str, Any],
    ) -> CostReport:
        from datetime import datetime, timezone

        node_metrics = combined_metrics.get("node_metrics", {})
        pod_metrics = combined_metrics.get("pod_metrics", {})

        node_recs = [
            self._analyze_node(n, node_metrics)
            for n in snapshot.nodes
        ]

        pod_recs_raw = [
            self._analyze_pod(p, pod_metrics)
            for p in snapshot.pods
        ]
        pod_recs = [r for r in pod_recs_raw if r is not None]

        total_current = sum(r.current_cost_monthly for r in node_recs)
        total_optimized = sum(r.recommended_cost_monthly for r in node_recs)
        monthly_savings = round(total_current - total_optimized, 2)
        annual_savings = round(monthly_savings * 12, 2)
        pod_annual_savings = sum(r.annual_savings_usd for r in pod_recs)
        savings_pct = round((monthly_savings / max(total_current, 0.01)) * 100, 1)

        idle_nodes = [r.node_name for r in node_recs if r.action == "terminate"]
        over_provisioned_pods = [r for r in pod_recs if r.action == "rightsize"]

        summary = {
            "nodes_to_terminate": len(idle_nodes),
            "nodes_to_downsize": sum(1 for r in node_recs if r.action == "downsize"),
            "nodes_to_upsize": sum(1 for r in node_recs if r.action == "upsize"),
            "pods_to_rightsize": len(over_provisioned_pods),
            "pod_cpu_waste_cores": round(
                sum(r.current_request_cpu - r.recommended_request_cpu for r in over_provisioned_pods), 2
            ),
            "pod_mem_waste_gi": round(
                sum(r.current_request_mem_gi - r.recommended_request_mem_gi for r in over_provisioned_pods), 2
            ),
            "total_pod_annual_savings": round(pod_annual_savings, 2),
            "metrics_sources": combined_metrics.get("sources", {}),
        }

        return CostReport(
            cluster_name=snapshot.cluster_name,
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_nodes=len(snapshot.nodes),
            total_pods=len(snapshot.pods),
            current_monthly_cost=round(total_current, 2),
            optimized_monthly_cost=round(total_optimized, 2),
            potential_monthly_savings=monthly_savings,
            potential_annual_savings=annual_savings,
            savings_percentage=savings_pct,
            idle_nodes=idle_nodes,
            node_recommendations=node_recs,
            pod_recommendations=pod_recs,
            summary=summary,
        )
