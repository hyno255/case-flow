#!/bin/sh
# Healthy when the tool finds a known package in a small package list. The last line printed is shown on the Tools page.
cd "$(dirname "$0")/health" || exit 1
../run.py sbom.json zlib-example | grep -q '"found": true' || { echo "did not find zlib-example in health/sbom.json"; exit 1; }
echo "finds a known package in health/sbom.json"
