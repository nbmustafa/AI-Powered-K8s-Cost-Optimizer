"""
Unit tests for EKS Cost Optimizer core modules.
Run with: pytest tests/ -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import asdict


# ── Test cost analyzer ────────────────────────────────────────────────────────

class TestNodeSizingLogic:
    """Test node right-sizing decision logic."""

    def setup_method(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.analyzers.cost_analyzer import CostAnalyzer
        self.analyzer = CostAnalyzer.__new__(CostAnalyzer)
        self.analyzer.region = "us-east-1"
        self.analyzer._pricing_cache = {}
        self.analyzer._pricing = None

    def _make_node(self, **kwargs):
        from app.collectors.k8s_collector import NodeInfo
        defaults = dict(
            name="test-node", instance_type="m5.4xlarge",
            region="us-east-1", zone="us-east-1a",
            capacity_cpu=16.0, capacity_memory_gi=64.0,
            allocatable_cpu=15.5, allocatable_memory_gi=60.0,
            requested_cpu=2.0, requested_memory_gi=8.0,
            limit_cpu=4.0, limit_memory_gi=16.0,
            pod_count=5, max_pods=110,
            conditions={"Ready": "True"}, labels={},
            node_group="general", age_days=30.0,
        )
        defaults.update(kwargs)
        return NodeInfo(**defaults)

    def test_idle_node_gets_terminate_action(self):
        node = self._make_node(pod_count=0)
        metrics = {"test-node": {"cpu_p95": 0.03, "mem_p95": 0.08}}
        result = self.analyzer._analyze_node(node, metrics)
        assert result.action == "terminate"
        assert result.savings_monthly > 0

    def test_underutilized_node_gets_downsize_action(self):
        node = self._make_node(pod_count=3)
        metrics = {"test-node": {"cpu_p95": 0.12, "mem_p95": 0.18}}
        result = self.analyzer._analyze_node(node, metrics)
        assert result.action == "downsize"
        assert result.savings_monthly > 0

    def test_overutilized_node_gets_upsize_action(self):
        node = self._make_node(pod_count=20)
        metrics = {"test-node": {"cpu_p95": 0.90, "mem_p95": 0.88}}
        result = self.analyzer._analyze_node(node, metrics)
        assert result.action == "upsize"
        assert result.savings_monthly <= 0  # Cost increases

    def test_well_utilized_node_gets_keep_action(self):
        node = self._make_node(pod_count=12)
        metrics = {"test-node": {"cpu_p95": 0.55, "mem_p95": 0.60}}
        result = self.analyzer._analyze_node(node, metrics)
        assert result.action == "keep"
        assert result.savings_monthly == 0

    def test_missing_metrics_falls_back_to_requests(self):
        node = self._make_node(pod_count=1, requested_cpu=1.0, allocatable_cpu=15.5)
        metrics = {}  # No metrics available
        result = self.analyzer._analyze_node(node, metrics)
        # Should not crash — uses requested_cpu / allocatable_cpu as fallback
        assert result.action in ("terminate", "downsize", "keep", "upsize")


class TestInstanceSizeRecommendations:
    def setup_method(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.analyzers.cost_analyzer import CostAnalyzer
        self.a = CostAnalyzer.__new__(CostAnalyzer)

    def test_recommend_smaller_from_4xlarge(self):
        assert self.a._recommend_smaller_instance("m5.4xlarge") == "m5.2xlarge"

    def test_recommend_smaller_from_large(self):
        assert self.a._recommend_smaller_instance("m5.large") == "m5.medium"

    def test_recommend_smaller_from_nano_returns_none(self):
        assert self.a._recommend_smaller_instance("t3.nano") is None

    def test_recommend_larger_from_xlarge(self):
        assert self.a._recommend_larger_instance("m5.xlarge") == "m5.2xlarge"

    def test_unknown_instance_returns_none(self):
        assert self.a._recommend_smaller_instance("g4dn.xlarge") is None


class TestPodSizingLogic:
    def setup_method(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.analyzers.cost_analyzer import CostAnalyzer
        self.analyzer = CostAnalyzer.__new__(CostAnalyzer)
        self.analyzer.region = "us-east-1"

    def _make_pod(self, **kwargs):
        from app.collectors.k8s_collector import PodInfo
        defaults = dict(
            name="test-pod-xyz", namespace="default",
            node_name="node-1", phase="Running",
            qos_class="Burstable", owner_kind="Deployment",
            owner_name="test-app", containers=[],
            request_cpu=4.0, request_memory_gi=8.0,
            limit_cpu=8.0, limit_memory_gi=16.0,
            age_days=30.0, restart_count=0,
        )
        defaults.update(kwargs)
        return PodInfo(**defaults)

    def test_over_provisioned_pod_gets_rightsize_action(self):
        pod = self._make_pod()
        metrics = {"default/test-pod-xyz": {"cpu_p95": 0.3, "mem_p95": 1.5}}
        result = self.analyzer._analyze_pod(pod, metrics)
        assert result is not None
        assert result.action == "rightsize"
        assert result.cpu_waste_pct > 0
        assert result.annual_savings_usd > 0

    def test_well_utilized_pod_gets_keep_action(self):
        pod = self._make_pod(request_cpu=1.0, request_memory_gi=2.0)
        metrics = {"default/test-pod-xyz": {"cpu_p95": 0.92, "mem_p95": 1.85}}
        result = self.analyzer._analyze_pod(pod, metrics)
        assert result is not None
        assert result.action == "keep"

    def test_non_running_pod_returns_none(self):
        pod = self._make_pod(phase="Pending")
        result = self.analyzer._analyze_pod(pod, {})
        assert result is None

    def test_besteffort_pod_with_zero_requests_returns_none(self):
        pod = self._make_pod(request_cpu=0.0, request_memory_gi=0.0)
        result = self.analyzer._analyze_pod(pod, {})
        assert result is None

    def test_recommended_values_include_headroom(self):
        pod = self._make_pod()
        metrics = {"default/test-pod-xyz": {"cpu_p95": 1.0, "mem_p95": 2.0}}
        result = self.analyzer._analyze_pod(pod, metrics)
        # Recommended should be > actual (headroom)
        assert result.recommended_request_cpu > 1.0
        assert result.recommended_request_mem_gi > 2.0


# ── Test K8s parsing helpers ──────────────────────────────────────────────────

class TestResourceParsing:
    def test_parse_cpu_millis(self):
        from app.collectors.k8s_collector import _parse_cpu
        assert _parse_cpu("500m") == 0.5
        assert _parse_cpu("250m") == 0.25
        assert _parse_cpu("1000m") == 1.0

    def test_parse_cpu_cores(self):
        from app.collectors.k8s_collector import _parse_cpu
        assert _parse_cpu("2") == 2.0
        assert _parse_cpu("0.5") == 0.5

    def test_parse_cpu_empty(self):
        from app.collectors.k8s_collector import _parse_cpu
        assert _parse_cpu("") == 0.0
        assert _parse_cpu(None) == 0.0

    def test_parse_memory_ki(self):
        from app.collectors.k8s_collector import _parse_memory
        result = _parse_memory("1048576Ki")  # 1 GiB
        assert abs(result - 1.0) < 0.01

    def test_parse_memory_mi(self):
        from app.collectors.k8s_collector import _parse_memory
        assert abs(_parse_memory("512Mi") - 0.5) < 0.01

    def test_parse_memory_gi(self):
        from app.collectors.k8s_collector import _parse_memory
        assert _parse_memory("4Gi") == 4.0

    def test_parse_memory_empty(self):
        from app.collectors.k8s_collector import _parse_memory
        assert _parse_memory("") == 0.0


# ── Test cache ────────────────────────────────────────────────────────────────

class TestMetricsCache:
    def setup_method(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.models.cache import MetricsCache
        self.cache = MetricsCache(ttl_seconds=60)

    def test_set_and_get(self):
        self.cache.set("key1", {"data": 42})
        result = self.cache.get("key1")
        assert result == {"data": 42}

    def test_missing_key_returns_none(self):
        assert self.cache.get("nonexistent") is None

    def test_expired_key_returns_none(self):
        import time
        self.cache.set("short", "value", ttl_override=0)
        time.sleep(0.01)
        assert self.cache.get("short") is None

    def test_invalidate_removes_key(self):
        self.cache.set("to_remove", "value")
        self.cache.invalidate("to_remove")
        assert self.cache.get("to_remove") is None

    def test_stats(self):
        self.cache.set("a", 1)
        self.cache.set("b", 2)
        stats = self.cache.stats()
        assert stats["total_keys"] == 2
        assert stats["live_keys"] == 2

    def test_clear(self):
        self.cache.set("x", 1)
        self.cache.clear()
        assert self.cache.get("x") is None


# ── Integration-style tests (mock K8s API) ────────────────────────────────────

class TestCostReportGeneration:
    """End-to-end report generation with mocked dependencies."""

    def setup_method(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    def _make_snapshot(self):
        from app.collectors.k8s_collector import ClusterSnapshot, NodeInfo, PodInfo
        nodes = [
            NodeInfo(
                name="node-1", instance_type="m5.xlarge",
                region="us-east-1", zone="us-east-1a",
                capacity_cpu=4.0, capacity_memory_gi=16.0,
                allocatable_cpu=3.8, allocatable_memory_gi=14.0,
                requested_cpu=0.5, requested_memory_gi=1.0,
                limit_cpu=1.0, limit_memory_gi=2.0,
                pod_count=2, max_pods=110,
                conditions={"Ready": "True"}, labels={},
                node_group="workers", age_days=90.0,
            )
        ]
        pods = [
            PodInfo(
                name="app-pod", namespace="production",
                node_name="node-1", phase="Running",
                qos_class="Burstable", owner_kind="Deployment",
                owner_name="app", containers=[],
                request_cpu=0.25, request_memory_gi=0.5,
                limit_cpu=0.5, limit_memory_gi=1.0,
                age_days=30.0, restart_count=0,
            )
        ]
        return ClusterSnapshot(
            cluster_name="test-cluster",
            nodes=nodes,
            pods=pods,
            hpas=[],
            namespaces=["production"],
            collected_at="2024-01-01T00:00:00Z",
        )

    def test_report_generates_without_error(self):
        from app.analyzers.cost_analyzer import CostAnalyzer
        analyzer = CostAnalyzer.__new__(CostAnalyzer)
        analyzer.region = "us-east-1"
        analyzer._pricing_cache = {}
        analyzer._pricing = None

        snapshot = self._make_snapshot()
        metrics = {
            "node_metrics": {"node-1": {"cpu_p95": 0.10, "mem_p95": 0.15}},
            "pod_metrics": {"production/app-pod": {"cpu_p95": 0.05, "mem_p95": 0.1}},
            "sources": {"prometheus": True, "cloudwatch": False},
        }

        report = analyzer.generate_report(snapshot, metrics)

        assert report.cluster_name == "test-cluster"
        assert report.total_nodes == 1
        assert report.total_pods == 1
        assert report.current_monthly_cost > 0
        assert len(report.node_recommendations) == 1
        assert report.node_recommendations[0].action in ("downsize", "terminate", "keep", "upsize")

    def test_savings_percentage_bounded(self):
        from app.analyzers.cost_analyzer import CostAnalyzer
        analyzer = CostAnalyzer.__new__(CostAnalyzer)
        analyzer.region = "us-east-1"
        analyzer._pricing_cache = {}
        analyzer._pricing = None

        snapshot = self._make_snapshot()
        metrics = {"node_metrics": {}, "pod_metrics": {}, "sources": {}}
        report = analyzer.generate_report(snapshot, metrics)

        assert 0 <= report.savings_percentage <= 100
