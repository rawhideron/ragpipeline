#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .git \
  --exclude .venv \
  ./ rawhideron@192.168.1.153:/home/rawhideron/Projects/ragpipeline/
