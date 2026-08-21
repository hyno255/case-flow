import Fastify from "fastify";
import {
  CapabilityReport, ClaimRequest, ResultSubmission, IngestRequest, EvalSubmission,
  HandlerManifest, SourceManifest, PROTOCOL_VERSION, validateAgainstSchema,
  searchKnowledge, type OutputSchema,
} from "@caseflow/protocol";
import { Store } from "./store.js";

/**
 * caseflow-hub — the headless service that owns state, routing, validation,
 * and the eval gate. The hub is the source of truth: every result is
 * re-validated server-side; runtimes are untrusted for validation. Knowledge
 * packages live in git; the hub only searches them (rebuildable by scan).
 */
export function buildServer(store: Store, knowledgeRoot = process.env.CASEFLOW_KNOWLEDGE ?? "knowledge") {
  const app = Fastify({ logger: false, bodyLimit: 8 * 1024 * 1024 });
  const capabilities = new Map<string, CapabilityReport>(); // runtime_id -> report

  app.post("/v1/handshake", async (req, reply) => {
    const parsed = CapabilityReport.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    const major = parsed.data.protocol.split(".")[0];
    if (major !== PROTOCOL_VERSION.split(".")[0]) {
      return reply.code(426).send({ error: `unsupported protocol ${parsed.data.protocol}; hub speaks ${PROTOCOL_VERSION}` });
    }
    capabilities.set(parsed.data.runtime_id, parsed.data);
    return { accepted: PROTOCOL_VERSION };
  });

  // Intake is metadata-only; content stays files under the CLI's cases root.
  // Per-item results tell the CLI where to place fetched content and which
  // known cases to run its source's on_existing policy against.
  app.post("/v1/ingest", async (req, reply) => {
    const parsed = IngestRequest.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    const results = parsed.data.items.map((item) => store.ingest(parsed.data.source_id, item, parsed.data.cases_root));
    const routing = store.routeNewItems();
    return {
      new: results.filter((r) => !r.existed && !r.rejected).length,
      existing: results.filter((r) => r.existed).length,
      rejected: results.filter((r) => r.rejected).length,
      ...routing,
      results,
    };
  });

  // The source plugin's verdict that new source material supersedes the old
  // (on_existing: replace, or a delta hook answering changed=true).
  app.post("/v1/cases/:id/reopen", async (req, reply) => {
    const { id } = req.params as { id: string };
    const { cases_root } = req.body as { cases_root?: string };
    if (typeof cases_root !== "string") return reply.code(400).send({ error: "cases_root required" });
    try { return store.reopen(id, cases_root); }
    catch { return reply.code(404).send({ error: "case not found" }); }
  });

  app.post("/v1/handlers", async (req, reply) => {
    const body = req.body as { manifest: unknown; package_ref: string };
    const parsed = HandlerManifest.safeParse(body.manifest);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    store.registerHandler(parsed.data, body.package_ref);
    return { registered: parsed.data.id, version: parsed.data.version };
  });

  app.get("/v1/handlers/:team/:name", async (req, reply) => {
    const { team, name } = req.params as { team: string; name: string };
    const h = store.getHandler(`${team}/${name}`);
    return h ?? reply.code(404).send({ error: "handler not found" });
  });

  app.post("/v1/sources", async (req, reply) => {
    const body = req.body as { manifest: unknown; dir_ref: string };
    const parsed = SourceManifest.safeParse(body.manifest);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    store.registerSource(parsed.data, body.dir_ref);
    return { registered: parsed.data.id };
  });

  app.get("/v1/sources", async () => ({ sources: store.listSources() }));

  app.post("/v1/routes", async (req, reply) => {
    const { source_id, handler_id } = req.body as { source_id?: string; handler_id?: string };
    if (typeof source_id !== "string" || typeof handler_id !== "string") {
      return reply.code(400).send({ error: "expected { source_id, handler_id }" });
    }
    if (!store.listSources().some((s) => s.source_id === source_id)) {
      return reply.code(404).send({ error: `source '${source_id}' not registered` });
    }
    if (!store.getHandler(handler_id)) {
      return reply.code(404).send({ error: `handler '${handler_id}' not registered` });
    }
    store.addRoute(source_id, handler_id);
    return { ok: true };
  });

  app.post("/v1/claims", async (req, reply) => {
    const parsed = ClaimRequest.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    const { runtime_id, handler_id, phase, limit, case_id } = parsed.data;
    const manifest = store.getHandler(handler_id);
    if (!manifest) return reply.code(404).send({ error: "handler not found" });
    // Capability matching: a runtime may only claim cases whose requires it satisfies.
    const caps = capabilities.get(runtime_id);
    if (!caps) return reply.code(412).send({ error: "handshake first" });
    const missing = missingRequirements(manifest, caps);
    if (missing.length) return reply.code(412).send({ error: `runtime lacks: ${missing.join(", ")}` });
    const { items, leaseExpiresAt } = store.claim(runtime_id, handler_id, limit, phase, case_id);
    return {
      items: items.map((i) => ({
        item_id: i.item_id, external_id: i.external_id, title: i.title,
        meta: JSON.parse(i.meta), content: i.content, current_stage: i.current_stage,
        generation: i.generation,
        prior_results: store.latestResults(i.item_id),
        next_attempts: store.nextAttempts(i.item_id),
        lease_expires_at: leaseExpiresAt,
      })),
    };
  });

  app.post("/v1/heartbeat", async (req, reply) => {
    const { runtime_id } = req.body as { runtime_id?: string };
    if (typeof runtime_id !== "string") return reply.code(400).send({ error: "runtime_id required" });
    return { extended: store.heartbeat(runtime_id) };
  });

  app.post("/v1/results", async (req, reply) => {
    const parsed = ResultSubmission.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    const r = parsed.data;
    const item = store.getItem(r.item_id);
    if (!item) return reply.code(404).send({ error: "item not found" });
    const manifest = item.handler_id ? store.getHandler(item.handler_id) : undefined;
    // A result for a stage the manifest doesn't declare must never advance the
    // state machine — reject it before anything is recorded.
    if (manifest && !isKnownStage(manifest, r.stage_name)) {
      return reply.code(400).send({ error: `unknown stage '${r.stage_name}' for ${item.handler_id}` });
    }
    // A network-level retry of an already-recorded submission is answered with
    // the STORED verdict before any state guard — replays are always safe.
    const existing = store.db.prepare(
      "SELECT status FROM attempts WHERE item_id=? AND stage_name=? AND attempt=?")
      .get(r.item_id, r.stage_name, r.attempt) as { status: string } | undefined;
    if (existing) return { ok: true, idempotent_replay: true, recorded_status: existing.status };
    // Only the runtime holding a live lease may submit — a stale claimant's
    // late result is rejected instead of corrupting a resumed case.
    if (!store.hasLiveClaim(r.item_id, r.runtime_id)) {
      return reply.code(409).send({ error: "no live claim on this case for this runtime; re-claim first" });
    }
    // State preconditions: results cannot skip the eval gate or re-open
    // settled cases. Output receipts only while writing_back; stage results
    // only while processing.
    const requiredState = r.stage_name === "output" ? "writing_back" : "processing";
    if (item.state !== requiredState) {
      return reply.code(409).send({ error: `case is '${item.state}'; '${r.stage_name}' results are only accepted in '${requiredState}'` });
    }

    // Server-side validation against the manifest schema — the hub never trusts runtime validation.
    let status = r.status;
    if (status === "ok" && manifest) {
      const schema = stageSchema(manifest, r.stage_name);
      if (schema && validateAgainstSchema(schema, r.result).length) status = "invalid_output";
    }
    const inserted = store.recordResult({
      itemId: r.item_id, handlerId: item.handler_id, handlerVersion: item.handler_version,
      stage: r.stage_name, attempt: r.attempt, agent: r.agent, promptHash: r.prompt_hash,
      status, result: r.result, rawOutput: r.raw_output, artifacts: r.artifacts,
    });
    if (!inserted) return { ok: true, idempotent_replay: true, recorded_status: status };

    if (manifest) advance(store, item.item_id, manifest, r.stage_name, status, r.result);
    return { ok: true, recorded_status: status };
  });

  // The human decision: record the eval, apply corrections, move to write-back.
  app.post("/v1/evals", async (req, reply) => {
    const parsed = EvalSubmission.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: parsed.error.flatten() });
    const item = store.getItem(parsed.data.item_id);
    if (!item) return reply.code(404).send({ error: "case not found" });
    if (item.state !== "needs_eval") {
      return reply.code(409).send({ error: `case is '${item.state}'; eval accepts cases in 'needs_eval'` });
    }
    store.applyEval(parsed.data);
    return { ok: true };
  });

  // Recall's grep: lexical search over the knowledge packages (evaluated cases only).
  app.get("/v1/knowledge/search", async (req) => {
    const { q, handler, k } = req.query as { q?: string; handler?: string; k?: string };
    const results = searchKnowledge(knowledgeRoot, q ?? "", Math.min(Number(k ?? 5) || 5, 20), handler)
      .filter((p) => Object.values(p.fields).some((f) => f.grade === "approved" || f.grade === "corrected"))
      .map((p) => ({
        dir: p.dir, title: p.title, handler_id: p.handler_id, tags: p.tags,
        lesson: p.lesson, fields: p.fields, banked_at: p.banked_at, score: p.score,
      }));
    return { results };
  });

  app.get("/v1/status", async (req) => {
    const { handler_id } = req.query as { handler_id?: string };
    const where = handler_id ? "AND handler_id=?" : "";
    const args = handler_id ? [handler_id] : [];
    const counts = store.db.prepare(
      `SELECT state, COUNT(*) AS n FROM items WHERE 1=1 ${where} GROUP BY state`).all(...args) as { state: string; n: number }[];
    const list = (states: string) => store.db.prepare(
      `SELECT item_id, external_id, title, handler_id, state, current_stage, p_severity, p_owner, first_seen_at FROM items
       WHERE state IN (${states}) ${where} ORDER BY first_seen_at ASC LIMIT 100`).all(...args);
    return {
      counts: Object.fromEntries(counts.map((c) => [c.state, c.n])),
      needs_eval: list("'needs_eval'"),
      in_flight: list("'routed','processing','writing_back'"),
      problems: list("'unrouted','error','dismissed'"),
      done: list("'done'"),
    };
  });

  app.get("/v1/stats", async (req) => {
    const { handler_id } = req.query as { handler_id?: string };
    return { generated_at: store.now(), handlers: store.stats(handler_id) };
  });

  app.post("/v1/runs", async (req, reply) => {
    const { handler_id, triggered_by } = req.body as { handler_id?: string; triggered_by?: string };
    if (typeof handler_id !== "string") return reply.code(400).send({ error: "handler_id required" });
    return { run_id: store.startRun(handler_id, triggered_by ?? "user") };
  });

  app.post("/v1/runs/:id/finish", async (req, reply) => {
    const { id } = req.params as { id: string };
    try { return { run_id: id, stats: store.finishRun(id) }; }
    catch (e) { return reply.code(404).send({ error: String(e) }); }
  });

  app.get("/v1/items/:id", async (req, reply) => {
    const { id } = req.params as { id: string };
    const item = store.getItem(id);
    if (!item) return reply.code(404).send({ error: "not found" });
    return {
      ...item, meta: JSON.parse(item.meta),
      results: store.latestResults(id), artifacts: store.itemArtifacts(id),
    };
  });

  return app;
}

function isKnownStage(manifest: HandlerManifest, stage: string): boolean {
  if (stage === "output") return true;
  if (stage === "screen") return manifest.screen !== undefined;
  return manifest.stages.some((s) => s.name === stage);
}

function stageSchema(manifest: HandlerManifest, stage: string): OutputSchema | undefined {
  if (stage === "screen") return manifest.screen?.output_schema as OutputSchema | undefined;
  if (stage === "output") return { status: { enum: ["ok", "failed"] } } as OutputSchema; // the write-back receipt contract
  return manifest.stages.find((s) => s.name === stage)?.output_schema as OutputSchema | undefined;
}

function missingRequirements(manifest: HandlerManifest, caps: CapabilityReport): string[] {
  const missing: string[] = [];
  const stages = [...(manifest.screen ? [manifest.screen] : []), ...manifest.stages];
  if (stages.some((s) => s.agent) && !caps.capabilities.runner.ok) {
    missing.push(`agent runner (${caps.capabilities.runner.detail ?? "not reported"})`);
  }
  for (const t of manifest.requires?.tools ?? []) {
    const have = caps.capabilities.tools.find((x) => x.name === t.name);
    if (!have?.ok) missing.push(`tool:${t.name}`);
  }
  return missing;
}

/** Advance the case state machine after a recorded result. */
function advance(store: Store, itemId: string, manifest: HandlerManifest, stage: string, status: string, result: unknown): void {
  if (stage === "output") {
    // Write-back: a failed or missing receipt keeps the case in writing_back
    // so the next sweep retries exactly the failures (receipt rows hold the
    // history). Done only on an ok receipt.
    const receiptOk = status === "ok" && (result as { status?: string } | undefined)?.status === "ok";
    if (receiptOk) {
      store.setItemState(itemId, "done", null);
      store.promote(itemId, manifest);
    } else {
      store.setItemState(itemId, "writing_back", "output");
    }
    store.releaseClaims(itemId);
    return;
  }
  if (status !== "ok") {
    if (status === "invalid_output" || status === "agent_error") store.setItemState(itemId, "error", stage);
    store.releaseClaims(itemId);
    return;
  }
  if (stage === "screen") {
    const r = result as { worth_triaging?: boolean };
    if (r.worth_triaging === false) { store.setItemState(itemId, "dismissed", null); store.releaseClaims(itemId); return; }
    store.setItemState(itemId, "processing", manifest.stages[0]?.name ?? null);
    return;
  }
  const idx = manifest.stages.findIndex((s) => s.name === stage);
  const next = manifest.stages[idx + 1];
  if (next) { store.setItemState(itemId, "processing", next.name); return; }
  // Last stage done -> promote fields; every case waits for a human eval.
  store.promote(itemId, manifest);
  store.setItemState(itemId, "needs_eval", null);
  store.releaseClaims(itemId);
}

// Entrypoint
const isMain = process.argv[1]?.endsWith("server.ts") || process.argv[1]?.endsWith("server.js");
if (isMain) {
  const store = new Store();
  const app = buildServer(store);
  const port = Number(process.env.CASEFLOW_PORT ?? 7377);
  app.listen({ port, host: "127.0.0.1" }).then(() => {
    console.log(`caseflow-hub listening on http://127.0.0.1:${port} (protocol v${PROTOCOL_VERSION})`);
  });
}
