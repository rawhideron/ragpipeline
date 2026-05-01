#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

API_IMAGE="${API_IMAGE:-kind-registry:5000/ragpipeline-api:0.1.3-lifecycle}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:-ragpipeline-frontend:0.1.1-lifecycle}"

docker build -t "$API_IMAGE" app
docker build -t "$FRONTEND_IMAGE" frontend

KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-$(kind get clusters | head -n 1)}"
KIND_LOAD_TMPDIR="${KIND_LOAD_TMPDIR:-$HOME/snap/docker/common/kind-load-tmp}"
mkdir -p "$KIND_LOAD_TMPDIR"

TMPDIR="$KIND_LOAD_TMPDIR" kind load docker-image --name "$KIND_CLUSTER_NAME" "$API_IMAGE"
TMPDIR="$KIND_LOAD_TMPDIR" kind load docker-image --name "$KIND_CLUSTER_NAME" "$FRONTEND_IMAGE"
