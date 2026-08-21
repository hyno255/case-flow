import { z } from "zod";

/** Capability precheck entry. `check` is a shell command; exit 0 = satisfied. */
export const RequireCheck = z.object({
  name: z.string(),
  check: z.string().optional(),
  why: z.string().optional(),
});

export const Requires = z.object({
  tools: z.array(RequireCheck).default([]),
}).partial().default({});

const FieldSpecSchema: z.ZodType<unknown> = z.union([
  z.enum(["string", "number", "boolean"]),
  z.object({ enum: z.array(z.string()) }),
  z.object({ type: z.enum(["string", "number", "boolean"]), optional: z.boolean().optional() }),
]);

/**
 * Every stage is a user script; the key chooses its EXECUTOR — exactly one:
 *   exec: <script>   bash runs it directly — deterministic, no AI
 *                    (cwd = case workspace, case record on stdin, JSON on stdout)
 *   agent: <script>  the configured agent runs it as orchestrator — it executes
 *                    the script in the case workspace, reports or flags any
 *                    issue, and answers with the stage's JSON. `prompt` injects
 *                    the plugin's own guidance into that orchestrator.
 */
const stageMode = {
  agent: z.string().optional(),
  exec: z.string().optional(),
  prompt: z.string().optional(),        // agent executor only: appended to the orchestrator prompt
  output_schema: z.record(FieldSpecSchema),
};
const exactlyOneMode = (s: { agent?: string; exec?: string }) => !!s.agent !== !!s.exec;
const MODE_MESSAGE = { message: "a stage declares exactly one of agent: or exec:" };

export const ScreenDef = z.object(stageMode).refine(exactlyOneMode, MODE_MESSAGE);
export const StageDef = z.object({ name: z.string(), ...stageMode }).refine(exactlyOneMode, MODE_MESSAGE);

/**
 * Handler manifest — the contract between a handler plugin and the platform.
 * Behavior lives here; deployment posture (routing) lives on routes.
 * Every case waits for a human eval — there is no review-mode config.
 */
export const HandlerManifest = z.object({
  id: z.string().regex(/^[a-z0-9-]+\/[a-z0-9-]+$/, "expected team/name"),
  version: z.string(),
  owners: z.array(z.string()).default([]),
  requires: Requires,
  screen: ScreenDef.optional(),                      // cheap gate; screened-out cases are dismissed (kept, visible)
  stages: z.array(StageDef).min(1),
  writeback: z.string().optional(),                  // script: stdin case record -> stdout receipt
  evaluator: z.string().optional(),                  // evaluator instructions file; default = first-party
  /** well-known field -> "stage.field"; promoted into indexed columns for status/reporting */
  promotes: z.record(z.string()).default({}),
});
export type HandlerManifest = z.infer<typeof HandlerManifest>;
export type StageDef = z.infer<typeof StageDef>;

/**
 * Source manifest — a source plugin: fetch script + its own scope + its own
 * change semantics. Scope (repo, project, team…) is a param with a default
 * set here — never a CLI flag. `on_existing` says what a re-fetched known
 * case means: nothing (ignore), always re-open (replace), or ask the
 * declared delta hook — a script (which may invoke your agent) that reads
 * old and new content and answers {"changed": true|false}. The platform
 * only triggers the hook and applies the verdict.
 */
export const SourceManifest = z.object({
  id: z.string(),
  owners: z.array(z.string()).default([]),
  run: z.string(),
  params: z.record(z.object({
    default: z.union([z.string(), z.number(), z.boolean()]).optional(),
    optional: z.boolean().optional(),
  })).default({}),
  on_existing: z.enum(["ignore", "replace", "delta"]).default("ignore"),
  delta: z.string().optional(),        // required when on_existing: delta
  requires: Requires,
});
export type SourceManifest = z.infer<typeof SourceManifest>;

export const PROMOTED_FIELDS = ["severity", "owner", "disposition", "confidence"] as const;
