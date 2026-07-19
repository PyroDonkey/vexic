#!/usr/bin/env bash
# Release audit dump for origin/main...dev (PR #253). Run from repo root:
#   bash scripts/release_audit_dump.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "=== 1. git rev-parse origin/main origin/dev HEAD ==="
git rev-parse origin/main origin/dev HEAD

echo ""
echo "=== 2. git log --oneline origin/main..dev ==="
git log --oneline origin/main..dev

echo ""
echo "=== 3. git diff --stat origin/main...dev ==="
git diff --stat origin/main...dev

echo ""
echo "=== 4. git diff --name-only origin/main...dev ==="
git diff --name-only origin/main...dev

echo ""
echo "=== 5. git diff origin/main...dev (Python source files) ==="
git diff origin/main...dev -- \
  src/vexic/mcp_http.py \
  src/vexic/recorders/cli.py \
  src/vexic/recorders/hosted_ingest.py \
  src/vexic/recorders/hosted_prime.py \
  src/vexic/recorders/status.py

echo ""
echo "=== 6a. git diff origin/main...dev (version files) ==="
git diff origin/main...dev -- pyproject.toml src/vexic/__init__.py

echo ""
echo "=== 6b. version/packaging changed files ==="
git diff --name-only origin/main...dev | rg -i 'version|packag' || true

echo ""
echo "=== 6c. git diff for version/packaging changed files ==="
mapfile -t _vfiles < <(git diff --name-only origin/main...dev | rg -i 'version|packag' || true)
if ((${#_vfiles[@]})); then
  git diff origin/main...dev -- "${_vfiles[@]}"
fi

echo ""
echo "=== 7. gh pr view 253 ==="
gh pr view 253 --json title,body,baseRefName,headRefName,commits,files,state,mergeable,statusCheckRollup
