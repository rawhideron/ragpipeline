#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .git \
  --exclude .venv \
  ./ ron-goodman@192.168.1.176:/home/ron-goodman/Projects/ragpipeline/
