import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { loadAgentConfig } from "@caseflow/runtime";

/**
 * `caseflow agent` (no arguments): the interactive ops copilot. Launches the
 * pi runner with a platform brief and the operator skills below, so a human
 * can check health, drive fetch/process/eval, and triage failures
 * conversationally. The agent acts only through the `caseflow` verbs and the
 * hub's read API, under the user's own auth — the same surface any human
 * uses, with the same eval gate.
 *
 * Brief + skills are embedded here and materialized under .caseflow/agent/
 * on every launch (platform-owned; edits belong in this file). Because
 * interactive mode runs pi with its normal discovery, the user's own
 * project skills and AGENTS.md load alongside.
 */
const OPS_BRIEF = `# Caseflow ops brief

You are the operations copilot for Caseflow — a local-first platform that
turns streams of work into cases: sources fetch them, handler pipelines
judge them, a human decides every case (eval), decisions bank into a
knowledge corpus used for recall and benchmarking.

The hub API is at $CASEFLOW_HUB_URL (default http://127.0.0.1:7377), read
endpoints: /v1/status, /v1/stats, /v1/items/<id>, /v1/sources,
/v1/knowledge/search?q=. The CLI is \`caseflow\` — run \`caseflow --help\`
for the verb list.

Hard rules:
- Act ONLY through \`caseflow\` verbs and GET requests to the hub API.
  Never write to caseflow.db, the knowledge/ corpus, or a case home's
  source/ zone directly.
- \`caseflow eval\` is the human's decision. Only run it when the user has
  explicitly decided a specific case, and always show them the case's
  proposal first. Never eval cases in bulk.
- Report failures as they are — do not retry silently or paper over errors.
`;

const OPS_SKILLS: Record<string, string> = {
  health: `---
name: caseflow-health
description: Check that the Caseflow platform is healthy — hub up, queues moving, plugins passing doctor.
---
1. \`curl -sf $CASEFLOW_HUB_URL/v1/status\` (default http://127.0.0.1:7377) — hub reachable? Report state counts.
2. \`caseflow status\` — surface the needs-eval queue and problems.
3. For each plugin dir the user names, \`caseflow doctor <dir>\` — runner, tools, contracts.
4. Summarize: what needs the human (eval queue), what is stuck (problems, writing_back), what is idle.
`,
  operate: `---
name: caseflow-operate
description: Drive the daily Caseflow loop on request — fetch, process, status, eval — with the human deciding.
---
The loop: \`caseflow fetch <source>\` (cheap) → \`caseflow process <handler>\` (agents judge) →
\`caseflow status\` → \`caseflow eval <case> ["decision"]\` → \`caseflow recall "query"\`.

- Run fetch/process/status freely when asked; report the outcome numbers.
- Before any eval: show the case (\`curl -s $CASEFLOW_HUB_URL/v1/items/<id>\`) — its proposal
  fields and state — and confirm the user's decision for THAT case. Bare eval confirms the
  proposal; text becomes their correction. Never eval more than one case per explicit ask.
- \`caseflow eval --handler <id>\` (the bench) is safe to run when asked; it writes nothing back.
`,
  "triage-failure": `---
name: caseflow-triage-failure
description: Diagnose why a Caseflow case or run failed, from the recorded evidence.
---
1. \`caseflow status <handler> problems\` — find error/unrouted/dismissed cases.
2. \`curl -s $CASEFLOW_HUB_URL/v1/items/<case_id>\` — read state, generation, latest results.
3. The case home (\`.caseflow/cases/<case_id>/gen-<n>/\` by default) holds case.json, source/,
   context/, artifacts/ — read them for what the stage actually saw and produced.
4. Failed stage output (raw_output, stderr) is recorded on the attempt — quote the real error.
5. Explain the root cause and propose the fix (instructions edit, script fix, missing tool,
   auth). Do NOT resubmit results or mutate state; the user re-runs with
   \`caseflow process <handler> --case <id>\` once fixed.
`,
  explain: `---
name: caseflow-explain
description: Explain what is set up in this Caseflow workspace — sources, handlers, routes, and how a case flows.
---
1. \`curl -s $CASEFLOW_HUB_URL/v1/sources\` and \`/v1/stats\` — what is registered and routed.
2. Read the named plugin's yaml (source.yaml / handler.yaml) to explain its scope, stages,
   change policy (on_existing), and write-back.
3. Walk the lifecycle when asked: new → routed → processing → needs_eval → writing_back → done,
   with dismissed/unrouted/error as visible side exits.
4. Point at the repo docs (docs/*.md) for depth instead of guessing.
`,
};

export function launchOpsAgent(): void {
  if (process.env.CASEFLOW_AGENT === "mock") {
    console.error("✖ interactive agent mode needs a real runner (CASEFLOW_AGENT=mock is set)");
    process.exit(1);
  }
  const root = resolve(".caseflow", "agent");
  mkdirSync(root, { recursive: true });
  writeFileSync(join(root, "BRIEF.md"), OPS_BRIEF);
  const skillDirs: string[] = [];
  for (const [name, content] of Object.entries(OPS_SKILLS)) {
    const dir = join(root, "skills", name);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "SKILL.md"), content);
    skillDirs.push(dir);
  }

  const cfg = loadAgentConfig();
  if (cfg.command[0] !== "pi") {
    console.error(`✖ interactive mode drives pi; agent.command overrides it to '${cfg.command[0]}'.`);
    console.error(`  Launch your own agent with the brief at ${join(root, "BRIEF.md")} and skills under ${join(root, "skills")}.`);
    process.exit(1);
  }
  const args = [
    "--append-system-prompt", join(root, "BRIEF.md"),
    ...skillDirs.flatMap((d) => ["--skill", d]),
    ...(cfg.model ? ["--model", cfg.model] : []),
    ...cfg.args,
  ];
  const child = spawn("pi", args, { stdio: "inherit" });
  child.on("error", () => {
    console.error("✖ 'pi' not found — install the runner: npm i -g @earendil-works/pi-coding-agent");
    process.exit(1);
  });
  child.on("close", (code) => process.exit(code ?? 0));
}
