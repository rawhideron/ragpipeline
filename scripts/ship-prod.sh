#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 \"commit message\" [path ...]" >&2
  exit 2
fi

message="$1"
shift

cd "$(dirname "$0")/.."

branch="$(git branch --show-current)"
if [[ "$branch" != "main" && "$branch" != "master" ]]; then
  echo "Refusing to ship from '$branch'. Switch to main or master first." >&2
  exit 1
fi

python3 -m py_compile app/src/main.py
git diff --check

if [[ $# -gt 0 ]]; then
  git add "$@"
else
  git add -A
fi

if git diff --cached --quiet; then
  echo "No staged changes to commit."
  exit 0
fi

git commit -m "$message"
git push origin "$branch"

cat <<EOF
Pushed to origin/$branch.
GitHub Actions will build and push production images, then commit the image tags
that ArgoCD watches under k8s/176.
EOF
