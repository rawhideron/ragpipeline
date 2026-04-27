#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .git \
  --exclude .venv \
  ./ rongoodman@192.168.1.230:/home/rongoodman/Projects/ragpipeline/
