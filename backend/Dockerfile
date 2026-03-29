# ─────────────────────────────────────────────────────────────────────────────
# EKS Cost Optimizer — Multi-stage Dockerfile
#
# Build context: REPO ROOT (not backend/)
#   docker build -f backend/Dockerfile .
#   docker compose up   ← sets context: . in docker-compose.yml
#
# Stages:
#   1. frontend-builder  — npm install + npm run build → /frontend/build
#   2. python-builder    — pip install → /install prefix
#   3. runtime           — lean image with Python app + React static files
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: React build ──────────────────────────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

# Install deps first (layer-cached until package.json changes)
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --silent

# Copy source and build
COPY frontend/ ./
# Ensure config.js placeholder exists before build so CRA copies it to build/
RUN echo "window.APP_CONFIG = { apiUrl: '__API_URL__' };" > public/config.js
RUN npm run build


# ── Stage 2: Python deps ──────────────────────────────────────────────────────
FROM python:3.12-slim AS python-builder

WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 3: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="EKS Cost Optimizer"
LABEL org.opencontainers.image.description="AI-powered Kubernetes cost optimization engine"
LABEL org.opencontainers.image.version="1.0.0"
LABEL org.opencontainers.image.vendor="Platform Engineering"

# Non-root user
RUN groupadd -r appgroup && useradd -r -g appgroup -u 1001 appuser

WORKDIR /app

# Python packages from builder
COPY --from=python-builder /install /usr/local

# Application source
COPY backend/app/ ./app/

# React static build → FastAPI serves from /app/static
COPY --from=frontend-builder /frontend/build/ ./static/

# Startup script (substitutes __API_URL__ in config.js, then launches uvicorn)
COPY backend/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV STATIC_FILES_DIR=/app/static

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

USER appuser
EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
