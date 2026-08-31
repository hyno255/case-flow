import { mkdirSync, writeFileSync, readdirSync, existsSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import type { IntakeItem } from "@caseflow/protocol";

/**
 * The case home: one directory per case × generation, three zones with
 * distinct ownership and lifecycle:
 *
 *   <cases>/<case_id>/gen-<n>/
 *   ├── case.json     platform-written: intake metadata + prior results — read-only by convention
 *   ├── source/       source-authored full content (any size, any shape) — written at fetch,
 *   │                 read-only after; the hub stores only a pointer to it
 *   ├── context/      plugin territory: repo worktrees, build dirs, scratch — REBUILDABLE, wiped freely
 *   ├── artifacts/    the durable lane: patches, analysis — pointer-tracked, fed to
 *   │                 write-back, promoted to knowledge evidence at eval
 *   └── logs/         platform-written execution record, one file per stage attempt
 *                     (command, timing, exit, full output) — never wiped, promoted to evidence
 *
 * All stages of one processing pass share the home (stage 2 can read the diff
 * stage 1 produced); the generation bump gives a re-opened case its own
 * directory, so two versions never share files and concurrent cases never
 * collide. Durable truth lives elsewhere — receipts in the hub, evidence in
 * knowledge — so everything except un-banked source/ is reproducible.
 */
export function casesRoot(): string {
  return resolve(process.env.CASEFLOW_CASES ?? ".caseflow/cases");
}

export function caseHome(caseId: string, generation: number, root = casesRoot()): string {
  const home = join(root, caseId, `gen-${generation}`);
  for (const zone of ["source", "context", "artifacts", "logs"]) mkdirSync(join(home, zone), { recursive: true });
  return home;
}

export function writeCaseJson(home: string, item: IntakeItem, prior: Record<string, unknown>): void {
  writeFileSync(join(home, "case.json"), JSON.stringify({ ...item, prior_results: prior }, null, 2));
}

export function sourceDir(home: string): string {
  return join(home, "source");
}

export function artifactsDir(home: string): string {
  return join(home, "artifacts");
}

export function logsDir(home: string): string {
  return join(home, "logs");
}

/** Relative paths of everything in the artifacts lane (capped; pointers only). */
export function listArtifacts(home: string, cap = 200): string[] {
  const root = artifactsDir(home);
  if (!existsSync(root)) return [];
  const out: string[] = [];
  const walk = (dir: string, prefix: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (out.length >= cap) return;
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(join(dir, entry.name), rel);
      else out.push(rel);
    }
  };
  walk(root, "");
  return out.sort();
}

/** Wipe the rebuildable zone — called once a case is done; costs nothing to lose. */
export function clearContext(home: string): void {
  rmSync(join(home, "context"), { recursive: true, force: true });
}
