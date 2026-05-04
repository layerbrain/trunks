#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8080}"
DURATION_S="${DURATION_S:-60}"
WORKDIR="$(pwd)"
PUBLIC_URL="${TRUNKS_PREVIEW_URL:-}"

if [ -z "$PUBLIC_URL" ] && [ -n "${DAYTONA_SANDBOX_ID:-}" ] && [ -n "${DAYTONA_API_KEY:-}" ]; then
  PUBLIC_URL="$(
    python3 - "$DAYTONA_SANDBOX_ID" "$PORT" <<'PY'
import json
import os
import sys
import urllib.request

sandbox_id = sys.argv[1]
port = sys.argv[2]
api_url = os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api").rstrip("/")
url = f"{api_url}/sandbox/{sandbox_id}/ports/{port}/signed-preview-url?expiresInSeconds=3600"
request = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['DAYTONA_API_KEY']}"})
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read().decode())
print(payload["url"])
PY
  )"
fi

echo "::group::install"
python3 -m pip install --quiet --no-input fastapi 'uvicorn[standard]'
echo "::endgroup::"

echo "::group::boot app"
mkdir -p logs
PYTHONPATH="$WORKDIR" PUBLIC_URL="$PUBLIC_URL" python3 -m uvicorn app.server:app --host 0.0.0.0 --port "$PORT" \
  > logs/app.log 2>&1 &
APP_PID=$!
echo "app pid = $APP_PID"

cleanup() {
  echo "::group::shutdown"
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  echo "::endgroup::"
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  if curl -sSf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "app ready"
    break
  fi
  sleep 0.5
done

if ! curl -sSf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo "app failed to start"
  cat logs/app.log
  exit 1
fi
echo "::endgroup::"

if [ -n "$PUBLIC_URL" ]; then
  echo
  echo "=================================================="
  echo " LIVE: $PUBLIC_URL"
  echo "=================================================="
  echo
fi

echo "::group::smoke ($DURATION_S s)"
TARGET_URL="http://127.0.0.1:${PORT}" PUBLIC_URL="$PUBLIC_URL" DURATION_S="$DURATION_S" REPORT_PATH="report.json" \
  python3 scripts/smoke.py
echo "::endgroup::"

echo "::group::tail app log"
tail -n 50 logs/app.log || true
echo "::endgroup::"
