import { readFileSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { parse } from "yaml";
import { HandlerManifest, SourceManifest } from "@caseflow/protocol";

export function loadHandlerManifest(dir: string): HandlerManifest {
  const raw = parse(readFileSync(join(dir, "handler.yaml"), "utf8"));
  const parsed = HandlerManifest.safeParse(raw);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `  ${i.path.join(".")}: ${i.message}`).join("\n");
    throw new Error(`handler.yaml invalid:\n${issues}\n  → see ${join(dir, "handler.yaml")}`);
  }
  return parsed.data;
}

export function loadSourceManifest(dir: string): SourceManifest {
  const raw = parse(readFileSync(join(dir, "source.yaml"), "utf8"));
  const parsed = SourceManifest.safeParse(raw);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `  ${i.path.join(".")}: ${i.message}`).join("\n");
    throw new Error(`source.yaml invalid:\n${issues}\n  → see ${join(dir, "source.yaml")}`);
  }
  return parsed.data;
}

/** Resolve a manifest-declared path — it must stay inside the plugin package. */
export function packagePath(pluginDir: string, rel: string): string {
  const root = resolve(pluginDir);
  const p = resolve(root, rel);
  if (!p.startsWith(root + sep)) throw new Error(`path escapes the plugin package: ${rel}`);
  return p;
}
