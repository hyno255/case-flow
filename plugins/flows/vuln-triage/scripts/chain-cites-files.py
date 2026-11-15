#!/usr/bin/env python3
"""check for the analyse stage: exit 0 passes; anything printed on a fail is the feedback."""
import os, re, sys
problems = []
if not os.path.exists("origin.md"):
    print("origin.md is missing"); sys.exit(1)
text = open("origin.md").read()
if not re.search(r"^Origin: (app dependency|base image|both)$", text, re.M):
    problems.append("no line 'Origin: app dependency', 'Origin: base image' or 'Origin: both'")
chain = text.split("## Chain", 1)[1] if "## Chain" in text else ""
links = [l[2:].strip() for l in chain.splitlines() if l.startswith("- ")]
if not links:
    problems.append("the '## Chain' list is missing or empty")
for i, link in enumerate(links, 1):
    cited = re.search(r"\(file: ([^)]+)\)\s*$", link)
    if not cited:
        problems.append(f'chain link {i} cites no file; end the link with "(file: <name>)", for example "(file: image-sbom.json)": "{link}"')
    elif not os.path.isfile(cited.group(1)):
        problems.append(f"chain link {i} cites {cited.group(1)}, which is not in the work folder")
print("\n".join(problems))
sys.exit(1 if problems else 0)
