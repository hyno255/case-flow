/**
 * Server state-machine test, driven through the HTTP API via fastify inject
 * (no socket). Covers claim ownership + state preconditions, server-side
 * validation downgrades, the eval gate, write-back retry, screen dismissal,
 * and knowledge search over a real package on disk.
 */
import { rmSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert";
import { writeKnowledgePackage, archivePackagesForCase, scanKnowledge } from "@caseflow/protocol";
import { Store } from "./store.js";
import { buildServer } from "./server.js";

const DB = "/tmp/caseflow-server-test.db";
rmSync(DB, { force: true });
const store = new Store(DB);
const knowledgeRoot = mkdtempSync(join(tmpdir(), "caseflow-knowledge-"));
const app = buildServer(store, knowledgeRoot);

const post = async (url: string, payload: object) => {
  const res = await app.inject({ method: "POST", url, payload });
  return { code: res.statusCode, body: JSON.parse(res.body) as Record<string, unknown> };
};
const get = async (url: string) => JSON.parse((await app.inject({ method: "GET", url })).body) as Record<string, unknown>;
const claim = async (runtime: string, opts: object = {}) =>
  (await post("/v1/claims", { runtime_id: runtime, handler_id: "team/h", ...opts })).body as
    { items: { item_id: string; next_attempts: Record<string, number> }[] };
const submit = (runtime: string, id: string, stage: string, attempt: number, extra: object) =>
  post("/v1/results", { runtime_id: runtime, item_id: id, stage_name: stage, attempt, ...extra });

const manifest = {
  id: "team/h", version: "1.0.0",
  screen: { agent: "./screen.sh", output_schema: { worth_triaging: "boolean", reason: "string" } },
  stages: [{ name: "triage", agent: "./triage.sh",
    output_schema: { severity: { enum: ["low", "high"] }, owner: "string" } }],
  promotes: { severity: "triage.severity" },
};

// ---- handshake: protocol version gate
assert.equal((await post("/v1/handshake", { protocol: "2.0", runtime_id: "rt", capabilities: { runner: { ok: true }, tools: [] } })).code, 426);
assert.equal((await post("/v1/handshake", { protocol: "1.0", runtime_id: "rt", capabilities: { runner: { ok: true }, tools: [] } })).code, 200);
await post("/v1/handshake", { protocol: "1.0", runtime_id: "intruder", capabilities: { runner: { ok: true }, tools: [] } });

// ---- register + route (unknown source/handler refused) + ingest
await post("/v1/handlers", { manifest, package_ref: "/tmp/pkg" });
assert.equal((await post("/v1/routes", { source_id: "ghost", handler_id: "team/h" })).code, 404);
assert.equal((await post("/v1/routes", { source_id: "src", handler_id: "team/ghost" })).code, 404);
await post("/v1/sources", { manifest: { id: "src", run: "./fetch.sh" }, dir_ref: "/tmp/src" });
await post("/v1/routes", { source_id: "src", handler_id: "team/h" });
const ing = (await post("/v1/ingest", { source_id: "src", cases_root: "/tmp/caseflow-server-test-cases", items: [
  { external_id: "A-1", title: "crash on login", meta: { labels: ["bug"] } },
  { external_id: "A-2", title: "noise", meta: {} },
] })).body as { routed: number; new: number; results: { external_id: string; case_id: string; content: string }[] };
assert.equal(ing.routed, 2);
assert.equal(ing.new, 2);
assert.ok(ing.results[0].content.endsWith(`/${ing.results[0].case_id}/gen-1/source`)); // the pointer the CLI fills

// ---- claims: capability-gated; results need a live claim in the right state
assert.equal((await post("/v1/claims", { runtime_id: "stranger", handler_id: "team/h" })).code, 412); // no handshake
const c1 = await claim("rt");
assert.equal(c1.items.length, 2);
const [id, id2] = [c1.items[0].item_id, c1.items[1].item_id];
// unknown stage: rejected outright
assert.equal((await submit("rt", id, "typo-stage", 1, { status: "ok", result: {} })).code, 400);
// hub-internal statuses cannot come over the wire
assert.equal((await submit("rt", id, "triage", 1, { status: "overridden", result: {} })).code, 400);
// no claim, no submission
assert.equal((await submit("intruder", id, "triage", 1, { status: "ok", result: { severity: "high", owner: "a" } })).code, 409);

// ---- screen dismissal path: kept + visible in problems
await submit("rt", id2, "screen", 1, { status: "ok", result: { worth_triaging: false, reason: "spam" } });
assert.equal(store.getItem(id2)!.state, "dismissed");

// ---- server-side validation downgrades off-schema "ok"
await submit("rt", id, "screen", 1, { status: "ok", result: { worth_triaging: true, reason: "r" } });
const bad = (await submit("rt", id, "triage", 1, { status: "ok", result: { severity: "NOT_IN_ENUM", owner: "a" } })).body;
assert.equal(bad.recorded_status, "invalid_output");
assert.equal(store.getItem(id)!.state, "error");
// error cases accept no results
assert.equal((await submit("rt", id, "triage", 2, { status: "ok", result: { severity: "high", owner: "a" } })).code, 409);

// ---- recover: re-route and walk to the eval gate
store.setItemState(id, "routed", null);
const c2 = await claim("rt");
assert.equal(c2.items[0].next_attempts.triage, 2); // hub-negotiated numbering survives the failure
await submit("rt", id, "screen", c2.items[0].next_attempts.screen ?? 2, { status: "ok", result: { worth_triaging: true, reason: "r" } });
const good = (await submit("rt", id, "triage", 2, { status: "ok", result: { severity: "high", owner: "alice" }, artifacts: ["analysis.md"] })).body;
assert.equal(good.recorded_status, "ok");
assert.equal(store.getItem(id)!.state, "needs_eval"); // every case waits for a human
assert.equal(store.getItem(id)!.p_severity, "high");
// idempotent replay answers with the STORED verdict even after the state moved on
const replay = (await submit("rt", id, "triage", 2, { status: "ok", result: { severity: "low", owner: "x" } })).body;
assert.equal(replay.idempotent_replay, true);
assert.equal(replay.recorded_status, "ok");

// ---- eval: the human decision (409 outside needs_eval; corrections applied)
assert.equal((await post("/v1/evals", { item_id: id2, fields: {}, user: "t" })).code, 409); // dismissed, not needs_eval
assert.equal((await post("/v1/evals", { item_id: "nope", fields: {}, user: "t" })).code, 404);
await post("/v1/evals", {
  item_id: id, input: "low actually",
  fields: { severity: { value: "low", grade: "corrected" }, owner: { value: "alice", grade: "approved" } },
  user: "tester",
});
assert.equal(store.getItem(id)!.state, "writing_back");
assert.equal(store.getItem(id)!.p_severity, "low"); // human value promotes immediately

// ---- write-back under output-phase claims: failed receipt stays retryable
const o1 = await claim("rt", { phase: "output" });
assert.equal(o1.items.length, 1);
assert.equal((await claim("intruder", { phase: "output" })).items.length, 0); // live lease blocks
await submit("rt", id, "output", o1.items[0].next_attempts.output ?? 1, { status: "ok", result: { status: "failed", error: "tracker 503" } });
assert.equal(store.getItem(id)!.state, "writing_back"); // NOT stranded
const o2 = await claim("rt", { phase: "output" });
assert.equal(o2.items[0].next_attempts.output, 2);      // retry targets exactly this failure
await submit("rt", id, "output", 2, { status: "ok", result: { status: "ok", actions: ["labeled"] } });
assert.equal(store.getItem(id)!.state, "done");

// ---- eval on done: refused (write-back already executed)
assert.equal((await post("/v1/evals", { item_id: id, fields: {}, user: "t" })).code, 409);

// ---- status views cover every state
const status = (await get("/v1/status")) as { counts: Record<string, number>; problems: { item_id: string }[] };
assert.equal(status.counts.done, 1);
assert.equal(status.problems.length, 1); // the dismissed case is visible, not silent

// ---- artifacts round-trip: pointers land on the case record
const rec = (await get(`/v1/items/${id}`)) as { artifacts: string[]; generation: number; meta: Record<string, unknown> };
assert.deepEqual(rec.artifacts, ["analysis.md"]);
assert.equal(rec.generation, 1);
assert.deepEqual(rec.meta, { labels: ["bug"] });

// ---- reopen over the API: the source's verdict — gen bump, pointer swing, re-queued
const ro = (await post(`/v1/cases/${id}/reopen`, { cases_root: "/tmp/caseflow-server-test-cases" })).body as
  { reopened: boolean; generation: number; content: string };
assert.equal(ro.reopened, true);
assert.equal(ro.generation, 2);
assert.ok(ro.content.endsWith(`/${id}/gen-2/source`));
assert.equal(store.getItem(id)!.state, "routed");
assert.equal((await post("/v1/cases/nope/reopen", { cases_root: "/x" })).code, 404);
store.setItemState(id, "done", null); // restore for the knowledge assertions below

// ---- knowledge search: serves evaluated packages from disk (rebuildable scan)
writeKnowledgePackage(knowledgeRoot, {
  case_id: id, handler_id: "team/h", source_id: "src",
  title: "crash on login", tags: ["auth"], banked_at: new Date().toISOString(),
  case: { external_id: "A-1", title: "crash on login", meta: { labels: ["bug"] } },
  fields: { severity: { value: "low", grade: "corrected" } },
  decided_by: "tester", lesson: "login crashes are severity low here", analysis: "test",
});
const hits = (await get("/v1/knowledge/search?q=login+crash")) as { results: { title: string; lesson: string }[] };
assert.equal(hits.results.length, 1);
assert.ok(hits.results[0].lesson.includes("login"));

// ---- archive-on-rebank: a new generation's package supersedes; history stays on disk
assert.equal(archivePackagesForCase(knowledgeRoot, id), 1);
writeKnowledgePackage(knowledgeRoot, {
  case_id: id, handler_id: "team/h", source_id: "src",
  title: "crash on login", tags: ["auth"], banked_at: new Date(Date.now() + 1000).toISOString(),
  case: { external_id: "A-1", title: "crash on login", meta: { labels: ["bug", "p1"] } },
  fields: { severity: { value: "high", grade: "corrected" } },
  decided_by: "tester", lesson: "gen-2 lesson", analysis: "updated",
});
const active = scanKnowledge(knowledgeRoot);
assert.equal(active.length, 1);                       // recall/bench see only the current truth
assert.equal(active[0].lesson, "gen-2 lesson");
assert.equal(scanKnowledge(knowledgeRoot, { includeArchived: true }).length, 2); // nothing deleted

await app.close();
console.log("✔ server test passed");
