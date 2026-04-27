#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker build -t ragpipeline-api:0.1.0 app
docker build -t ragpipeline-frontend:0.1.0 frontend

KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-$(kind get clusters | head -n 1)}"
KIND_LOAD_TMPDIR="${KIND_LOAD_TMPDIR:-$HOME/snap/docker/common/kind-load-tmp}"
mkdir -p "$KIND_LOAD_TMPDIR"

TMPDIR="$KIND_LOAD_TMPDIR" kind load docker-image --name "$KIND_CLUSTER_NAME" ragpipeline-api:0.1.0
TMPDIR="$KIND_LOAD_TMPDIR" kind load docker-image --name "$KIND_CLUSTER_NAME" ragpipeline-frontend:0.1.0
