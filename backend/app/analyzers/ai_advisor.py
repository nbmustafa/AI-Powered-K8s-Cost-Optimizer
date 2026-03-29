"""
AIAdvisor — Claude-powered natural language cost optimization advisor.

Capabilities:
  1. Analyze cost report and generate prioritized action plan
  2. Answer natural language questions about cluster cost
  3. Generate kubectl/eksctl remediation commands
  4. Evaluate workload-specific optimization strategies
  5. Explain HPA/VPA/Karpenter configuration recommendations
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import anthropic

from app.config import settings
from app.analyzers.cost_analyzer import CostReport

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Principal Platform/Site Reliability Engineer specializing in 
Kubernetes cost optimization on Amazon EKS. You have deep expertise in:

- Kubernetes resource management (requests, limits, QoS classes, LimitRanges, ResourceQuotas)
- EKS node groups, managed node groups, Karpenter, Cluster Autoscaler
- AWS EC2 instance family selection and right-sizing
- Prometheus metrics, CloudWatch Container Insights
- HorizontalPodAutoscaler, VerticalPodAutoscaler, KEDA
- Cost allocation, chargeback, showback strategies
- FinOps best practices for Kubernetes workloads

When analyzing cost data:
1. Be specific and actionable — provide exact recommended values (CPU/memory numbers)
2. Prioritize recommendations by ROI (highest savings first)
3. Flag risk — note workloads that must not be disrupted
4. Generate ready-to-run kubectl/eksctl commands when applicable
5. Explain the "why" behind each recommendation
6. Consider workload patterns (batch vs always-on, stateful vs stateless)
7. Account for HPA behavior when recommending resource adjustments

Always format responses clearly with sections, and when giving commands use code blocks.
Be concise but thorough. Avoid generic advice — use the actual data provided."""


class AIAdvisor:
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = settings.anthropic_model

    # ── Core analysis ─────────────────────────────────────────────────────────

    async def analyze_cost_report(
        self,
        report: CostReport,
        focus: Optional[str] = None,
    ) -> str:
        """
        Generate a comprehensive AI analysis of the cost report.
        focus: optional — "nodes" | "pods" | "quick_wins" | "risk"
        """
        report_dict = report.to_dict()

        # Trim large pod list for token efficiency
        pod_sample = report_dict.get("pod_recommendations", [])[:30]
        report_dict["pod_recommendations"] = pod_sample
        report_dict["_pod_sample_note"] = f"Showing top 30 of {report.total_pods} pods by waste"

        focus_instruction = ""
        if focus == "quick_wins":
            focus_instruction = "\n\nFOCUS: Identify the top 5 quick-win actions (highest ROI, lowest risk, executable today)."
        elif focus == "nodes":
            focus_instruction = "\n\nFOCUS: Deep-dive node-level analysis — idle nodes, downsizing, instance family changes."
        elif focus == "pods":
            focus_instruction = "\n\nFOCUS: Deep-dive workload right-sizing — resource requests/limits optimization, VPA recommendations."
        elif focus == "risk":
            focus_instruction = "\n\nFOCUS: Risk assessment — which optimizations could cause instability and how to mitigate them."

        prompt = f"""Analyze this EKS cluster cost report and provide a comprehensive optimization plan.{focus_instruction}

COST REPORT:
```json
{json.dumps(report_dict, indent=2, default=str)}
```

Provide your analysis with these sections:
1. **Executive Summary** — key numbers, urgency, total opportunity
2. **Top Priority Actions** — ordered by monthly savings impact
3. **Node Optimization Plan** — specific nodes to act on with commands
4. **Workload Right-sizing Plan** — top pods/deployments with exact new values
5. **Automation Recommendations** — HPA/VPA/Karpenter configs to prevent recurrence
6. **Risk & Rollback Plan** — what to watch and how to revert safely
"""

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=settings.ai_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text

    # ── Streaming analysis ────────────────────────────────────────────────────

    async def stream_analysis(
        self,
        report: CostReport,
        focus: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Stream the AI analysis for real-time UI updates."""
        report_dict = report.to_dict()
        pod_sample = report_dict.get("pod_recommendations", [])[:25]
        report_dict["pod_recommendations"] = pod_sample

        focus_instruction = ""
        if focus:
            focus_map = {
                "quick_wins": "FOCUS: Top 5 quick-win actions with highest ROI and lowest risk.",
                "nodes": "FOCUS: Node-level analysis — idle nodes, downsizing, instance family changes.",
                "pods": "FOCUS: Workload right-sizing — resource requests/limits, VPA recommendations.",
                "risk": "FOCUS: Risk assessment — what could break and mitigation strategies.",
            }
            focus_instruction = f"\n\n{focus_map.get(focus, '')}"

        prompt = f"""Analyze this EKS cost report.{focus_instruction}

REPORT:
```json
{json.dumps(report_dict, indent=2, default=str)}
```

Provide actionable analysis with: Executive Summary, Priority Actions, Node Plan, Pod Right-sizing, Automation, Risk/Rollback."""

        async with self._client.messages.stream(
            model=self.model,
            max_tokens=settings.ai_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    # ── Q&A chat ──────────────────────────────────────────────────────────────

    async def chat(
        self,
        question: str,
        report: CostReport,
        history: Optional[List[Dict]] = None,
    ) -> str:
        """Answer a natural-language question about the cost report."""
        context = json.dumps(
            {
                "cluster": report.cluster_name,
                "monthly_cost": report.current_monthly_cost,
                "savings_opportunity": report.potential_monthly_savings,
                "nodes": report.total_nodes,
                "idle_nodes": report.idle_nodes,
                "summary": report.summary,
                "top_node_recs": [
                    {k: v for k, v in vars(r).items()}
                    for r in report.node_recommendations[:10]
                ],
                "top_pod_recs": [
                    {k: v for k, v in vars(r).items()}
                    for r in sorted(
                        report.pod_recommendations,
                        key=lambda x: x.annual_savings_usd,
                        reverse=True,
                    )[:15]
                ],
            },
            default=str,
        )

        messages = []
        if history:
            messages.extend(history[-10:])  # keep last 10 turns

        messages.append({
            "role": "user",
            "content": f"""Context — Current cluster cost analysis:
{context}

Question: {question}""",
        })

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        return response.content[0].text

    # ── Command generation ────────────────────────────────────────────────────

    async def generate_remediation_commands(
        self,
        report: CostReport,
        action_type: str = "all",
    ) -> str:
        """
        Generate ready-to-run kubectl/eksctl/aws CLI commands.
        action_type: "drain_idle_nodes" | "patch_pod_resources" | "scale_down_hpa" | "all"
        """
        data = {
            "idle_nodes": report.idle_nodes,
            "node_recs": [
                {"name": r.node_name, "action": r.action,
                 "current": r.current_instance, "recommended": r.recommended_instance}
                for r in report.node_recommendations
                if r.action in ("terminate", "downsize")
            ],
            "pod_recs": [
                {
                    "namespace": r.namespace,
                    "owner_kind": r.owner_kind,
                    "owner_name": r.owner_name,
                    "new_cpu_req": r.recommended_request_cpu,
                    "new_mem_req": r.recommended_request_mem_gi,
                    "new_cpu_lim": r.recommended_limit_cpu,
                    "new_mem_lim": r.recommended_limit_mem_gi,
                }
                for r in sorted(
                    report.pod_recommendations,
                    key=lambda x: x.annual_savings_usd,
                    reverse=True,
                )[:20]
                if r.action == "rightsize"
            ],
        }

        prompt = f"""Generate production-safe kubectl and AWS CLI commands for these EKS cost optimizations.

Data:
```json
{json.dumps(data, indent=2)}
```

Requirements:
- Include safety checks (dry-run first, then actual command)
- Add kubectl cordon + drain for node removals
- Generate kubectl patch commands for pod resource changes targeting the owner (Deployment/StatefulSet)
- Convert GiB values to Mi for kubectl (e.g., 0.5Gi = 512Mi)
- Include eksctl commands for node group scaling where appropriate
- Group commands by risk level: LOW → MEDIUM → HIGH
- Add comments explaining each command
- Include a rollback command for each change

Return only the commands in organized code blocks."""

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=3000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text

    # ── Karpenter / HPA / VPA config ──────────────────────────────────────────

    async def generate_autoscaler_config(
        self,
        snapshot_summary: Dict,
        tool: str = "karpenter",
    ) -> str:
        """Generate HPA, VPA, or Karpenter configuration YAML."""
        prompt = f"""Generate production-ready {tool.upper()} configuration for this EKS cluster.

Cluster summary:
```json
{json.dumps(snapshot_summary, indent=2, default=str)}
```

Requirements:
- Generate complete, deployable YAML
- Include comments explaining key settings
- Follow EKS best practices
- For Karpenter: include NodePool and EC2NodeClass
- For VPA: include VPA objects for top over-provisioned workloads
- For HPA: include HPA objects with CPU+memory metrics
- Include resource-aware consolidation settings where applicable

Return only the YAML in a code block."""

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=3000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text
