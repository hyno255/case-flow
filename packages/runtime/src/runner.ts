import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import type { AgentDef } from "@caseflow/protocol";

/**
 * User-owned agents. An agent is `{command?, prompt?}`: the full command line
 * (the prompt is appended as its final argument; reply on stdout) and its
 * standing guidance. Definitions live in the deployment config
 * (.caseflow/config.yaml `agents:`) and in plugin manifests; resolution is
 * deployment → plugin → built-in pi default, so the machine that holds the
 * auth always has the last word. The platform spawns commands and reads
 * stdout — it never calls a model API and never holds a credential.
 * `CASEFLOW_AGENT=mock` swaps every agent for the synthetic backend.
 */
export interface RunnerResult {
  stdout: string;
  stderr: string;
  code: number | null;
  /** True when the invocation was killed at timeoutMs — partial output must not be trusted. */
  timedOut?: boolean;
}

export interface ResolvedAgent {
  name: string;
  command: string[];
  prompt?: string;
}

/** The built-in default: pi, one-shot, hermetic. Override by defining agents.default. */
export const DEFAULT_PI_COMMAND = "pi -p --no-session --no-context-files --no-skills --no-extensions";

export function loadDeploymentAgents(path = process.env.CASEFLOW_CONFIG ?? ".caseflow/config.yaml"): Record<string, AgentDef> {
  const file = resolve(path);
  if (!existsSync(file)) return {};
  const raw = (parse(readFileSync(file, "utf8")) ?? {}) as { agents?: Record<string, AgentDef> };
  return raw.agents ?? {};
}

/** Deployment definition wins over the plugin's; "default" always resolves. */
export function resolveAgent(name: string | undefined, pluginAgents: Record<string, AgentDef> = {}): ResolvedAgent {
  const n = name ?? "default";
  const def = loadDeploymentAgents()[n] ?? pluginAgents[n] ?? (n === "default" ? {} : undefined);
  if (!def) {
    throw new Error(`agent '${n}' is not defined — add agents.${n} to .caseflow/config.yaml or the plugin manifest`);
  }
  return { name: n, command: (def.command ?? DEFAULT_PI_COMMAND).split(/\s+/), prompt: def.prompt };
}

export function runAgent(
  prompt: string,
  opts: { cwd: string; timeoutMs: number; agent: ResolvedAgent; sessionFile?: string },
): Promise<RunnerResult> {
  if (process.env.CASEFLOW_AGENT === "mock") return Promise.resolve(mockReply(prompt));
  let args = opts.agent.command.slice(1);
  // Execution tracking: pi can record its full tool-call transcript. Only for
  // pi — the platform does not inject flags into commands it doesn't know.
  if (opts.sessionFile && opts.agent.command[0] === "pi") {
    args = args.filter((a) => a !== "--no-session").concat("--session", opts.sessionFile);
  }
  args.push(prompt);
  return new Promise((res) => {
    // detached: the child gets its own process group, so a timeout kill takes
    // the agent's own subprocesses with it instead of orphaning them.
    const child = spawn(opts.agent.command[0], args, { cwd: opts.cwd, stdio: ["pipe", "pipe", "pipe"], detached: true });
    let stdout = "", stderr = "", timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { process.kill(-child.pid!, "SIGKILL"); } catch { child.kill("SIGKILL"); }
    }, opts.timeoutMs);
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.stdin.on("error", () => { /* child exited before reading stdin */ });
    child.on("close", (code) => { clearTimeout(timer); res({ stdout, stderr, code, timedOut }); });
    child.on("error", (err) => { clearTimeout(timer); res({ stdout, stderr: String(err), code: null, timedOut }); });
    child.stdin.end();
  });
}

/**
 * The mock backend: synthesizes credential-free replies so the whole loop
 * (process, eval, bench, doctor smoke) runs end-to-end without a real agent.
 */
export function mockReply(prompt: string): RunnerResult {
  // Evaluator prompts: echo the proposal back as an all-match judgment.
  const pm = prompt.match(/--- PROPOSAL[^\n]*---\n([\s\S]*?)\n--- REFERENCE/);
  if (pm) {
    try {
      const proposal = JSON.parse(pm[1]) as Record<string, unknown>;
      const fields = Object.fromEntries(Object.entries(proposal)
        .map(([k, v]) => [k, { verdict: "match", value: v }]));
      return {
        stdout: JSON.stringify({ fields, reasons: "mock evaluation", lesson: "mock lesson", tags: ["mock"] }),
        stderr: "", code: 0,
      };
    } catch { /* fall through */ }
  }
  // Stage prompts: synthesize a schema-conforming result from the output contract.
  const cm = prompt.indexOf("JSON object matching: ");
  if (cm >= 0) {
    try {
      const schema = JSON.parse(prompt.slice(cm + "JSON object matching: ".length).split("\n")[0]) as Record<string, unknown>;
      const out: Record<string, unknown> = {};
      for (const [k, spec] of Object.entries(schema)) {
        if (spec === "string") out[k] = `mock ${k}`;
        else if (spec === "number") out[k] = 0.95;
        else if (spec === "boolean") out[k] = true;
        else if (spec && typeof spec === "object" && "enum" in (spec as object)) out[k] = (spec as { enum: string[] }).enum[0];
        else if (spec && typeof spec === "object" && "type" in (spec as object)) {
          const t = (spec as { type: string }).type;
          out[k] = t === "number" ? 0.95 : t === "boolean" ? true : `mock ${k}`;
        }
      }
      return { stdout: JSON.stringify(out), stderr: "", code: 0 };
    } catch { /* fall through */ }
  }
  return { stdout: '{"error": "mock agent found no parsable output contract"}', stderr: "", code: 0 };
}
