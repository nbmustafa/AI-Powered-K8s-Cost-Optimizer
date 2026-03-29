"""
EKS Cost Optimizer - Main FastAPI Application
Principal Platform Engineer: Production-grade AI-powered cost optimization service
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.api.routes import router as api_router
from app.config import settings
from app.collectors.k8s_collector import K8sCollector
from app.collectors.metrics_collector import MetricsCollector
from app.analyzers.cost_analyzer import CostAnalyzer
from app.analyzers.ai_advisor import AIAdvisor
from app.models.cache import MetricsCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup & shutdown."""
    logger.info("EKS Cost Optimizer starting up...")

    app.state.k8s_collector = K8sCollector(settings.kubeconfig_path)
    app.state.metrics_collector = MetricsCollector(
        prometheus_url=settings.prometheus_url,
        cloudwatch_region=settings.aws_region,
    )
    app.state.cost_analyzer = CostAnalyzer(region=settings.aws_region)
    app.state.ai_advisor = AIAdvisor(api_key=settings.anthropic_api_key)
    app.state.cache = MetricsCache(ttl_seconds=settings.cache_ttl_seconds)

    asyncio.create_task(_warm_cache(app))
    logger.info("All services initialized")
    yield
    logger.info("EKS Cost Optimizer shutting down...")


async def _warm_cache(app: FastAPI):
    try:
        await asyncio.sleep(2)
        data = await app.state.k8s_collector.collect_all()
        app.state.cache.set("cluster_snapshot", data)
        logger.info("Cache warmed successfully")
    except Exception as e:
        logger.warning(f"Cache warm-up failed (non-fatal): {e}")


app = FastAPI(
    title="EKS Cost Optimizer",
    description="AI-powered Kubernetes cost optimization and right-sizing engine",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Middleware ─────────────────────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routes  ────────────────────────────────────────────────────────────────
app.include_router(api_router, prefix="/api/v1")


# ── Health probes (defined before SPA catch-all) ───────────────────────────────
@app.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok", "service": "eks-cost-optimizer"}


@app.get("/readyz", tags=["health"])
async def readyz():
    return {"status": "ready"}


# ── Static files  ──────────────────────────────────────────────────────────────
# The multi-stage Dockerfile copies `npm run build` output into /app/static.
# In local dev, point STATIC_FILES_DIR at frontend/build after running npm build,
# or just use the CRA dev server (npm start) with its built-in proxy to :8080.
_static_dir = os.environ.get(
    "STATIC_FILES_DIR",
    os.path.join(os.path.dirname(__file__), "..", "static"),
)

if os.path.isdir(_static_dir):
    # Serve hashed JS/CSS/image assets at /static/*
    app.mount("/static", StaticFiles(directory=_static_dir), name="react-static")
    logger.info(f"Serving React build from: {_static_dir}")

    @app.get("/config.js", include_in_schema=False)
    async def config_js():
        """Runtime config injected by entrypoint.sh — consumed by public/index.html."""
        return FileResponse(
            os.path.join(_static_dir, "config.js"),
            media_type="application/javascript",
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """
        SPA catch-all — must be last.
        Returns index.html for any path not already matched by /api/*, /healthz, etc.
        React Router then handles client-side navigation.
        """
        index_path = os.path.join(_static_dir, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return JSONResponse({"detail": "Frontend build not found"}, status_code=404)
else:
    logger.warning(f"No static dir at {_static_dir} — running in API-only mode")


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        workers=1,
        log_level="info",
    )
