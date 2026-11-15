#!/bin/sh
# One ticket arrives as JSON on stdin. The current directory is its work folder.
# Everything written here is kept as evidence. No model is involved.
set -e
cat > ticket.json
# Stand-in for the catalogue lookup, the clone, the dependency listing, the image package
# lists and the candidate versions (svc-catalog, git, deps-tree, syft, pkg-candidates).
cp "$AGENTFLOW_FLOW_DIR/fixtures/$AGENTFLOW_ITEM_ID/"*.json .
