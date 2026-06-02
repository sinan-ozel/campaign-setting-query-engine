#!/bin/bash
set -e

echo ""
echo "=========================================="
echo "Running Ruff (linter)..."
echo "=========================================="
ruff check ./server ./tests

echo ""
echo "=========================================="
echo "All linting steps completed!"
echo "=========================================="
