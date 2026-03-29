#!/bin/sh
# entrypoint.sh
# At runtime, substitute the __API_URL__ placeholder in the React build
# so the frontend knows where the backend API lives.
# In-cluster: API_URL is empty ("") because React is served from the same origin.
# External: set API_URL env var to the backend's public URL.

set -e

API_URL="${API_URL:-}"
CONFIG_FILE="/app/static/config.js"

if [ -f "$CONFIG_FILE" ]; then
  sed -i "s|__API_URL__|${API_URL}|g" "$CONFIG_FILE"
  echo "[entrypoint] Injected API_URL='${API_URL}' into ${CONFIG_FILE}"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 2 \
  --log-level info
