# Frontend ↔ Backend — How it's wired

This document explains exactly how the React frontend connects to the FastAPI
backend in every environment, so there is no ambiguity.

---

## Single container, same origin (production & staging)

```
Browser
  │
  ├── GET /            → index.html  (React app shell)
  ├── GET /static/*    → JS/CSS/images (hashed by CRA)
  ├── GET /config.js   → runtime config  { apiUrl: "" }
  │
  └── GET /api/v1/*    → FastAPI handlers
      POST /api/v1/*
```

A single container image serves **both** the React build and the FastAPI API
on port 8080.  The ALB forwards all traffic to that one port.

React calls `fetch('/api/v1/cost-report')` — same origin, no CORS, no separate
backend URL needed.

### How the static files get there

```
Dockerfile (3 stages)
  │
  ├── Stage 1: frontend-builder (Node 20)
  │     COPY frontend/ → npm ci → npm run build → /frontend/build/
  │
  ├── Stage 2: python-builder
  │     pip install → /install/
  │
  └── Stage 3: runtime
        COPY --from=python-builder /install  → /usr/local/
        COPY backend/app/            → /app/app/
        COPY --from=frontend-builder /frontend/build/ → /app/static/
        COPY backend/entrypoint.sh   → /app/entrypoint.sh
```

### Runtime config injection

At **pod startup**, `entrypoint.sh` runs:

```sh
sed -i "s|__API_URL__|${API_URL}|g" /app/static/config.js
```

`API_URL` is an env var set in the Helm values (`extraEnv`).  For same-origin
deployments it is `""` (empty string), so the React app calls relative paths.

`index.html` loads `/config.js` before React boots:

```html
<script src="/config.js"></script>   <!-- sets window.APP_CONFIG -->
```

`src/App.js` reads it:

```js
const API_URL = window.APP_CONFIG?.apiUrl ?? '';
```

### FastAPI routing

Routes are registered in this order (order matters):

1. `/api/v1/*`   — API router (highest priority)
2. `/healthz`    — liveness probe
3. `/readyz`     — readiness probe
4. `/docs`       — Swagger UI
5. `/static/*`   — StaticFiles mount (hashed JS/CSS assets)
6. `/config.js`  — runtime config file
7. `/{path:path}` — **SPA catch-all** (lowest priority, returns index.html)

The catch-all is what makes React Router work: navigating to
`/nodes` or `/ai` in the browser returns `index.html` and React handles
the route client-side.

---

## Local dev — Mode A: full stack in Docker (matches production)

```
Browser :8080
    │
    └── docker compose up --build
          └── single container
                ├── FastAPI on :8080
                └── React build inside /app/static/
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make dev          # or: docker compose up --build
open http://localhost:8080
```

No hot-reload. Requires a full `docker compose up --build` after any
frontend change.  Good for verifying the production container behaviour.

---

## Local dev — Mode B: split processes (hot-reload)

```
Browser :3000  →  CRA dev server (hot-reload)
                      │
                      └── /api/* proxy → FastAPI :8080
                                            │
                                       docker compose
                                       (backend + prometheus)
```

```bash
# Terminal 1 — backend only
export ANTHROPIC_API_KEY=sk-ant-...
make dev-api       # docker compose up backend prometheus

# Terminal 2 — frontend hot-reload
make dev-ui        # cd frontend && npm start → :3000
open http://localhost:3000
```

The `"proxy": "http://localhost:8080"` field in `frontend/package.json` tells
the CRA dev server to forward any request that isn't a static asset to the
FastAPI backend.  This means `fetch('/api/v1/cost-report')` in the React code
works identically in both modes.

---

## Split-host deployment (optional)

If you ever want to deploy the API and frontend on separate domains:

1. Set `API_URL` in `values-production.yaml`:

```yaml
extraEnv:
  - name: API_URL
    value: "https://api.eks-cost-optimizer.internal.company.com"
```

2. The Helm Deployment passes this to the pod; `entrypoint.sh` writes it into
   `/app/static/config.js`; `App.js` picks it up as `apiUrl`.

3. Add the frontend domain to `CORS_ORIGINS` in the backend config:

```yaml
config:
  CORS_ORIGINS: '["https://eks-cost-optimizer.internal.company.com"]'
```

---

## GitHub Actions build

The CI workflow sets `context: .` (repo root) and
`file: backend/Dockerfile` so Docker can reach both `frontend/` and
`backend/` in a single build:

```yaml
- uses: docker/build-push-action@v5
  with:
    context: .                   # ← repo root
    file: backend/Dockerfile     # ← Dockerfile inside backend/
    push: true
    tags: ${{ env.ECR_REPO }}:${{ env.SHA }}
```

The `.dockerignore` at the repo root excludes `node_modules`, `.git`,
test files, and secrets to keep the build context small and fast.
