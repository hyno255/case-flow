# Writing a source

A source is a plugin that *brings cases in*: a fetch script, its own scope,
and its own change semantics, packaged together. Scope (which repo, which
project, which team) lives in the plugin's yaml — never on the command line.

```
my-github/
├── source.yaml
├── fetch.sh
└── delta.sh        # only if on_existing: delta
```

Start from `caseflow source init <name>` — the scaffold fetches GitHub issues
via the public API and works unauthenticated for public repos.

## The fetch contract

The platform stores **metadata**; content stays **files**. Your fetch script
does both halves:

- **stdout** — one small JSON line per case:

  ```json
  {"external_id": "acme/app#123", "title": "NPE in checkout", "meta": {"labels": ["bug"], "author": "kim"}}
  ```

  - `external_id` — the case's stable ID in the source system. Identity is
    `(source, external_id)`: the same record seen again is the same case.
  - `title` — one human-readable line for queues.
  - `meta` — small source-specific JSON (labels, author, links…). It seeds
    search and prompts, and it is the *only* case data the hub stores
    (≤ 64 KB — it's metadata by definition).

- **files** — the full content, any size, any shape, under
  `--out/<external_id>/` (the platform passes `--out <dir>`): the
  description, attachments, log excerpts, whatever the handler should read.
  The platform moves that directory into the case's home as its `source/`
  zone and keeps only the path. A metadata-only source can skip this half.

## source.yaml

```yaml
id: my-github
run: ./fetch.sh
params:
  repo: { default: "your-org/your-app" }   # ← the scope, set once, here
  since: { default: 24h }
on_existing: ignore                        # what a re-fetched KNOWN case means (below)
requires:
  tools:
    - { name: curl, check: "curl --version" }
    - { name: jq, check: "jq --version" }
```

Declared params are passed to the script as flags, defaults applied from this
file; a param can still be overridden per call (`caseflow fetch my-github
--since 7d`). **One source per scope** — two repos means two source dirs, so
identity never collides.

Credentials follow execution: the script runs on the machine that triggers
the fetch, under that user's auth. The hub never sees a token.

## Change detection is yours, not the platform's

Whether a re-fetched known case means anything is a question only the source
can answer — so `on_existing` declares it, and the platform only triggers
your policy and applies the verdict:

- **`ignore`** (default) — a re-fetch means nothing; metadata refreshes
  (title, meta, last-seen), the case's state never moves.
- **`replace`** — every re-fetch supersedes: the case re-opens with the new
  content (generation bump, fresh `source/`, back through the pipeline).
- **`delta`** — ask your `delta:` script. It receives on stdin

  ```json
  {"old": {"path": "…/gen-1/source", "meta": {…}}, "new": {"path": "…/fetch-tmp/<id>", "meta": {…}}}
  ```

  and prints `{"changed": true}` or `{"changed": false}`. Diff one file,
  compare a field, or invoke your agent to read both versions and judge —
  it's a script, so the intelligence level is your call.

On `replace`, or a `delta` verdict of `changed: true`, the case **re-opens**:
its generation bumps, the new content becomes `gen-<n+1>/source/`, and it
re-enters the pipeline — with its own fresh home, so the old generation's
files and the banked knowledge from the old decision stay intact (the old
package is auto-archived when the new decision banks). A case mid-flight
under a live lease is left alone; the change is picked up on the next fetch.

Register and run:

```bash
caseflow source add ./my-github
caseflow route my-github my-team/triage
caseflow fetch my-github
```

Fetching is cheap — no platform AI runs until `process` (your delta hook may
choose to spend intelligence, but that's the plugin's call). Ingested cases
wait in `routed` at zero cost, so fetch aggressively and process
deliberately.
