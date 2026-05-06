#!/usr/bin/env bash
set -euo pipefail

: "${GENAUG_API_KEY:?Set GENAUG_API_KEY to a project-scoped key}"
GENAUG_API_BASE_URL="${GENAUG_API_BASE_URL:-https://api.generalaugment.com}"

curl -sS "${GENAUG_API_BASE_URL}/v1/responses" \
  -H "Authorization: Bearer ${GENAUG_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-curl-response-1" \
  -d '{
    "model": "balanced",
    "user": "app-user-123",
    "input": "Reply with a concise welcome message.",
    "metadata": {
      "example": "curl"
    }
  }'
