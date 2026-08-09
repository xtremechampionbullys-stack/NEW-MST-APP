#!/usr/bin/env bash
# Regenerate TypeScript types from the running API's OpenAPI schema.
#
# Run after changing any request model or route signature. The output is
# committed so the frontend builds without a running backend.
#
#   ./scripts/generate-types.sh [api-url]
#
# Scope: this types request bodies, path params and query params exactly, from
# the server's own schema. It does NOT cover response shapes -- FastAPI only
# describes responses that declare a response_model, and most of ours return
# plain dicts. Response drift is caught instead by the contract tests in
# services/api/tests/test_contracts.py, which assert against real responses.
set -euo pipefail

API="${1:-http://localhost:8000}"

echo "fetching ${API}/openapi.json"
curl -sf "${API}/openapi.json" -o /tmp/marketshark-openapi.json

# --default-non-nullable=false is deliberate. By default openapi-typescript
# marks any field carrying a default as REQUIRED, on the reasoning that the
# server always populates it. That is right for responses and backwards for
# request bodies: a field the server defaults is precisely one the client may
# omit. With the flag on, every call site would have to spell out every
# parameter -- including ones it has no opinion about -- or fail to compile.
# We consume request types from this schema and not response types (see the
# note in apps/web/src/lib/api.ts), so this trade is the correct way round.
echo "generating apps/web/src/lib/api-schema.d.ts"
(cd apps/web && npx openapi-typescript /tmp/marketshark-openapi.json \
  --default-non-nullable=false -o src/lib/api-schema.d.ts)
echo "done"
