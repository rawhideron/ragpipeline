#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-$HOME/.local/bin/kubectl}"
cd "$(dirname "$0")/.."

"$KUBECTL" apply -f argocd/project.yaml
"$KUBECTL" apply -f argocd/ragpipeline-workloads.yaml
"$KUBECTL" apply -f argocd/ragpipeline-ingress.yaml
"$KUBECTL" -n argocd get applications.argoproj.io ragpipeline-workloads ragpipeline-ingress
