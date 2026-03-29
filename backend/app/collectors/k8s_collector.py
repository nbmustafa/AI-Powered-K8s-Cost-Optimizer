"""
K8sCollector — pulls raw resource data from the Kubernetes API.

Collects:
  • Nodes (capacity, allocatable, conditions, labels)
  • Pods (requests, limits, phase, owner references)
  • Namespaces
  • HorizontalPodAutoscalers
  • VerticalPodAutoscalers (if installed)
  • PodDisruptionBudgets
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class NodeInfo:
    name: str
    instance_type: str
    region: str
    zone: str
    capacity_cpu: float         # cores
    capacity_memory_gi: float   # GiB
    allocatable_cpu: float
    allocatable_memory_gi: float
    requested_cpu: float = 0.0
    requested_memory_gi: float = 0.0
    limit_cpu: float = 0.0
    limit_memory_gi: float = 0.0
    pod_count: int = 0
    max_pods: int = 110
    conditions: Dict[str, str] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    node_group: Optional[str] = None
    age_days: float = 0.0


@dataclass
class PodInfo:
    name: str
    namespace: str
    node_name: str
    phase: str
    qos_class: str
    owner_kind: str
    owner_name: str
    containers: List[Dict[str, Any]] = field(default_factory=list)
    # Aggregated across all containers
    request_cpu: float = 0.0    # cores
    request_memory_gi: float = 0.0
    limit_cpu: float = 0.0
    limit_memory_gi: float = 0.0
    age_days: float = 0.0
    restart_count: int = 0


@dataclass
class HPAInfo:
    name: str
    namespace: str
    target_kind: str
    target_name: str
    min_replicas: int
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    metrics: List[Dict] = field(default_factory=list)


@dataclass
class ClusterSnapshot:
    cluster_name: str
    nodes: List[NodeInfo] = field(default_factory=list)
    pods: List[PodInfo] = field(default_factory=list)
    hpas: List[HPAInfo] = field(default_factory=list)
    namespaces: List[str] = field(default_factory=list)
    collected_at: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_cpu(value: str) -> float:
    """Convert k8s CPU string to float cores."""
    if not value:
        return 0.0
    if value.endswith("m"):
        return int(value[:-1]) / 1000.0
    return float(value)


def _parse_memory(value: str) -> float:
    """Convert k8s memory string to GiB."""
    if not value:
        return 0.0
    units = {"Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1, "Ti": 1024,
             "K": 1 / (1000 ** 2), "M": 1 / 1000, "G": 1, "T": 1000}
    for unit, factor in units.items():
        if value.endswith(unit):
            return float(value[: -len(unit)]) * factor
    return float(value) / (1024 ** 3)  # bytes → GiB


def _age_days(creation_timestamp) -> float:
    from datetime import datetime, timezone
    if not creation_timestamp:
        return 0.0
    now = datetime.now(timezone.utc)
    return (now - creation_timestamp).total_seconds() / 86400


# ── Collector ─────────────────────────────────────────────────────────────────

class K8sCollector:
    def __init__(self, kubeconfig_path: Optional[str] = None):
        self._load_config(kubeconfig_path)
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._autoscaling = client.AutoscalingV2Api()
        self._metrics_api = client.CustomObjectsApi()

    def _load_config(self, kubeconfig_path: Optional[str]):
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            config.load_kube_config(config_file=kubeconfig_path)
            logger.info(f"Loaded kubeconfig from {kubeconfig_path or '~/.kube/config'}")

    # ── Node collection ───────────────────────────────────────────────────────

    async def collect_nodes(self) -> List[NodeInfo]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._collect_nodes_sync)

    def _collect_nodes_sync(self) -> List[NodeInfo]:
        nodes = []
        try:
            node_list = self._core.list_node()
        except ApiException as e:
            logger.error(f"Failed to list nodes: {e}")
            return nodes

        for n in node_list.items:
            labels = n.metadata.labels or {}
            capacity = n.status.capacity or {}
            allocatable = n.status.allocatable or {}
            conditions = {
                c.type: c.status
                for c in (n.status.conditions or [])
            }

            node_info = NodeInfo(
                name=n.metadata.name,
                instance_type=labels.get("node.kubernetes.io/instance-type", "unknown"),
                region=labels.get("topology.kubernetes.io/region", "unknown"),
                zone=labels.get("topology.kubernetes.io/zone", "unknown"),
                capacity_cpu=_parse_cpu(capacity.get("cpu", "0")),
                capacity_memory_gi=_parse_memory(capacity.get("memory", "0")),
                allocatable_cpu=_parse_cpu(allocatable.get("cpu", "0")),
                allocatable_memory_gi=_parse_memory(allocatable.get("memory", "0")),
                max_pods=int(capacity.get("pods", "110")),
                conditions=conditions,
                labels=labels,
                node_group=labels.get(
                    "eks.amazonaws.com/nodegroup",
                    labels.get("karpenter.sh/provisioner-name", None),
                ),
                age_days=_age_days(n.metadata.creation_timestamp),
            )
            nodes.append(node_info)

        return nodes

    # ── Pod collection ────────────────────────────────────────────────────────

    async def collect_pods(self) -> List[PodInfo]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._collect_pods_sync)

    def _collect_pods_sync(self) -> List[PodInfo]:
        pods = []
        try:
            pod_list = self._core.list_pod_for_all_namespaces()
        except ApiException as e:
            logger.error(f"Failed to list pods: {e}")
            return pods

        for p in pod_list.items:
            owner_kind, owner_name = "None", "None"
            owners = p.metadata.owner_references or []
            if owners:
                owner_kind = owners[0].kind
                owner_name = owners[0].name

            containers_data = []
            total_req_cpu = total_req_mem = total_lim_cpu = total_lim_mem = 0.0
            total_restarts = 0

            for c in (p.spec.containers or []):
                res = c.resources or client.V1ResourceRequirements()
                req = res.requests or {}
                lim = res.limits or {}

                req_cpu = _parse_cpu(req.get("cpu", "0"))
                req_mem = _parse_memory(req.get("memory", "0"))
                lim_cpu = _parse_cpu(lim.get("cpu", "0"))
                lim_mem = _parse_memory(lim.get("memory", "0"))

                total_req_cpu += req_cpu
                total_req_mem += req_mem
                total_lim_cpu += lim_cpu
                total_lim_mem += lim_mem

                containers_data.append({
                    "name": c.name,
                    "image": c.image,
                    "request_cpu": round(req_cpu, 4),
                    "request_memory_gi": round(req_mem, 4),
                    "limit_cpu": round(lim_cpu, 4),
                    "limit_memory_gi": round(lim_mem, 4),
                })

            # Count restarts
            for cs in (p.status.container_statuses or []):
                total_restarts += cs.restart_count or 0

            pods.append(PodInfo(
                name=p.metadata.name,
                namespace=p.metadata.namespace,
                node_name=p.spec.node_name or "unscheduled",
                phase=p.status.phase or "Unknown",
                qos_class=p.status.qos_class or "BestEffort",
                owner_kind=owner_kind,
                owner_name=owner_name,
                containers=containers_data,
                request_cpu=round(total_req_cpu, 4),
                request_memory_gi=round(total_req_mem, 4),
                limit_cpu=round(total_lim_cpu, 4),
                limit_memory_gi=round(total_lim_mem, 4),
                age_days=_age_days(p.metadata.creation_timestamp),
                restart_count=total_restarts,
            ))

        return pods

    # ── HPA collection ────────────────────────────────────────────────────────

    async def collect_hpas(self) -> List[HPAInfo]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._collect_hpas_sync)

    def _collect_hpas_sync(self) -> List[HPAInfo]:
        hpas = []
        try:
            hpa_list = self._autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces()
        except ApiException as e:
            logger.warning(f"Failed to list HPAs: {e}")
            return hpas

        for h in hpa_list.items:
            spec = h.spec
            status = h.status
            hpas.append(HPAInfo(
                name=h.metadata.name,
                namespace=h.metadata.namespace,
                target_kind=spec.scale_target_ref.kind,
                target_name=spec.scale_target_ref.name,
                min_replicas=spec.min_replicas or 1,
                max_replicas=spec.max_replicas,
                current_replicas=status.current_replicas or 0,
                desired_replicas=status.desired_replicas or 0,
            ))

        return hpas

    # ── Metrics server (top nodes/pods) ───────────────────────────────────────

    async def collect_metrics_server_data(self) -> Dict:
        """Collect live CPU/memory usage from metrics-server if available."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._collect_metrics_server_sync)

    def _collect_metrics_server_sync(self) -> Dict:
        try:
            node_metrics = self._metrics_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes",
            )
            pod_metrics = self._metrics_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
            )
            return {
                "node_usage": {
                    item["metadata"]["name"]: {
                        "cpu": _parse_cpu(item["usage"]["cpu"]),
                        "memory_gi": _parse_memory(item["usage"]["memory"]),
                    }
                    for item in node_metrics.get("items", [])
                },
                "pod_usage": {
                    f"{item['metadata']['namespace']}/{item['metadata']['name']}": {
                        "cpu": sum(
                            _parse_cpu(c["usage"]["cpu"])
                            for c in item.get("containers", [])
                        ),
                        "memory_gi": sum(
                            _parse_memory(c["usage"]["memory"])
                            for c in item.get("containers", [])
                        ),
                    }
                    for item in pod_metrics.get("items", [])
                },
            }
        except ApiException as e:
            logger.warning(f"metrics-server unavailable: {e.status}")
            return {"node_usage": {}, "pod_usage": {}}

    # ── Aggregate all ─────────────────────────────────────────────────────────

    async def collect_all(self) -> ClusterSnapshot:
        from datetime import datetime, timezone
        from app.config import settings

        nodes, pods, hpas, metrics = await asyncio.gather(
            self.collect_nodes(),
            self.collect_pods(),
            self.collect_hpas(),
            self.collect_metrics_server_data(),
        )

        # Enrich nodes with pod aggregated requests
        node_map: Dict[str, NodeInfo] = {n.name: n for n in nodes}
        for pod in pods:
            if pod.phase != "Running":
                continue
            n = node_map.get(pod.node_name)
            if n:
                n.requested_cpu += pod.request_cpu
                n.requested_memory_gi += pod.request_memory_gi
                n.limit_cpu += pod.limit_cpu
                n.limit_memory_gi += pod.limit_memory_gi
                n.pod_count += 1

        # Attach live usage from metrics-server to nodes
        node_usage = metrics.get("node_usage", {})
        for node in nodes:
            usage = node_usage.get(node.name, {})
            node.labels["_actual_cpu"] = str(round(usage.get("cpu", 0), 4))
            node.labels["_actual_memory_gi"] = str(round(usage.get("memory_gi", 0), 4))

        # Attach live usage to pods
        pod_usage = metrics.get("pod_usage", {})
        for pod in pods:
            key = f"{pod.namespace}/{pod.name}"
            usage = pod_usage.get(key, {})
            # Store in a synthetic label for easy access
            pod.containers.append({
                "__actual_cpu": round(usage.get("cpu", 0), 4),
                "__actual_memory_gi": round(usage.get("memory_gi", 0), 4),
            })

        namespaces_raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: [
                ns.metadata.name
                for ns in self._core.list_namespace().items
            ],
        )

        return ClusterSnapshot(
            cluster_name=settings.cluster_name,
            nodes=nodes,
            pods=pods,
            hpas=hpas,
            namespaces=namespaces_raw,
            collected_at=datetime.now(timezone.utc).isoformat(),
        )
