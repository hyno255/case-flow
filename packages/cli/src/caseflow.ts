#!/usr/bin/env node
/**
 * caseflow — the CLI control plane.
 * Surface (10 verbs): source init|add · handler init|add · route · doctor
 *                     fetch · process · status · eval · recall · agent
 * Behavior lives in plugins; deployment lives on routes; the CLI stays thin.
 */
import { Command } from "commander";
import { spawnSync, execSync } from "node:child_process";
import { readdirSync, cpSync, existsSync, writeFileSync, mkdirSync, mkdtempSync, renameSync, rmSync } from "node:fs";
import { resolve, join, basename, dirname } from "node:path";
import { hostname } from "node:os";
import {
  HubClient, startHeartbeat, loadHandlerManifest, loadSourceManifest, runDoctor, toCapabilityReport,
  processItem, runStagesLocal, proposalFields, runOutputScript, runScript, runEvaluator,
  runAgent, mockReply, orchestratorBootstrap, extractJson,
  caseHome, casesRoot, sourceDir, artifactsDir, listArtifacts, clearContext,
} from "@caseflow/runtime";
import { launchOpsAgent } from "./opsAgent.js";
import {
  writeKnowledgePackage, scanKnowledge, archivePackagesForCase, type KnowledgeFields, type IntakeItem,
  type HandlerManifest, type SourceManifest, type Grade, type OutputSchema, type Judgment,
} from "@caseflow/protocol";
import { renderStatusHtml } from "./report.js";
import { SOURCE_YAML, SOURCE_FETCH, HANDLER_YAML, HANDLER_TRIAGE_SCRIPT, HANDLER_WRITEBACK, SEED_CASES } from "./scaffold.js";
import { runMcpServer } from "./mcp.js";

const program = new Command();
const hub = new HubClient();
const runtimeId = `${hostname()}-${process.env.USER ?? "user"}`;
const user = process.env.USER ?? "user";
const knowledgeRoot = resolve(process.env.CASEFLOW_KNOWLEDGE ?? "knowledge");

program.name("caseflow")
  .description("Turn any stream of work into cases: your AI judges, you decide, every decision compounds")
  .version("1.0.0")
  .addHelpText("after", `
Typical workflow:
  setup   caseflow source init my-source        (edit source.yaml: scope lives there)
          caseflow source add ./my-source
          caseflow handler add ./my-handler     (or: handler init to scaffold one)
          caseflow route my-source team/my-handler
          caseflow doctor ./my-handler
  daily   caseflow fetch my-source              (cheap: scripts + your change policy, no AI)
          caseflow process team/my-handler      (agents judge; nothing written back)
          caseflow status                       (the queues)
          caseflow eval <case> ["your decision"]  (decide → write back → bank knowledge)
          caseflow recall "query"               (ask everything you've decided before)
          caseflow agent                        (ops copilot: health, runs, failure triage — interactive)
  measure caseflow eval --handler team/my-handler   (blind-replay banked cases, per-field scores)

The agent runner (pi by default) is configured in .caseflow/config.yaml (agent.model);
credentials stay in the runner's own auth. CASEFLOW_AGENT=mock runs everything credential-free.`);

// ---------- helpers ----------

/** Install a plugin package: local dir, git URL, or org/repo shorthand. */
function installPackage(ref: string): string {
  if (existsSync(ref)) return resolve(ref);
  const url = /^(https?:\/\/|git@)/.test(ref) ? ref
    : /^[\w.-]+\/[\w.-]+$/.test(ref) ? `https://github.com/${ref}.git` : null;
  if (!url) { console.error(`✖ '${ref}' is neither a directory nor a git reference`); process.exit(1); }
  const dest = resolve(".caseflow", "plugins", basename(ref).replace(/\.git$/, ""));
  mkdirSync(resolve(".caseflow", "plugins"), { recursive: true });
  execSync(`git clone --depth 1 ${url} ${dest}`, { stdio: "pipe" });
  return dest;
}

/** Copy a plugin's seed knowledge packages into the workspace corpus. */
function importSeedKnowledge(pluginDir: string): number {
  const src = join(pluginDir, "knowledge");
  if (!existsSync(src)) return 0;
  mkdirSync(knowledgeRoot, { recursive: true });
  let n = 0;
  for (const entry of readdirSync(src, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dest = join(knowledgeRoot, entry.name);
    if (existsSync(dest)) continue;
    cpSync(join(src, entry.name), dest, { recursive: true });
    n++;
  }
  return n;
}

async function resolveHandler(handlerId: string): Promise<{ handlerDir: string; manifest: HandlerManifest }> {
  const reg = await hub.getHandler(handlerId);
  const handlerDir = resolve(reg.package_ref);
  return { handlerDir, manifest: loadHandlerManifest(handlerDir) };
}

/** Move a fetched content dir into a case home's source/ zone (cross-volume safe). */
function placeContent(fetched: string, contentDst: string): void {
  mkdirSync(dirname(contentDst), { recursive: true });
  rmSync(contentDst, { recursive: true, force: true }); // a re-opened generation's source/ starts fresh
  try { renameSync(fetched, contentDst); }
  catch { cpSync(fetched, contentDst, { recursive: true }); rmSync(fetched, { recursive: true, force: true }); }
}

/**
 * The source's own change semantics: run its delta hook over old and new
 * content and read one verdict. The platform triggers and applies — the
 * decision (diff a field, re-read files, ask your agent) belongs to the
 * plugin.
 */
async function deltaChanged(manifest: SourceManifest, dir: string,
  old: { path: string | null; meta: Record<string, unknown> },
  next: { path: string | null; meta: Record<string, unknown> }): Promise<boolean> {
  if (!manifest.delta) { console.error(`✖ source ${manifest.id}: on_existing is 'delta' but no delta: script is declared`); process.exit(1); }
  const verdict = extractJson((await runScript(manifest.delta, dir, { old, new: next })).stdout) as { changed?: boolean } | undefined;
  if (typeof verdict?.changed !== "boolean") {
    console.error(`  ✖ delta hook printed no {"changed": true|false} verdict — treating as unchanged`);
    return false;
  }
  return verdict.changed;
}

/** Exact-checkable = enum-typed in some stage schema, or non-string value. Free strings are AI-judged. */
function isExactField(manifest: HandlerManifest, field: string, value: unknown): boolean {
  const stages = [...(manifest.screen ? [{ output_schema: manifest.screen.output_schema }] : []), ...manifest.stages];
  for (const s of stages) {
    const spec = (s.output_schema as Record<string, unknown>)[field];
    if (spec && typeof spec === "object" && "enum" in spec) return true;
  }
  return typeof value === "boolean" || typeof value === "number";
}

// ---------- source ----------
const sourceCmd = program.command("source").description("Source plugins: bring cases in (init / add)");

sourceCmd.command("init <name>")
  .description("Scaffold a source plugin — fetch script + its own scope config")
  .action((name: string) => {
    const dir = resolve(name);
    if (existsSync(dir)) { console.error(`✖ ${dir} already exists`); process.exit(1); }
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "source.yaml"), SOURCE_YAML(basename(name)));
    writeFileSync(join(dir, "fetch.sh"), SOURCE_FETCH, { mode: 0o755 });
    console.log(`✔ Created ${dir}`);
    console.log(`  Next: edit source.yaml (set repo: — scope lives in the plugin), then: caseflow source add ${name}`);
  });

sourceCmd.command("add <ref>")
  .description("Install & register a source plugin (dir, git URL, or org/repo)")
  .action(async (ref: string) => {
    const dir = installPackage(ref);
    const manifest = loadSourceManifest(dir);
    await hub.registerSource(manifest, dir);
    console.log(`✔ source ${manifest.id}`);
  });

// ---------- handler ----------
const handlerCmd = program.command("handler").description("Handler plugins: judge cases (init / add)");

handlerCmd.command("init <name>")
  .description("Scaffold a handler plugin — stage script, rubric prompt, output fields, write-back, seed knowledge")
  .action((name: string) => {
    const id = name.includes("/") ? name : `${user}/${name}`;
    const dir = resolve(basename(name));
    if (existsSync(dir)) { console.error(`✖ ${dir} already exists`); process.exit(1); }
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "handler.yaml"), HANDLER_YAML(id));
    writeFileSync(join(dir, "triage.sh"), HANDLER_TRIAGE_SCRIPT, { mode: 0o755 });
    writeFileSync(join(dir, "writeback.sh"), HANDLER_WRITEBACK, { mode: 0o755 });
    // Seed knowledge: examples that double as the first benchmark cases; their
    // source files land in evidence/source/ exactly as a live case's would.
    mkdirSync(join(dir, "knowledge"), { recursive: true });
    for (const seed of SEED_CASES(id)) {
      const pkgDir = writeKnowledgePackage(join(dir, "knowledge"), seed.pkg);
      mkdirSync(join(pkgDir, "evidence", "source"), { recursive: true });
      for (const [name, content] of Object.entries(seed.source)) {
        writeFileSync(join(pkgDir, "evidence", "source", name), content);
      }
    }
    console.log(`✔ Created ${dir} (handler.yaml, triage.sh, writeback.sh, ${SEED_CASES(id).length} seed knowledge cases)`);
    console.log(`  Next: edit the prompt in handler.yaml (your rubric) and triage.sh (your evidence), then: caseflow handler add ${basename(name)}`);
  });

handlerCmd.command("add <ref>")
  .description("Install & register a handler plugin (installs only — never an implicit bench run)")
  .action(async (ref: string) => {
    const dir = installPackage(ref);
    const manifest = loadHandlerManifest(dir);
    await hub.registerHandler(manifest, dir);
    const seeds = importSeedKnowledge(dir);
    console.log(`✔ handler ${manifest.id}${seeds ? ` (+${seeds} seed knowledge cases)` : ""}`);
    console.log(`  bench: caseflow eval --handler ${manifest.id}`);
  });

// ---------- route ----------
program.command("route <source> <handler>")
  .description("Wire a source to a handler (deterministic, first match wins)")
  .action(async (source: string, handler: string) => {
    await hub.addRoute(source, handler);
    console.log(`✔ ${source} → ${handler}`);
  });

// ---------- doctor ----------
program.command("doctor [dir]")
  .description("Check the agent runner, tools, and plugin contracts; report fixes")
  .action(async (dir) => {
    const d = resolve(dir ?? ".");
    let failed = 0;
    const manifests = [];
    if (existsSync(join(d, "handler.yaml"))) manifests.push(loadHandlerManifest(d));
    if (existsSync(join(d, "source.yaml"))) manifests.push(loadSourceManifest(d));
    if (manifests.length === 0) { console.error("✖ no handler.yaml or source.yaml here"); process.exit(1); }
    for (const l of runDoctor(manifests)) {
      if (l.ok) console.log(`✔ ${l.name}${l.version ? ` — ${l.version}` : ""}`);
      else { console.log(`✖ ${l.name}\n  → ${l.fix}`); failed++; }
    }
    // Contract checks for handler plugins.
    const hm = manifests.find((m): m is HandlerManifest => "stages" in m);
    if (hm) {
      const stages = [...(hm.screen ? [{ ...hm.screen, name: "screen" }] : []), ...hm.stages];
      for (const s of stages) {
        const mode = s.exec ? "exec" : "agent";
        const script = join(d, (s.exec ?? s.agent)!);
        if (existsSync(script)) console.log(`✔ stage ${s.name} (${mode})`);
        else { console.log(`✖ stage ${s.name}\n  → missing script ${script}`); failed++; }
        if (s.exec) continue;
        // Mock smoke: the orchestrator bootstrap/extraction path must round-trip.
        const smoke = mockReply(orchestratorBootstrap(s.name, script, s.prompt, s.output_schema as OutputSchema));
        if (extractJson(smoke.stdout) === undefined) { console.log(`✖ stage ${s.name}: mock pipeline failed`); failed++; }
      }
      if (hm.writeback) {
        const receipt = extractJson((await runScript(hm.writeback, d,
          { external_id: "X-1", title: "dry run", meta: {}, results: {} }, 15_000)).stdout) as { status?: string } | undefined;
        if (receipt?.status === "ok" || receipt?.status === "failed") console.log("✔ writeback dry-run (receipt printed)");
        else { console.log("✖ writeback dry-run\n  → script must print a receipt: {\"status\":\"ok\",\"actions\":[...]}"); failed++; }
      }
    }
    process.exit(failed ? 1 : 0);
  });

// ---------- fetch ----------
program.command("fetch <source>")
  .description("Run a source's fetch script and ingest (cheap: scripts + the source's change policy, no platform AI)")
  .allowUnknownOption(true)
  .action(async (source: string, _opts, cmd) => {
    let dir: string;
    if (existsSync(resolve(source))) dir = resolve(source);
    else {
      const { sources } = await hub.listSources();
      const reg = sources.find((s) => s.source_id === source);
      if (!reg) { console.error(`✖ unknown source '${source}' — register it: caseflow source add <dir>`); process.exit(1); }
      dir = String(reg.config.dir_ref);
    }
    const manifest = loadSourceManifest(dir);
    // Declared params default from the plugin yaml; CLI may override per call.
    const extraArgs = cmd.args.slice(1) as string[];
    const provided = new Set(extraArgs.filter((a) => a.startsWith("--")).map((a) => a.slice(2)));
    for (const [name, spec] of Object.entries(manifest.params)) {
      if (spec.default !== undefined && String(spec.default) !== "" && !provided.has(name)) {
        extraArgs.push(`--${name}`, String(spec.default));
      }
    }
    // The fetch contract: one metadata line per case on stdout
    // ({external_id, title, meta}); full content as files under --out/<external_id>/.
    mkdirSync(casesRoot(), { recursive: true });
    const outDir = mkdtempSync(join(casesRoot(), ".fetch-"));
    try {
      const res = spawnSync("bash", [manifest.run, ...extraArgs, "--out", outDir],
        { cwd: dir, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
      if (res.status !== 0) { console.error(`✖ fetch script failed (exit ${res.status}):\n${res.stderr || "(no stderr)"}`); process.exit(1); }
      const items = res.stdout.trim().split("\n").filter(Boolean).map((l) => JSON.parse(l) as IntakeItem);
      const stats: Record<string, number> = { new: 0, existing: 0, rejected: 0, routed: 0, unrouted: 0, reopened: 0 };
      for (let i = 0; i < Math.max(items.length, 1); i += 500) {
        const batch = await hub.ingest(manifest.id, casesRoot(), items.slice(i, i + 500));
        for (const k of ["new", "existing", "rejected", "routed", "unrouted"] as const) stats[k] += batch[k];
        for (const r of batch.results) {
          const fetched = resolve(outDir, r.external_id);
          if (!fetched.startsWith(outDir + "/")) { console.error(`  ✖ ${r.external_id}: unsafe id, content skipped`); continue; }
          if (r.rejected || !r.case_id) { console.error(`  ✖ ${r.external_id}: rejected (meta over cap — content belongs in files, not meta)`); continue; }
          if (!r.existed) {
            if (existsSync(fetched) && r.content) placeContent(fetched, r.content);
            continue;
          }
          // Known case: the source plugin decides what a re-fetch means.
          if (manifest.on_existing === "ignore" || !existsSync(fetched)) continue;
          if (manifest.on_existing === "delta") {
            const item = items.find((x) => x.external_id === r.external_id);
            const changed = await deltaChanged(manifest, dir,
              { path: r.content, meta: r.prior_meta ?? {} }, { path: fetched, meta: item?.meta ?? {} });
            if (!changed) continue;
          }
          const re = await hub.reopen(r.case_id, casesRoot());
          if (!re.reopened) { console.log(`  ${r.external_id}: changed but in flight — will pick up next fetch`); continue; }
          if (re.content) placeContent(fetched, re.content);
          stats.reopened++;
        }
      }
      console.log(`✔ ${manifest.id}: ${JSON.stringify(stats)}`);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  });

// ---------- process ----------
program.command("process <handler>")
  .description("Run the handler's agents over queued cases (the expensive step)")
  .option("--case <id>", "process exactly one case (re-run after a fix, debug, jump the queue)")
  .action(async (handlerId: string, opts: { case?: string }) => {
    const { handlerDir, manifest } = await resolveHandler(handlerId);
    await hub.handshake(toCapabilityReport(runtimeId, runDoctor([manifest])));
    const { run_id } = await hub.startRun(handlerId, user);
    const { items } = await hub.claim({ runtime_id: runtimeId, handler_id: handlerId, case_id: opts.case });
    console.log(`claimed ${items.length} case(s)`);
    const stopHeartbeat = startHeartbeat(hub, runtimeId);
    try {
      for (const item of items) {
        try {
          const outcome = await processItem(hub, manifest, item, { handlerDir, runtimeId });
          console.log(`  ${item.external_id}: ${outcome}`);
        } catch (e) {
          console.error(`  ${item.external_id}: ✖ ${(e as Error).message}`);
        }
      }
      // Sweep pending write-back retries (failed receipts from earlier evals).
      const retries = await hub.claim({ runtime_id: runtimeId, handler_id: handlerId, phase: "output", limit: 50 });
      for (const item of retries.items) {
        const ws = caseHome(item.item_id, item.generation);
        const record = { ...await hub.item(item.item_id), workspace: ws, artifacts: listArtifacts(ws) };
        const { ok } = await runOutputScript(hub, manifest, record, item.item_id, item.next_attempts.output ?? 1, { handlerDir, runtimeId });
        console.log(`  ${item.external_id}: write-back retry ${ok ? "ok" : "failed"}`);
      }
    } finally {
      stopHeartbeat();
      const { stats } = await hub.finishRun(run_id);
      console.log(`run ${run_id}: ${JSON.stringify(stats)}`);
    }
  });

// ---------- status ----------
program.command("status [handler] [view]")
  .description("Queues and metrics; views: needs-eval | in-flight | problems | done")
  .option("--html <file>", "write the self-contained status page (for managers)")
  .option("--format <fmt>", "jsonl exports banked cases as eval items {input, ground_truth}")
  .action(async (handlerId: string | undefined, view: string | undefined, opts: { html?: string; format?: string }) => {
    if (opts.format === "jsonl") {
      for (const pkg of scanKnowledge(knowledgeRoot, { handler: handlerId })) {
        const gt = Object.fromEntries(Object.entries(pkg.fields)
          .filter(([, f]) => f.grade === "approved" || f.grade === "corrected")
          .map(([k, f]) => [k, f.value]));
        console.log(JSON.stringify({ input: pkg.case, ground_truth: gt }));
      }
      return;
    }
    const s = await hub.status(handlerId);
    if (opts.html) {
      const { generated_at, handlers } = await hub.stats(handlerId);
      writeFileSync(opts.html, renderStatusHtml(generated_at, handlers));
      console.log(`✔ wrote ${opts.html}`);
      return;
    }
    console.log(`states: ${JSON.stringify(s.counts)}`);
    const views: Record<string, typeof s.needs_eval> = {
      "needs-eval": s.needs_eval, "in-flight": s.in_flight, problems: s.problems, done: s.done,
    };
    const show = view ? { [view]: views[view] ?? [] } : { "needs-eval": s.needs_eval, problems: s.problems };
    for (const [name, rows] of Object.entries(show)) {
      if (!rows.length) continue;
      console.log(`\n${name.toUpperCase().replace("-", " ")} (${rows.length}):`);
      for (const r of rows.slice(0, 25)) {
        const extra = r.state === "needs_eval"
          ? `[${r.p_severity ?? "?"}${r.p_owner ? ` ${r.p_owner}` : ""}]`
          : `(${r.state}${r.current_stage ? `@${r.current_stage}` : ""})`;
        console.log(`  ${r.item_id}  ${r.external_id}  ${extra}  ${r.title}`);
      }
    }
    if (s.needs_eval.length) console.log(`\ndecide: caseflow eval <case> ["your decision"]`);
    const { handlers } = await hub.stats(handlerId);
    for (const h of handlers) {
      const rate = h.evals.agreement_rate;
      console.log(`\n${h.handler_id}: ${h.evals.decided} decided, ${h.evals.corrected} corrected` +
        (rate === null ? "" : ` — agreement ${Math.round(rate * 100)}%`) +
        (h.median_hours_to_done !== null ? ` · ${h.median_hours_to_done}h median to done` : ""));
    }
  });

// ---------- eval ----------
program.command("eval [case] [input]")
  .description('Decide a case: bare = confirm the proposal; with text = your decision or an instruction. One action: decide → write back → bank knowledge')
  .option("--handler <id>", "instead: blind-replay this handler's banked cases (the benchmark)")
  .option("--sample <n>", "bench only: limit replayed cases")
  .action(async (caseId: string | undefined, input: string | undefined, opts: { handler?: string; sample?: string }) => {
    if (opts.handler) return bench(opts.handler, opts.sample ? Number(opts.sample) : undefined);
    if (!caseId) { console.error("✖ usage: caseflow eval <case> [\"decision\"]  ·  caseflow eval --handler <id>"); process.exit(1); }

    const item = await hub.item(caseId) as {
      item_id: string; external_id: string; title: string; state: string; handler_id: string;
      meta: Record<string, unknown>; source_id: string; generation: number;
      results: Record<string, unknown>;
    };
    if (item.state !== "needs_eval") { console.error(`✖ case is '${item.state}' — eval decides cases in 'needs_eval'`); process.exit(1); }
    const { handlerDir, manifest } = await resolveHandler(item.handler_id);
    const env: IntakeItem = { external_id: item.external_id, title: item.title, meta: item.meta };
    const proposal = proposalFields(item.results);

    let fields: KnowledgeFields;
    let reasons = "", lesson = "";
    if (!input) {
      // Bare confirmation: the human accepts the proposal. No evaluator spawn — one keystroke stays free.
      fields = Object.fromEntries(Object.entries(proposal).map(([k, v]) => [k, { value: v, grade: "approved" as Grade }]));
    } else {
      // The evaluator structures the human's decision into per-field verdicts.
      const judgment = await runEvaluator(manifest, handlerDir, { env, proposal, reference: input });
      fields = {};
      for (const [k, f] of Object.entries(judgment.fields)) {
        if (f.verdict === "miss") continue;
        fields[k] = { value: f.value, grade: f.verdict === "match" ? "approved" : "corrected" };
      }
      reasons = judgment.reasons; lesson = judgment.lesson;
    }

    await hub.submitEval({ item_id: item.item_id, input, fields, reasons, lesson, user });

    // Write back immediately (receipted); failures stay retryable via `process`.
    // The record carries the case-home path + artifact pointers so the script
    // can attach patches, logs, or anything else the stages produced.
    const ws = caseHome(item.item_id, item.generation);
    await hub.handshake(toCapabilityReport(runtimeId, runDoctor([manifest])));
    const { items } = await hub.claim({ runtime_id: runtimeId, handler_id: item.handler_id, phase: "output", case_id: item.item_id, limit: 1 });
    let receiptNote = "write-back pending (no claim)";
    let wroteBack = false;
    if (items.length) {
      const record = { ...await hub.item(item.item_id), workspace: ws, artifacts: listArtifacts(ws) };
      const { ok } = await runOutputScript(hub, manifest, record, item.item_id, items[0].next_attempts.output ?? 1, { handlerDir, runtimeId });
      wroteBack = ok;
      receiptNote = ok ? "written back (receipt recorded)" : "write-back FAILED — will retry on next process";
    }

    // Bank the knowledge package: the decision is the knowledge. The case home's
    // durable zones (source/ as decided, the artifacts lane) are promoted into
    // permanent evidence; an earlier generation's package for this case is
    // archived so recall/bench see the current truth.
    mkdirSync(knowledgeRoot, { recursive: true });
    const archived = archivePackagesForCase(knowledgeRoot, item.item_id);
    const path = writeKnowledgePackage(knowledgeRoot, {
      case_id: item.item_id, handler_id: item.handler_id, source_id: item.source_id,
      title: item.title, tags: [], banked_at: new Date().toISOString(),
      case: env, fields, decided_by: user, lesson, analysis: reasons,
    }, { sourceDir: sourceDir(ws), artifactsDir: artifactsDir(ws) });
    if (wroteBack) clearContext(ws); // rebuildable by contract; evidence is banked, receipts are in the hub
    console.log(`✔ ${item.external_id} ${input ? "decided" : "confirmed"} · ${receiptNote}`);
    console.log(`  banked ${path}${archived ? ` (archived ${archived} earlier generation)` : ""}`);
  });

/** The benchmark: blind-replay banked cases; ANSWER never enters the handler's context. */
async function bench(handlerId: string, sample?: number): Promise<void> {
  const { handlerDir, manifest } = await resolveHandler(handlerId);
  let pkgs = scanKnowledge(knowledgeRoot, { handler: handlerId })
    .filter((p) => Object.values(p.fields).some((f) => f.grade === "approved" || f.grade === "corrected"));
  if (sample && pkgs.length > sample) pkgs = pkgs.slice(0, sample);
  if (!pkgs.length) { console.log(`no banked cases for ${handlerId} yet — decide some with caseflow eval`); return; }

  const perField: Record<string, { match: number; total: number }> = {};
  let failures = 0;
  for (const pkg of pkgs) {
    // Fresh home per replayed case, materialized exactly like a live one: the
    // frozen source material fills ./source/. The previous pipeline's
    // artifacts and ANSWER.md never enter the handler's context — the replay
    // must earn its conclusions from the same inputs the original had.
    const ws = caseHome(`bench-${pkg.dir}`, 1);
    const frozenSource = join(knowledgeRoot, pkg.dir, "evidence", "source");
    if (existsSync(frozenSource)) cpSync(frozenSource, sourceDir(ws), { recursive: true });
    const { outputs, failed } = await runStagesLocal(manifest, pkg.case, { handlerDir, runtimeId, workspace: ws });
    if (failed) { failures++; console.log(`  ${pkg.dir}: ✖ ${failed}`); continue; }
    const proposal = proposalFields(outputs);
    const gradable = Object.entries(pkg.fields).filter(([, f]) => f.grade === "approved" || f.grade === "corrected");
    const judgedFields = gradable.filter(([k, f]) => !isExactField(manifest, k, f.value) && k in proposal);
    let judgment: Judgment | undefined;
    if (judgedFields.length) {
      const reference = `Verified answer fields: ${JSON.stringify(Object.fromEntries(gradable.map(([k, f]) => [k, f.value])))}\n` +
        `Lesson: ${pkg.lesson}\nAnalysis: ${pkg.analysis}`;
      judgment = await runEvaluator(manifest, handlerDir, { env: pkg.case, proposal, reference });
    }
    for (const [field, f] of gradable) {
      perField[field] ??= { match: 0, total: 0 };
      perField[field].total++;
      if (isExactField(manifest, field, f.value)) {
        if (proposal[field] === f.value) perField[field].match++;
      } else if (judgment?.fields[field]?.verdict === "match") {
        perField[field].match++;
      }
    }
  }
  console.log(`\nbench ${handlerId} — ${pkgs.length} case(s)${failures ? `, ${failures} failed to replay` : ""}:`);
  for (const [field, { match, total }] of Object.entries(perField)) {
    console.log(`  ${field.padEnd(16)} ${Math.round((match / total) * 100)}%  (${match}/${total})`);
  }
}

// ---------- recall ----------
program.command("recall <query>")
  .description("Ask the knowledge corpus (evaluated cases only) — also an MCP tool for agents")
  .action(async (query: string) => {
    const { results } = await hub.searchKnowledge(query);
    if (!results.length) { console.log("no matching knowledge yet"); return; }
    for (const r of results) {
      const outcome = Object.entries(r.fields).map(([k, f]) => `${k}:${String(f.value)}`).join(" ");
      console.log(`${r.dir}  (${r.handler_id})`);
      console.log(`  → ${r.lesson || "(no lesson recorded)"}`);
      console.log(`  outcome: ${outcome}\n`);
    }
  });

// ---------- agent: the ops copilot (interactive) and one-shot runner ----------
program.command("agent [prompt...]")
  .description("Talk to the ops agent — interactive without arguments; one-shot with a prompt")
  .action(async (promptWords: string[]) => {
    if (promptWords.length) {
      // One-shot: the same runner that backs agent-mode stages; exec scripts
      // and delta hooks call this for AI on demand.
      const out = await runAgent(promptWords.join(" "), { cwd: process.cwd(), timeoutMs: 300_000 });
      if (out.stdout.trim()) console.log(out.stdout.trim());
      if (out.code !== 0) { console.error(out.stderr.trim()); process.exit(out.code ?? 1); }
      return;
    }
    launchOpsAgent();
  });

// ---------- mcp (serves recall_knowledge / get_case to any MCP-speaking agent) ----------
program.command("mcp", { hidden: true })
  .description("Run the MCP server (stdio) exposing recall_knowledge and get_case")
  .action(() => runMcpServer(hub));

program.parseAsync().catch((e) => { console.error(`✖ ${e.message}`); process.exit(1); });
