#!/bin/bash
set -e

echo ""
echo "=========================================="
echo "Running Black (code formatter)..."
echo "=========================================="
black server/ tests/

echo ""
echo "=========================================="
echo "Running docformatter (docstring formatter)..."
echo "=========================================="
docformatter server/ tests/

echo ""
echo "=========================================="
echo "Running isort (import sorter)..."
echo "=========================================="
isort server/ tests/

echo ""
echo "=========================================="
echo "Formatting complete!"
echo "=========================================="
