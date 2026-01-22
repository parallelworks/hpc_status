#!/usr/bin/env bash
#
# Run tests for HPC Status Monitor

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${HPC_STATUS_VENV:-${HOME}/.venvs/hpc-status}"

cd "${PROJECT_ROOT}"

# Check if venv exists
if [ ! -d "$VENV_DIR" ]; then
    echo "Virtual environment not found. Running setup..."
    ./scripts/run.sh --help >/dev/null 2>&1 || true
fi

# Activate venv if it exists
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

# Install test dependencies
echo "Installing test dependencies..."
pip install -q pytest pytest-cov 2>/dev/null || true

# Run tests
echo "Running tests..."
python -m pytest tests/ -v --tb=short "$@"
