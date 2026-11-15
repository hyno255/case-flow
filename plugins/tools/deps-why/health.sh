#!/bin/sh
# Healthy when the tool finds the way to a known package in a small dependency tree. The last line printed is shown on the Tools page.
cd "$(dirname "$0")/health" || exit 1
../run.py zlib-example | grep -q '"name": "http-kit"' || { echo "did not find the way to zlib-example in health/dependency-tree.json"; exit 1; }
echo "finds the way to a known package in health/dependency-tree.json"
