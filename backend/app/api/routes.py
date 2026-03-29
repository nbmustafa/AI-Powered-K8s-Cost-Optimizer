"""
API routes — RESTful endpoints for the EKS Cost Optimizer.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


def get_services(request: Request):
    return {
        "k8s": request.app.state.k8s_collector,
        "metrics": request.app.state.metrics_collector,
        "cost": request.app.state.cost_analyzer,
        "ai": request.app.state.ai_advisor,
        "cache": request.app.state.cache,
    }


# ── Request/Response schemas ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    history: Optional[List[Dict]] = []


class CommandsRequest(BaseModel):
    action_type: str = "all"   # "all" | "drain_idle_nodes" | "patch_pod_resources"


class AutoscalerRequest(BaseModel):
    tool: str = "karpenter"    # "karpenter" | "hpa" | "vpa"


# ── Cluster snapshot ──────────────────────────────────────────────────────────

@router.get("/snapshot", tags=["cluster"])
async def get_snapshot(
    force_refresh: bool = Query(False, description="Bypass cache"),
    svc=Depends(get_services),
):
    """Return raw K8s resource snapshot (nodes, pods, HPAs)."""
    cache = svc["cache"]
    if not force_refresh:
        cached = cache.get("cluster_snapshot")
        if cached:
            return {"source": "cache", "data": cached.to_dict()}

    snapshot = await svc["k8s"].collect_all()
    cache.set("cluster_snapshot", snapshot)
    return {"source": "live", "data": snapshot.to_dict()}


# ── Cost report ───────────────────────────────────────────────────────────────

@router.get("/cost-report", tags=["cost"])
async def get_cost_report(
    force_refresh: bool = Query(False),
    svc=Depends(get_services),
):
    """
    Full cost analysis: node waste, pod over-provisioning, savings opportunities.
    """
    cache = svc["cache"]
    if not force_refresh:
        cached = cache.get("cost_report")
        if cached:
            return {"source": "cache", "data": cached}

    # 1. Collect K8s state
    snapshot = cache.get("cluster_snapshot")
    if not snapshot or force_refresh:
        snapshot = await svc["k8s"].collect_all()
        cache.set("cluster_snapshot", snapshot)

    # 2. Collect metrics
    node_names = [n.name for n in snapshot.nodes]
    combined_metrics = await svc["metrics"].get_combined_metrics(node_names)

    # 3. Generate cost report
    report = svc["cost"].generate_report(snapshot, combined_metrics)
    report_dict = report.to_dict()
    cache.set("cost_report", report_dict)

    return {"source": "live", "data": report_dict}


# ── AI analysis ───────────────────────────────────────────────────────────────

@router.get("/ai/analyze", tags=["ai"])
async def ai_analyze(
    focus: Optional[str] = Query(None, description="quick_wins|nodes|pods|risk"),
    force_refresh: bool = Query(False),
    svc=Depends(get_services),
):
    """Full AI analysis of cost report."""
    cache_key = f"ai_analysis_{focus}"
    if not force_refresh:
        cached = cache.get(cache_key) if (cache := svc["cache"]) else None
        if cached:
            return {"source": "cache", "analysis": cached}

    # Get or generate report
    report_data = svc["cache"].get("cost_report")
    if not report_data or force_refresh:
        resp = await get_cost_report(force_refresh=force_refresh, svc=svc)
        report_data = resp["data"]

    from app.analyzers.cost_analyzer import CostReport
    from dataclasses import fields

    # Reconstruct CostReport object for AI advisor
    report = _dict_to_report(report_data)

    analysis = await svc["ai"].analyze_cost_report(report, focus=focus)
    svc["cache"].set(cache_key, analysis, ttl_override=600)
    return {"source": "live", "analysis": analysis}


@router.get("/ai/stream", tags=["ai"])
async def ai_stream(
    focus: Optional[str] = Query(None),
    svc=Depends(get_services),
):
    """Stream AI analysis via Server-Sent Events."""
    report_data = svc["cache"].get("cost_report")
    if not report_data:
        resp = await get_cost_report(svc=svc)
        report_data = resp["data"]

    report = _dict_to_report(report_data)

    async def event_stream():
        try:
            async for chunk in svc["ai"].stream_analysis(report, focus=focus):
                yield f"data: {chunk}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: [ERROR] {str(e)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/ai/chat", tags=["ai"])
async def ai_chat(
    req: ChatRequest,
    svc=Depends(get_services),
):
    """Q&A about cost report."""
    report_data = svc["cache"].get("cost_report")
    if not report_data:
        resp = await get_cost_report(svc=svc)
        report_data = resp["data"]

    report = _dict_to_report(report_data)
    answer = await svc["ai"].chat(req.question, report, req.history)
    return {"answer": answer}


@router.post("/ai/commands", tags=["ai"])
async def generate_commands(
    req: CommandsRequest,
    svc=Depends(get_services),
):
    """Generate remediation commands (kubectl/eksctl/aws)."""
    report_data = svc["cache"].get("cost_report")
    if not report_data:
        resp = await get_cost_report(svc=svc)
        report_data = resp["data"]

    report = _dict_to_report(report_data)
    commands = await svc["ai"].generate_remediation_commands(report, req.action_type)
    return {"commands": commands}


@router.post("/ai/autoscaler-config", tags=["ai"])
async def generate_autoscaler_config(
    req: AutoscalerRequest,
    svc=Depends(get_services),
):
    """Generate Karpenter/HPA/VPA YAML configs."""
    snapshot = svc["cache"].get("cluster_snapshot")
    if not snapshot:
        snapshot = await svc["k8s"].collect_all()
    summary = {
        "cluster_name": snapshot.cluster_name,
        "node_count": len(snapshot.nodes),
        "namespaces": snapshot.namespaces,
        "instance_types": list({n.instance_type for n in snapshot.nodes}),
        "node_groups": list({n.node_group for n in snapshot.nodes if n.node_group}),
    }
    config = await svc["ai"].generate_autoscaler_config(summary, req.tool)
    return {"config": config, "tool": req.tool}


# ── Nodes & Pods endpoints ────────────────────────────────────────────────────

@router.get("/nodes", tags=["cluster"])
async def get_nodes(svc=Depends(get_services)):
    snapshot = svc["cache"].get("cluster_snapshot")
    if not snapshot:
        snapshot = await svc["k8s"].collect_all()
    return {"nodes": [vars(n) for n in snapshot.nodes]}


@router.get("/pods", tags=["cluster"])
async def get_pods(
    namespace: Optional[str] = None,
    svc=Depends(get_services),
):
    snapshot = svc["cache"].get("cluster_snapshot")
    if not snapshot:
        snapshot = await svc["k8s"].collect_all()
    pods = snapshot.pods
    if namespace:
        pods = [p for p in pods if p.namespace == namespace]
    return {"pods": [vars(p) for p in pods], "total": len(pods)}


# ── Helper ────────────────────────────────────────────────────────────────────

def _dict_to_report(data: Dict) -> "CostReport":
    """Reconstruct CostReport from a dict (after JSON round-trip)."""
    from app.analyzers.cost_analyzer import CostReport, NodeSizing, PodSizing

    node_recs = [NodeSizing(**n) for n in data.get("node_recommendations", [])]
    pod_recs = [PodSizing(**p) for p in data.get("pod_recommendations", [])]

    return CostReport(
        cluster_name=data["cluster_name"],
        generated_at=data["generated_at"],
        total_nodes=data["total_nodes"],
        total_pods=data["total_pods"],
        current_monthly_cost=data["current_monthly_cost"],
        optimized_monthly_cost=data["optimized_monthly_cost"],
        potential_monthly_savings=data["potential_monthly_savings"],
        potential_annual_savings=data["potential_annual_savings"],
        savings_percentage=data["savings_percentage"],
        idle_nodes=data["idle_nodes"],
        node_recommendations=node_recs,
        pod_recommendations=pod_recs,
        summary=data.get("summary", {}),
    )
