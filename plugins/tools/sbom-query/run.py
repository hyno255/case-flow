#!/usr/bin/env python3
import json, sys
sbom_file, package = sys.argv[1], sys.argv[2]
hits = [p for p in json.load(open(sbom_file))["packages"] if p["name"] == package]
print(json.dumps({"file": sbom_file, "package": package, "found": bool(hits), **(hits[0] if hits else {})}))
