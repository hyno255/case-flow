-- Platform schema. One schema serves all teams; items.handler_id is the tenancy key.
-- Knowledge packages live in git, never here — the hub only ever indexes them.
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  config JSON NOT NULL DEFAULT '{}',   -- manifest snapshot + dir_ref
  owners JSON NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS handlers (
  handler_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  manifest JSON NOT NULL,          -- full validated manifest; source of truth for schemas
  package_ref TEXT                 -- git repo or local path
);

CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  meta JSON NOT NULL DEFAULT '{}',         -- small source-specific metadata; the ONLY case data the hub stores
  content TEXT,                            -- pointer to the case home's source/ zone; the platform never reads it
  handler_id TEXT,
  handler_version TEXT,
  state TEXT NOT NULL DEFAULT 'new',
  current_stage TEXT,
  generation INTEGER NOT NULL DEFAULT 1,   -- bumps when the source re-opens the case; keys the case-home dir
  p_severity TEXT, p_owner TEXT, p_disposition TEXT, p_confidence REAL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_handler_state ON items(handler_id, state);

CREATE TABLE IF NOT EXISTS attempts (           -- APPEND-ONLY
  result_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  handler_id TEXT,
  handler_version TEXT,
  stage_name TEXT NOT NULL,                     -- screen | <stage> | output
  attempt INTEGER NOT NULL,
  agent TEXT,
  prompt_hash TEXT,
  result JSON,
  raw_output TEXT,
  artifacts JSON,                               -- relative paths under the case home's artifacts/ lane (pointers only)
  duration_ms INTEGER,
  log TEXT,                                     -- relative path under the case home's logs/ (the execution record)
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(item_id, stage_name, attempt)          -- idempotency key for result submission
);

CREATE TABLE IF NOT EXISTS evals (              -- APPEND-ONLY: one row per human eval
  eval_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  input TEXT,                                   -- NULL = bare confirmation
  fields JSON NOT NULL,                         -- {name: {value, grade}}
  reasons TEXT,
  lesson TEXT,
  user TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  runtime_id TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id, released_at);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  handler_id TEXT,
  triggered_by TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stats JSON NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS routes (             -- deterministic wire: source -> handler, first-match by rowid
  route_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  handler_id TEXT NOT NULL
);
