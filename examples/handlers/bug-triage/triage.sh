#!/usr/bin/env bash
# The stage script: gather this case's evidence for the orchestrator to judge.
set -euo pipefail
cat ./case.json
echo "--- source material ---"
cat ./source/* 2>/dev/null || echo "(no source files)"
