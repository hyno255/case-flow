#!/usr/bin/env python3
import json, sys
package = sys.argv[1]
def find(node, trail):
    trail = trail + [{k: node[k] for k in ("name", "version") if k in node}]
    if node["name"] == package:
        return trail
    for dep in node.get("deps", []):
        hit = find(dep, trail)
        if hit:
            return hit
print(json.dumps({"file": "dependency-tree.json", "package": package, "path": find(json.load(open("dependency-tree.json")), []) or []}))
