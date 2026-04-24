#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-$HOME/.local/bin/kubectl}"
cd "$(dirname "$0")/.."

"$KUBECTL" apply -f k8s/230
"$KUBECTL" -n rag rollout status deploy/rag-oauth2-proxy --timeout=180s
"$KUBECTL" -n rag get pods,svc,ingress

