"""
Application configuration — all values from environment variables.
Follows 12-factor App principles.
"""

from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # ── Kubernetes ────────────────────────────────────────────────────────────
    kubeconfig_path: Optional[str] = Field(None, env="KUBECONFIG")
    k8s_in_cluster: bool = Field(True, env="K8S_IN_CLUSTER")
    cluster_name: str = Field("eks-cluster", env="CLUSTER_NAME")

    # ── AWS ───────────────────────────────────────────────────────────────────
    aws_region: str = Field("us-east-1", env="AWS_DEFAULT_REGION")
    aws_access_key_id: Optional[str] = Field(None, env="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(None, env="AWS_SECRET_ACCESS_KEY")
    aws_role_arn: Optional[str] = Field(None, env="AWS_ROLE_ARN")

    # ── Anthropic AI ──────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(..., env="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-opus-4-5", env="ANTHROPIC_MODEL")
    ai_max_tokens: int = Field(4096, env="AI_MAX_TOKENS")

    # ── Prometheus ────────────────────────────────────────────────────────────
    prometheus_url: Optional[str] = Field(
        "http://prometheus-server.monitoring.svc.cluster.local:9090",
        env="PROMETHEUS_URL",
    )
    metrics_lookback_hours: int = Field(24, env="METRICS_LOOKBACK_HOURS")

    # ── Cache ─────────────────────────────────────────────────────────────────
    cache_ttl_seconds: int = Field(300, env="CACHE_TTL_SECONDS")

    # ── Server ────────────────────────────────────────────────────────────────
    cors_origins: List[str] = Field(["*"], env="CORS_ORIGINS")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    # ── Cost ─────────────────────────────────────────────────────────────────
    # Override EC2 on-demand pricing (USD/hour) — fallback if AWS Pricing API unavailable
    default_cpu_cost_per_core_hour: float = Field(0.048, env="CPU_COST_PER_CORE_HOUR")
    default_mem_cost_per_gb_hour: float = Field(0.006, env="MEM_COST_PER_GB_HOUR")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
