import Database from "better-sqlite3";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ulid } from "ulid";
import {
  META_CAP_BYTES, PROMOTED_FIELDS,
  type IntakeItem, type IngestResult, type HandlerManifest, type SourceManifest, type ItemState,
  type ResultStatus, type EvalSubmission, type HandlerStats,
} from "@caseflow/protocol";

const __dirname = dirname(fileURLToPath(import.meta.url));

export interface ItemRow {
  item_id: string; source_id: string; external_id: string; title: string;
  meta: string; content: string | null; handler_id: string | null;
  handler_version: string | null; state: ItemState; current_stage: string | null; generation: number;
  p_severity: string | null; p_owner: string | null; p_disposition: string | null; p_confidence: number | null;
  first_seen_at: string; last_seen_at: string; updated_at: string;
}

export const LEASE_MS = 15 * 60 * 1000;

export class Store {
  readonly db: Database.Database;

  constructor(path = process.env.CASEFLOW_DB ?? "caseflow.db") {
    this.db = new Database(path);
    this.db.pragma("journal_mode = WAL");
    // schema.sql ships next to the built file (copied by the build).
    this.db.exec(readFileSync(join(__dirname, "schema.sql"), "utf8"));
  }

  now(): string { return new Date().toISOString(); }

  // ---------- intake: metadata in, content stays files ----------
  /** Where a case generation's source/ zone lives under the CLI's cases root. */
  private contentPath(casesRoot: string, caseId: string, generation: number): string {
    return resolve(casesRoot, caseId, `gen-${generation}`, "source");
  }

  /**
   * Ingest one intake item — METADATA ONLY. New identities get a row and a
   * content pointer (the CLI moves the fetched files there). Known identities
   * get a metadata refresh (title, meta, last_seen) and nothing else: whether
   * a re-fetched case re-opens is the source plugin's `on_existing` call,
   * applied by the CLI via `reopen`.
   */
  ingest(sourceId: string, item: IntakeItem, casesRoot: string): IngestResult {
    const base = { external_id: item.external_id };
    if (Buffer.byteLength(JSON.stringify(item.meta)) > META_CAP_BYTES) {
      return { ...base, case_id: null, generation: 0, existed: false, content: null, rejected: true }; // meta is metadata — content belongs in source/
    }
    const existing = this.getItemBySource(sourceId, item.external_id);
    const now = this.now();
    if (!existing) {
      const caseId = ulid();
      const content = this.contentPath(casesRoot, caseId, 1);
      this.db.prepare(`INSERT INTO items
        (item_id, source_id, external_id, title, meta, content, state, first_seen_at, last_seen_at, updated_at)
        VALUES (?,?,?,?,?,?, 'new', ?,?,?)`)
        .run(caseId, sourceId, item.external_id, item.title, JSON.stringify(item.meta), content, now, now, now);
      return { ...base, case_id: caseId, generation: 1, existed: false, content };
    }
    this.db.prepare("UPDATE items SET title=?, meta=?, last_seen_at=? WHERE item_id=?")
      .run(item.title, JSON.stringify(item.meta), now, existing.item_id);
    return {
      ...base, case_id: existing.item_id, generation: existing.generation, existed: true,
      content: existing.content, prior_meta: JSON.parse(existing.meta) as Record<string, unknown>,
    };
  }

  /**
   * Re-open a case for a new version of its source material: bump the
   * generation, swing the content pointer to the fresh gen's source/ zone, and
   * send it back through the pipeline. Called by the CLI when the source's
   * `on_existing` policy (replace, or a delta hook answering changed=true)
   * says the new fetch supersedes the old. Refused only while a runtime holds
   * a live lease — the in-flight pass finishes against its own generation.
   */
  reopen(caseId: string, casesRoot: string): { reopened: boolean; generation: number; content: string | null } {
    const item = this.getItem(caseId);
    if (!item) throw new Error("item not found");
    if (item.state === "processing" && this.hasLiveLease(caseId)) {
      return { reopened: false, generation: item.generation, content: item.content };
    }
    const generation = item.generation + 1;
    const content = this.contentPath(casesRoot, caseId, generation);
    this.db.prepare("UPDATE items SET generation=?, content=? WHERE item_id=?").run(generation, content, caseId);
    this.setItemState(caseId, item.handler_id ? "routed" : "new", null);
    return { reopened: true, generation, content };
  }

  private hasLiveLease(itemId: string): boolean {
    return !!this.db.prepare(
      "SELECT 1 FROM claims WHERE item_id=? AND released_at IS NULL AND lease_expires_at > ?")
      .get(itemId, this.now());
  }

  // ---------- routing: deterministic source -> handler, first match ----------
  routeNewItems(): { routed: number; unrouted: number } {
    const routes = this.db.prepare("SELECT source_id, handler_id FROM routes ORDER BY rowid ASC").all() as
      { source_id: string; handler_id: string }[];
    const items = this.db.prepare("SELECT item_id, source_id FROM items WHERE state='new'").all() as
      { item_id: string; source_id: string }[];
    let routed = 0, unrouted = 0;
    const upd = this.db.prepare("UPDATE items SET handler_id=?, handler_version=?, state=?, updated_at=? WHERE item_id=?");
    for (const item of items) {
      const match = routes.find((r) => r.source_id === item.source_id);
      if (match) {
        const h = this.getHandler(match.handler_id);
        upd.run(match.handler_id, h?.version ?? null, "routed", this.now(), item.item_id);
        routed++;
      } else {
        upd.run(null, null, "unrouted", this.now(), item.item_id);
        unrouted++;
      }
    }
    return { routed, unrouted };
  }

  addRoute(sourceId: string, handlerId: string): void {
    this.db.prepare("INSERT INTO routes (route_id, source_id, handler_id) VALUES (?,?,?)")
      .run(ulid(), sourceId, handlerId);
    // Give previously unroutable source items a chance under the new table.
    this.db.prepare("UPDATE items SET state='new' WHERE state='unrouted'").run();
    this.routeNewItems();
  }

  // ---------- registry ----------
  registerHandler(manifest: HandlerManifest, packageRef: string): void {
    this.db.prepare(`INSERT INTO handlers (handler_id, version, manifest, package_ref) VALUES (?,?,?,?)
      ON CONFLICT(handler_id) DO UPDATE SET version=excluded.version, manifest=excluded.manifest, package_ref=excluded.package_ref`)
      .run(manifest.id, manifest.version, JSON.stringify(manifest), packageRef);
  }

  getHandler(id: string): (HandlerManifest & { package_ref: string }) | undefined {
    const row = this.db.prepare("SELECT manifest, package_ref FROM handlers WHERE handler_id=?").get(id) as
      { manifest: string; package_ref: string } | undefined;
    return row ? { ...(JSON.parse(row.manifest) as HandlerManifest), package_ref: row.package_ref } : undefined;
  }

  registerSource(manifest: SourceManifest, dirRef: string): void {
    this.db.prepare(`INSERT INTO sources (source_id, config, owners) VALUES (?,?,?)
      ON CONFLICT(source_id) DO UPDATE SET config=excluded.config, owners=excluded.owners`)
      .run(manifest.id, JSON.stringify({ ...manifest, dir_ref: dirRef }), JSON.stringify(manifest.owners));
  }

  listSources(): { source_id: string; config: Record<string, unknown> }[] {
    return (this.db.prepare("SELECT source_id, config FROM sources").all() as
      { source_id: string; config: string }[])
      .map((r) => ({ source_id: r.source_id, config: JSON.parse(r.config) as Record<string, unknown> }));
  }

  // ---------- claims & leases ----------
  /**
   * Claim a slice of work under a lease. `pipeline` claims routed cases (and
   * un-leased processing cases — resume after crash/expiry) and marks them
   * processing; `output` claims writing_back cases for write-back, so
   * concurrent runs split the set instead of double-executing scripts.
   * Runs in one transaction so two simultaneous claims can never overlap.
   */
  claim(runtimeId: string, handlerId: string, limit: number,
    phase: "pipeline" | "output" = "pipeline", caseId?: string):
    { items: ItemRow[]; leaseExpiresAt: string } {
    return this.db.transaction(() => {
      this.expireLeases();
      const states = phase === "output" ? "('writing_back')" : "('routed','processing')";
      const byCase = caseId ? "AND i.item_id = ?" : "";
      const params = caseId ? [handlerId, caseId, this.now()] : [handlerId, this.now()];
      const candidates = this.db.prepare(`
        SELECT i.* FROM items i
        WHERE i.handler_id = ?
          AND i.state IN ${states} ${byCase}
          AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.item_id = i.item_id AND c.released_at IS NULL AND c.lease_expires_at > ?)
        ORDER BY i.first_seen_at ASC`).all(...params) as ItemRow[];
      const items = candidates.slice(0, limit);
      const leaseExpiresAt = new Date(Date.now() + LEASE_MS).toISOString();
      const ins = this.db.prepare("INSERT INTO claims (claim_id, item_id, runtime_id, lease_expires_at) VALUES (?,?,?,?)");
      const upd = this.db.prepare("UPDATE items SET state='processing', updated_at=? WHERE item_id=?");
      for (const i of items) {
        ins.run(ulid(), i.item_id, runtimeId, leaseExpiresAt);
        if (phase === "pipeline") upd.run(this.now(), i.item_id); // output cases stay writing_back
      }
      return { items, leaseExpiresAt };
    })();
  }

  /** True when this runtime holds a live lease on the item — required to submit results. */
  hasLiveClaim(itemId: string, runtimeId: string): boolean {
    return !!this.db.prepare(
      "SELECT 1 FROM claims WHERE item_id=? AND runtime_id=? AND released_at IS NULL AND lease_expires_at > ?")
      .get(itemId, runtimeId, this.now());
  }

  heartbeat(runtimeId: string): number {
    const lease = new Date(Date.now() + LEASE_MS).toISOString();
    return this.db.prepare("UPDATE claims SET lease_expires_at=? WHERE runtime_id=? AND released_at IS NULL AND lease_expires_at > ?")
      .run(lease, runtimeId, this.now()).changes;
  }

  /**
   * Lease expiry sweep: release expired claims and record an explicit `lost`
   * row for each — silence is never success, even for the hub itself.
   */
  expireLeases(): number {
    const expired = this.db.prepare(`
      SELECT c.claim_id, c.item_id, i.state, i.current_stage, i.handler_id, i.handler_version
      FROM claims c JOIN items i ON i.item_id = c.item_id
      WHERE c.released_at IS NULL AND c.lease_expires_at <= ?`).all(this.now()) as
      { claim_id: string; item_id: string; state: string; current_stage: string | null; handler_id: string | null; handler_version: string | null }[];
    const rel = this.db.prepare("UPDATE claims SET released_at=? WHERE claim_id=?");
    for (const c of expired) {
      rel.run(this.now(), c.claim_id);
      const stage = c.state === "writing_back" ? "output" : c.current_stage ?? "screen";
      this.recordResult({
        itemId: c.item_id, handlerId: c.handler_id, handlerVersion: c.handler_version,
        stage, attempt: this.nextAttempt(c.item_id, stage), status: "lost", rawOutput: "lease expired without a result",
      });
    }
    return expired.length;
  }

  releaseClaims(itemId: string): void {
    this.db.prepare("UPDATE claims SET released_at=? WHERE item_id=? AND released_at IS NULL").run(this.now(), itemId);
  }

  // ---------- attempts: explicit, validated, idempotent ----------
  latestResults(itemId: string): Record<string, unknown> {
    // rowid breaks same-millisecond ties so a human correction can never lose
    // to the agent row it corrects.
    const rows = this.db.prepare(`SELECT stage_name, result FROM attempts
      WHERE item_id=? AND status IN ('ok','overridden') ORDER BY created_at ASC, rowid ASC`).all(itemId) as
      { stage_name: string; result: string }[];
    const out: Record<string, unknown> = {};
    for (const r of rows) out[r.stage_name] = JSON.parse(r.result); // later rows win
    return out;
  }

  /** Authoritative attempt numbering, sent to runtimes at claim time. */
  nextAttempts(itemId: string): Record<string, number> {
    const rows = this.db.prepare(
      "SELECT stage_name, MAX(attempt) AS m FROM attempts WHERE item_id=? GROUP BY stage_name")
      .all(itemId) as { stage_name: string; m: number }[];
    return Object.fromEntries(rows.map((r) => [r.stage_name, r.m + 1]));
  }

  nextAttempt(itemId: string, stage: string): number {
    return this.nextAttempts(itemId)[stage] ?? 1;
  }

  recordResult(p: { itemId: string; handlerId: string | null; handlerVersion: string | null; stage: string;
    attempt: number; agent?: string; promptHash?: string; status: ResultStatus; result?: unknown; rawOutput?: string;
    artifacts?: string[]; durationMs?: number; log?: string }): boolean {
    try {
      this.db.prepare(`INSERT INTO attempts
        (result_id, item_id, handler_id, handler_version, stage_name, attempt, agent, prompt_hash, result, raw_output, artifacts, duration_ms, log, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
        .run(ulid(), p.itemId, p.handlerId, p.handlerVersion, p.stage, p.attempt, p.agent ?? null,
          p.promptHash ?? null, p.result === undefined ? null : JSON.stringify(p.result), p.rawOutput ?? null,
          p.artifacts?.length ? JSON.stringify(p.artifacts) : null, p.durationMs ?? null, p.log ?? null,
          p.status, this.now());
      return true;
    } catch (e: unknown) {
      if (String(e).includes("UNIQUE")) return false; // idempotent replay
      throw e;
    }
  }

  setItemState(itemId: string, state: ItemState, currentStage: string | null): void {
    this.db.prepare("UPDATE items SET state=?, current_stage=?, updated_at=? WHERE item_id=?")
      .run(state, currentStage, this.now(), itemId);
  }

  getItem(itemId: string): ItemRow | undefined {
    return this.db.prepare("SELECT * FROM items WHERE item_id=?").get(itemId) as ItemRow | undefined;
  }

  /** Union of artifact paths reported by ok attempts for this case (pointers into the workspace's artifacts lane). */
  itemArtifacts(itemId: string): string[] {
    const rows = this.db.prepare(
      "SELECT artifacts FROM attempts WHERE item_id=? AND artifacts IS NOT NULL AND status IN ('ok','overridden') ORDER BY created_at ASC")
      .all(itemId) as { artifacts: string }[];
    const out = new Set<string>();
    for (const r of rows) for (const a of JSON.parse(r.artifacts) as string[]) out.add(a);
    return [...out];
  }

  /** Per-attempt execution summary: what ran, how long, and where its log lives. */
  itemAttempts(itemId: string): { stage_name: string; attempt: number; status: string; agent: string | null;
    duration_ms: number | null; log: string | null; created_at: string }[] {
    return this.db.prepare(`SELECT stage_name, attempt, status, agent, duration_ms, log, created_at
      FROM attempts WHERE item_id=? ORDER BY created_at ASC, rowid ASC`).all(itemId) as ReturnType<Store["itemAttempts"]>;
  }

  getItemBySource(sourceId: string, externalId: string): ItemRow | undefined {
    return this.db.prepare("SELECT * FROM items WHERE source_id=? AND external_id=?")
      .get(sourceId, externalId) as ItemRow | undefined;
  }

  // ---------- evals: the human decision, append-only ----------
  /**
   * Record a human eval and apply it: corrected fields land as `overridden`
   * attempt rows (human value wins everywhere downstream), promoted columns
   * refresh, and the case moves to writing_back. One action.
   */
  applyEval(e: EvalSubmission): void {
    const item = this.getItem(e.item_id);
    if (!item) throw new Error("item not found");
    this.db.prepare(`INSERT INTO evals (eval_id, item_id, input, fields, reasons, lesson, user, created_at)
      VALUES (?,?,?,?,?,?,?,?)`)
      .run(ulid(), e.item_id, e.input ?? null, JSON.stringify(e.fields), e.reasons ?? null, e.lesson ?? null, e.user, this.now());

    const corrected = Object.entries(e.fields).filter(([, f]) => f.grade === "corrected");
    if (corrected.length) {
      // Merge corrections into the results of the stage that owns each field.
      const results = this.latestResults(e.item_id);
      const manifest = item.handler_id ? this.getHandler(item.handler_id) : undefined;
      const stages = manifest ? [...(manifest.screen ? [{ name: "screen", output_schema: manifest.screen.output_schema }] : []), ...manifest.stages] : [];
      const owner = (field: string) => stages.find((s) => field in (s.output_schema as Record<string, unknown>))?.name;
      const byStage = new Map<string, Record<string, unknown>>();
      for (const [field, f] of corrected) {
        const stage = owner(field) ?? "eval"; // fields the evaluator added (e.g. root_cause) live on a virtual stage
        const base = byStage.get(stage) ?? { ...((results[stage] as Record<string, unknown>) ?? {}) };
        base[field] = f.value;
        byStage.set(stage, base);
      }
      for (const [stage, merged] of byStage) {
        this.recordResult({
          itemId: e.item_id, handlerId: null, handlerVersion: null, stage,
          attempt: this.nextAttempt(e.item_id, stage), status: "overridden", result: merged,
        });
      }
    }
    if (item.handler_id) {
      const manifest = this.getHandler(item.handler_id);
      if (manifest) this.promote(e.item_id, manifest);
    }
    this.setItemState(e.item_id, "writing_back", null);
  }

  /** Copy manifest `promotes` mappings into indexed columns for status/reporting. */
  promote(itemId: string, manifest: HandlerManifest): void {
    const results = this.latestResults(itemId);
    const pick = (path: string): unknown => {
      const [stage, field] = path.split(".");
      const r = results[stage] as Record<string, unknown> | undefined;
      return r?.[field];
    };
    const cols: Record<string, unknown> = {};
    for (const [wk, path] of Object.entries(manifest.promotes)) {
      if ((PROMOTED_FIELDS as readonly string[]).includes(wk)) cols[`p_${wk}`] = pick(path) ?? null;
    }
    if (Object.keys(cols).length === 0) return;
    const sets = Object.keys(cols).map((c) => `${c}=?`).join(", ");
    this.db.prepare(`UPDATE items SET ${sets}, updated_at=? WHERE item_id=?`)
      .run(...Object.values(cols), this.now(), itemId);
  }

  // ---------- runs: success is computed, not reported ----------
  startRun(handlerId: string, triggeredBy: string): string {
    const runId = ulid();
    this.db.prepare("INSERT INTO runs (run_id, handler_id, triggered_by, started_at) VALUES (?,?,?,?)")
      .run(runId, handlerId, triggeredBy, this.now());
    return runId;
  }

  /** Reconcile a run from stored attempts — the hub never trusts self-reported stats. */
  finishRun(runId: string): Record<string, unknown> {
    const run = this.db.prepare("SELECT handler_id, started_at FROM runs WHERE run_id=?").get(runId) as
      { handler_id: string; started_at: string } | undefined;
    if (!run) throw new Error(`unknown run ${runId}`);
    const results = this.db.prepare(
      `SELECT status, COUNT(*) AS n FROM attempts WHERE handler_id=? AND created_at>=? GROUP BY status`)
      .all(run.handler_id, run.started_at) as { status: string; n: number }[];
    const states = this.db.prepare(
      `SELECT state, COUNT(*) AS n FROM items WHERE handler_id=? GROUP BY state`)
      .all(run.handler_id) as { state: string; n: number }[];
    const stats = {
      results: Object.fromEntries(results.map((r) => [r.status, r.n])),
      items_by_state: Object.fromEntries(states.map((s) => [s.state, s.n])),
    };
    this.db.prepare("UPDATE runs SET finished_at=?, stats=? WHERE run_id=?")
      .run(this.now(), JSON.stringify(stats), runId);
    return stats;
  }

  // ---------- reporting: computed from the store, nobody files paperwork ----------
  stats(handlerId?: string): HandlerStats[] {
    const ids = handlerId
      ? [handlerId]
      : (this.db.prepare("SELECT DISTINCT handler_id AS id FROM items WHERE handler_id IS NOT NULL ORDER BY id")
          .all() as { id: string }[]).map((r) => r.id);
    return ids.map((id) => {
      const countBy = (column: string): Record<string, number> =>
        Object.fromEntries(
          (this.db.prepare(`SELECT ${column} AS k, COUNT(*) AS n FROM items WHERE handler_id=? GROUP BY ${column}`)
            .all(id) as { k: string | null; n: number }[])
            .filter((r) => r.k !== null).map((r) => [r.k as string, r.n]));
      const states = countBy("state");
      const severity = countBy("p_severity");
      // Agreement: of evaluated cases, how many were confirmed without a correction.
      const evalRows = this.db.prepare(
        `SELECT e.fields FROM evals e JOIN items i ON i.item_id = e.item_id WHERE i.handler_id=?`)
        .all(id) as { fields: string }[];
      const decided = evalRows.length;
      const correctedCases = evalRows.filter((r) =>
        Object.values(JSON.parse(r.fields) as Record<string, { grade: string }>).some((f) => f.grade === "corrected")).length;
      const doneSpans = (this.db.prepare(
        "SELECT first_seen_at AS a, updated_at AS b FROM items WHERE handler_id=? AND state='done'")
        .all(id) as { a: string; b: string }[])
        .map((r) => (Date.parse(r.b) - Date.parse(r.a)) / 3_600_000)
        .sort((x, y) => x - y);
      const weekly = this.db.prepare(
        `SELECT strftime('%Y-W%W', first_seen_at) AS week, COALESCE(p_severity, '(unclassified)') AS severity, COUNT(*) AS n
         FROM items WHERE handler_id=? AND first_seen_at >= datetime('now', '-56 days')
         GROUP BY week, severity ORDER BY week`).all(id) as { week: string; severity: string; n: number }[];
      const lastRun = this.db.prepare(
        "SELECT run_id, triggered_by, started_at, finished_at, stats FROM runs WHERE handler_id=? ORDER BY started_at DESC LIMIT 1")
        .get(id) as { run_id: string; triggered_by: string; started_at: string; finished_at: string | null; stats: string } | undefined;
      return {
        handler_id: id,
        states,
        severity,
        evals: {
          decided,
          corrected: correctedCases,
          agreement_rate: decided ? Number(((decided - correctedCases) / decided).toFixed(3)) : null,
        },
        median_hours_to_done: doneSpans.length
          ? Number(doneSpans[Math.floor(doneSpans.length / 2)].toFixed(1)) : null,
        weekly,
        last_run: lastRun ? { ...lastRun, stats: JSON.parse(lastRun.stats) as Record<string, unknown> } : null,
      };
    });
  }
}
