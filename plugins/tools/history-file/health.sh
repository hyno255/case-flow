#!/bin/sh
# Healthy when the engine's command can be called. The last line printed is shown on the Tools page.
command -v agentflow >/dev/null || { echo "the agentflow command is not on PATH"; exit 1; }
echo "agentflow history can be called"
