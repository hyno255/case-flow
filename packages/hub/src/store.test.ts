/**
 * Store test: metadata-only intake, reopen (the source's verdict applied),
 * routing by source, claims & leases, attempt negotiation, evals (the human
 * decision), promotion, stats.
 * Run: npm test (tsx, throwaway SQLite db — no server needed).
 */
import { rmSync } from "node:fs";
import assert from "node:assert";
import { Store, LEASE_MS } from "./store.js";
import { HandlerManifest, SourceManifest } from "@caseflow/protocol";

const DB = "/tmp/caseflow-test.db";
const CASES = "/tmp/caseflow-test-cases";
rmSync(DB, { force: true });
const store = new Store(DB);

// ---- intake: metadata only — new rows get a content pointer, known
// identities get a metadata refresh and nothing else (re-opening is the
// source plugin's call, not intake's)
const first = store.ingest("src", { external_id: "A-1", title: "t", meta: { labels: ["bug"] } }, CASES);
assert.equal(first.existed, false);
assert.equal(first.generation, 1);
assert.equal(first.content, `${CASES}/${first.case_id}/gen-1/source`);
const again = store.ingest("src", { external_id: "A-1", title: "t2", meta: { labels: ["bug", "p1"] } }, CASES);
assert.equal(again.existed, true);
assert.equal(again.case_id, first.case_id);
assert.equal(again.generation, 1);                               // no silent re-open
assert.deepEqual(again.prior_meta, { labels: ["bug"] });         // delta hooks compare against what the hub knew
assert.equal(store.getItem(first.case_id!)!.title, "t2");        // metadata refresh is unconditional
assert.equal(store.getItem(first.case_id!)!.state, "new");
const big = store.ingest("src", { external_id: "A-2", title: "big", meta: { blob: "x".repeat(70_000) } }, CASES);
assert.equal(big.rejected, true);                                // meta is metadata, not content

// ---- routing: deterministic by source, first match; addRoute re-routes unrouted
const manifest = HandlerManifest.parse({
  id: "team/h", version: "1.0.0",
  stages: [{ name: "triage", agent: "./triage.sh",
    output_schema: { severity: { enum: ["low", "high"] }, owner: "string" } }],
  promotes: { severity: "triage.severity", owner: "triage.owner" },
});
store.registerHandler(manifest, "/tmp/pkg");
store.registerSource(SourceManifest.parse({ id: "src", run: "./fetch.sh" }), "/tmp/src");
assert.equal(store.routeNewItems().unrouted, 1); // no route yet
store.addRoute("src", "team/h");                 // re-news unrouted + routes
assert.equal(store.getItemBySource("src", "A-1")!.state, "routed");

// ---- claim: pipeline phase, lease held, single-case claim, output phase empty
const { items: claimed } = store.claim("rt-1", "team/h", 10);
assert.equal(claimed.length, 1);
const itemId = claimed[0].item_id;
assert.equal(store.claim("rt-2", "team/h", 10).items.length, 0); // live lease blocks
assert.equal(store.claim("rt-1", "team/h", 10, "output").items.length, 0); // nothing writing_back
assert.ok(store.hasLiveClaim(itemId, "rt-1"));
assert.ok(!store.hasLiveClaim(itemId, "rt-2"));

// ---- results: attempt negotiation + idempotent replay + promote
assert.deepEqual(store.nextAttempts(itemId), {});
store.recordResult({ itemId, handlerId: "team/h", handlerVersion: "1.0.0", stage: "triage", attempt: 1, status: "ok", result: { severity: "high", owner: "alice" } });
assert.equal(store.nextAttempt(itemId, "triage"), 2);
assert.equal(store.recordResult({ itemId, handlerId: "team/h", handlerVersion: "1.0.0", stage: "triage", attempt: 1, status: "ok", result: {} }), false); // replay
store.promote(itemId, manifest);
assert.equal(store.getItem(itemId)!.p_severity, "high");

// ---- eval: the human decision — corrections win, state moves to writing_back
store.setItemState(itemId, "needs_eval", null);
store.releaseClaims(itemId);
store.applyEval({
  item_id: itemId, input: "actually low; and infra owns it",
  fields: {
    severity: { value: "low", grade: "corrected" },
    owner: { value: "infra", grade: "corrected" },
  },
  user: "tester",
});
assert.equal(store.getItem(itemId)!.state, "writing_back");
const results = store.latestResults(itemId) as { triage: { severity: string; owner: string } };
assert.equal(results.triage.severity, "low");   // human value wins in latest results
assert.equal(results.triage.owner, "infra");
assert.equal(store.getItem(itemId)!.p_severity, "low"); // and in promoted columns

// ---- output claims now see the case; done via receipt is server-side (see server.test)
assert.equal(store.claim("rt-1", "team/h", 10, "output").items.length, 1);
store.releaseClaims(itemId);

// ---- leases: expiry releases the claim AND records an explicit 'lost' row
store.setItemState(itemId, "routed", null);
store.claim("rt-1", "team/h", 10);
store.db.prepare("UPDATE claims SET lease_expires_at=? WHERE released_at IS NULL")
  .run(new Date(Date.now() - LEASE_MS).toISOString());
const { items: reclaimed } = store.claim("rt-2", "team/h", 10);
assert.equal(reclaimed.length, 1);
const lost = store.db.prepare("SELECT COUNT(*) AS n FROM attempts WHERE item_id=? AND status='lost'").get(itemId) as { n: number };
assert.ok(lost.n >= 1); // silence is never success
store.releaseClaims(itemId);

// ---- reopen: the source plugin's "this changed" verdict — generation bump,
// content pointer swings to the fresh gen, case re-enters the pipeline.
// Refused only while a runtime holds a live lease.
store.setItemState(itemId, "done", null);
assert.equal(store.getItem(itemId)!.generation, 1);
const re = store.reopen(itemId, CASES);
assert.equal(re.reopened, true);
assert.equal(re.generation, 2);
assert.equal(re.content, `${CASES}/${itemId}/gen-2/source`);
assert.equal(store.getItem(itemId)!.state, "routed");
assert.equal(store.getItem(itemId)!.content, re.content);
store.claim("rt-1", "team/h", 10);                       // now processing under a live lease
const held = store.reopen(itemId, CASES);
assert.equal(held.reopened, false);                      // in-flight pass finishes against its own generation
assert.equal(store.getItem(itemId)!.generation, 2);
store.releaseClaims(itemId);

// ---- artifacts: pointers recorded on attempts, unioned per case
store.recordResult({ itemId, handlerId: "team/h", handlerVersion: "1.0.0", stage: "triage",
  attempt: store.nextAttempt(itemId, "triage"), status: "ok",
  result: { severity: "low", owner: "infra" }, artifacts: ["diff.patch", "logs/build.log"] });
assert.deepEqual(store.itemArtifacts(itemId).sort(), ["diff.patch", "logs/build.log"]);

store.setItemState(itemId, "routed", null);

// ---- stats: decided/corrected/agreement from evals
const [hs] = store.stats("team/h");
assert.equal(hs.evals.decided, 1);
assert.equal(hs.evals.corrected, 1);
assert.equal(hs.evals.agreement_rate, 0);
assert.equal(store.stats().length, 1);

console.log("✔ store test passed");
