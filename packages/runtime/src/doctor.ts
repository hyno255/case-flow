import { execSync } from "node:child_process";
import { PROTOCOL_VERSION, type HandlerManifest, type SourceManifest, type CapabilityReport } from "@caseflow/protocol";
import { loadAgentConfig } from "./runner.js";

/**
 * `caseflow doctor`: check the one agent runner and every `requires.tools`
 * check command, and assemble the capability report sent at handshake. Every
 * failure includes a suggested fix — precheck failures must never surface as
 * mid-run agent errors.
 */
export interface DoctorLine { name: string; kind: "runner" | "tool"; ok: boolean; version?: string; fix?: string }

export function runDoctor(manifests: (HandlerManifest | SourceManifest)[]): DoctorLine[] {
  const lines: DoctorLine[] = [checkRunner()];
  const seen = new Set<string>();
  for (const m of manifests) {
    for (const t of m.requires?.tools ?? []) {
      if (seen.has(t.name)) continue;
      seen.add(t.name);
      lines.push(checkTool(t.name, t.check, t.why));
    }
  }
  return lines;
}

/**
 * The runner check: binary present, and (for pi with a configured model) the
 * provider's credentials ready. `CASEFLOW_AGENT=mock` is always ok — the
 * synthetic backend needs nothing.
 */
function checkRunner(): DoctorLine {
  if (process.env.CASEFLOW_AGENT === "mock") {
    return { name: "agent runner", kind: "runner", ok: true, version: "mock (CASEFLOW_AGENT=mock)" };
  }
  const cfg = loadAgentConfig();
  const bin = cfg.command[0];
  let version: string;
  try {
    version = execSync(`${bin} --version`, { stdio: ["ignore", "pipe", "pipe"], timeout: 10_000 }).toString().trim().split("\n")[0];
  } catch {
    return {
      name: "agent runner", kind: "runner", ok: false,
      fix: `'${bin}' not found — install the runner (npm i -g @earendil-works/pi-coding-agent) or set agent.command in .caseflow/config.yaml`,
    };
  }
  if (bin === "pi" && cfg.model) {
    try {
      const out = execSync(`pi auth check --model ${JSON.stringify(cfg.model)} --json`,
        { stdio: ["ignore", "pipe", "pipe"], timeout: 15_000 }).toString();
      const status = (JSON.parse(out) as { status?: string }).status;
      if (status !== "ready") {
        return {
          name: "agent runner", kind: "runner", ok: false, version: `${bin} ${version}`,
          fix: `pi has no credentials for model '${cfg.model}' — set the provider's API key in your environment or pi's config`,
        };
      }
    } catch {
      return {
        name: "agent runner", kind: "runner", ok: false, version: `${bin} ${version}`,
        fix: `could not verify pi credentials for model '${cfg.model}' (pi auth check failed)`,
      };
    }
  }
  return { name: "agent runner", kind: "runner", ok: true, version: `${bin} ${version}${cfg.model ? ` · ${cfg.model}` : ""}` };
}

function checkTool(name: string, cmd?: string, why?: string): DoctorLine {
  if (!cmd) return { name, kind: "tool", ok: true };
  try {
    const out = execSync(cmd, { stdio: ["ignore", "pipe", "pipe"], timeout: 10_000 }).toString().trim();
    return { name, kind: "tool", ok: true, version: out.split("\n")[0] };
  } catch {
    return {
      name, kind: "tool", ok: false,
      fix: `install/authenticate '${name}' (check: \`${cmd}\`)${why ? ` — needed for: ${why}` : ""}`,
    };
  }
}

export function toCapabilityReport(runtimeId: string, lines: DoctorLine[]): CapabilityReport {
  const runner = lines.find((l) => l.kind === "runner");
  return {
    protocol: PROTOCOL_VERSION,
    runtime_id: runtimeId,
    capabilities: {
      runner: { ok: runner?.ok ?? false, detail: runner?.version ?? runner?.fix },
      tools: lines.filter((l) => l.kind === "tool").map((l) => ({ name: l.name, ok: l.ok })),
    },
  };
}
