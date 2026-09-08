#!/usr/bin/env bash
# Queue happy-path end-to-end test wrapper.
# Expects a dev virtualenv at .venv-dev with lh-harness and its dependencies.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${REPO_ROOT}/.venv-dev/bin/python"

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${REPO_ROOT}/src"

exec "${PYTHON}" "${REPO_ROOT}/e2e/happy_path.py"
