#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

kubectl apply -f k8s/153
kubectl -n rag rollout status deploy/qdrant --timeout=180s
kubectl -n rag rollout status deploy/ingestion-api --timeout=180s
kubectl -n rag rollout status deploy/rag-api --timeout=180s
kubectl -n rag rollout status deploy/rag-frontend --timeout=180s
kubectl -n rag get pods,svc,pvc

