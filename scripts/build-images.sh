#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker build -t ragpipeline-api:0.1.0 app
docker build -t ragpipeline-frontend:0.1.0 frontend

kind load docker-image ragpipeline-api:0.1.0
kind load docker-image ragpipeline-frontend:0.1.0
