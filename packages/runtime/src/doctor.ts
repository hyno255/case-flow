import { execSync } from "node:child_process";
import { PROTOCOL_VERSION, type HandlerManifest, type SourceManifest, type CapabilityReport } from "@caseflow/protocol";
import { resolveAgent } from "./runner.js";

/**
 * `caseflow doctor`: resolve every agent the manifests reference, check each
 * resolved command's binary and every `requires.tools` check, and assemble
 * the capability report sent at handshake. Every failure includes a
 * suggested fix — precheck failures must never surface as mid-run errors.
 */
export interface DoctorLine { name: string; kind: "agent" | "tool"; ok: boolean; version?: string; fix?: string }

export function runDoctor(manifests: (HandlerManifest | SourceManifest)[]): DoctorLine[] {
  const lines: DoctorLine[] = [];
  const seenAgents = new Set<string>();
  const seenTools = new Set<string>();
  for (const m of manifests) {
    if ("stages" in m) {
      for (const name of referencedAgents(m)) {
        if (seenAgents.has(name)) continue;
        seenAgents.add(name);
        lines.push(checkAgent(name, m));
      }
    }
    for (const t of m.requires?.tools ?? []) {
      if (seenTools.has(t.name)) continue;
      seenTools.add(t.name);
      lines.push(checkTool(t.name, t.check, t.why));
    }
  }
  return lines;
}

/** Every agent name the handler's stages resolve to ("default" when unnamed). */
export function referencedAgents(manifest: HandlerManifest): string[] {
  const stages = [...(manifest.screen ? [manifest.screen] : []), ...manifest.stages];
  return [...new Set(stages.filter((s) => s.agent).map((s) => s.use ?? "default"))];
}

function checkAgent(name: string, manifest: HandlerManifest): DoctorLine {
  if (process.env.CASEFLOW_AGENT === "mock") {
    return { name: `agent ${name}`, kind: "agent", ok: true, version: "mock (CASEFLOW_AGENT=mock)" };
  }
  let bin: string, command: string;
  try {
    const resolved = resolveAgent(name, manifest.agents);
    bin = resolved.command[0];
    command = resolved.command.join(" ");
  } catch (e) {
    return { name: `agent ${name}`, kind: "agent", ok: false, fix: (e as Error).message };
  }
  try {
    const out = execSync(`${bin} --version`, { stdio: ["ignore", "pipe", "pipe"], timeout: 10_000 }).toString().trim();
    return { name: `agent ${name}`, kind: "agent", ok: true, version: `${command} · ${out.split("\n")[0]}` };
  } catch {
    return {
      name: `agent ${name}`, kind: "agent", ok: false,
      fix: `'${bin}' not found — install it, or rebind agents.${name} in .caseflow/config.yaml`,
    };
  }
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
  return {
    protocol: PROTOCOL_VERSION,
    runtime_id: runtimeId,
    capabilities: {
      agents: lines.filter((l) => l.kind === "agent")
        .map((l) => ({ name: l.name.replace(/^agent /, ""), ok: l.ok, detail: l.version ?? l.fix })),
      tools: lines.filter((l) => l.kind === "tool").map((l) => ({ name: l.name, ok: l.ok })),
    },
  };
}
