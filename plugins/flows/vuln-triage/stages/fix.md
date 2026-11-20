Read origin.md and candidates.json.
- App dependency: pick the lowest release of the direct dependency that brings in a
  fixed version. Prefer one that is not a major release.
- Base image: pick the newest tag on the same base line whose package list has a
  fixed version.
- Both: do both.
- If candidates.json shows no release or tag with a fixed version, the stage has failed: say so.
Write fix.md: what to change, to which version or tag, and the risk.
