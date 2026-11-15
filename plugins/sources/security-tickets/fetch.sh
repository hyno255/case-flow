#!/bin/sh
# Writes the open security tickets into the fetch's output folder, as source.yaml says: tickets.json.
# Stand-in for the team's tracker CLI (for example: secticket list --status open --json > "$AGENTFLOW_OUT/tickets.json").
cp "$(dirname "$0")/fixtures/tickets.json" "$AGENTFLOW_OUT/tickets.json"
