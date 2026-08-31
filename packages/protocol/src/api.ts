import { z } from "zod";
import { IntakeItem } from "./intake.js";
import { RUNTIME_RESULT_STATUSES, GRADES } from "./states.js";

/** Hub <-> Runtime wire protocol, version 1. Negotiated at handshake. */
export const PROTOCOL_VERSION = "1.0";

export const CapabilityReport = z.object({
  protocol: z.string(),
  runtime_id: z.string(),
  capabilities: z.object({
    /** Every agent name the runtime can resolve and spawn (including "default"). */
    agents: z.array(z.object({ name: z.string(), ok: z.boolean(), detail: z.string().optional() })).default([]),
    tools: z.array(z.object({ name: z.string(), ok: z.boolean() })).default([]),
  }),
});
export type CapabilityReport = z.infer<typeof CapabilityReport>;

export const ClaimRequest = z.object({
  runtime_id: z.string(),
  handler_id: z.string(),
  /** pipeline: claim routed/resumable cases to run stages. output: claim
   *  writing_back cases so concurrent write-back runs cannot double-execute. */
  phase: z.enum(["pipeline", "output"]).default("pipeline"),
  /** Claim exactly this case (`process --case <id>` — the one special-case parameter). */
  case_id: z.string().optional(),
  limit: z.number().int().positive().max(200).default(25),
});
export type ClaimRequest = z.infer<typeof ClaimRequest>;

export const ClaimedItem = z.object({
  item_id: z.string(),
  external_id: z.string(),
  title: z.string(),
  meta: z.record(z.unknown()),
  content: z.string().nullable(),         // pointer to the case home's source/ zone
  current_stage: z.string().nullable(),
  generation: z.number().int().default(1), // bumps when the source re-opens the case; keys the case-home dir
  prior_results: z.record(z.unknown()),   // stage_name -> latest ok result
  next_attempts: z.record(z.number()).default({}), // stage_name -> hub-authoritative next attempt number
  lease_expires_at: z.string(),
});
export type ClaimedItem = z.infer<typeof ClaimedItem>;

export const ResultSubmission = z.object({
  runtime_id: z.string(),
  item_id: z.string(),
  stage_name: z.string(),
  attempt: z.number().int().positive(),
  agent: z.string().optional(),
  prompt_hash: z.string().optional(),
  status: z.enum(RUNTIME_RESULT_STATUSES), // `lost`/`overridden` are hub-written, never accepted over the wire
  result: z.unknown().optional(),
  raw_output: z.string().optional(),      // preserved on invalid_output / agent_error
  /** Relative paths under the workspace's artifacts/ — the durable lane. Pointers only; content stays opaque. */
  artifacts: z.array(z.string()).max(200).optional(),
  duration_ms: z.number().int().nonnegative().optional(),
  log: z.string().optional(),             // relative path under the workspace's logs/ — the execution record
});
export type ResultSubmission = z.infer<typeof ResultSubmission>;

/**
 * The evaluator plugin's output contract: for each field, a verdict against
 * the reference (the human decision or instruction) and the final value.
 * The reasons/lesson become the body of the banked knowledge package.
 */
export const Judgment = z.object({
  fields: z.record(z.object({
    verdict: z.enum(["match", "corrected", "miss"]),
    value: z.unknown(),
  })),
  reasons: z.string().default(""),
  lesson: z.string().default(""),
  tags: z.array(z.string()).default([]),
});
export type Judgment = z.infer<typeof Judgment>;

/**
 * A human eval, submitted once per case. `input` empty = bare confirmation of
 * the agent's proposal; otherwise the user's decision or instruction text.
 * Fields carry the final values with their trust grades.
 */
export const EvalSubmission = z.object({
  item_id: z.string(),
  input: z.string().optional(),
  fields: z.record(z.object({
    value: z.unknown(),
    grade: z.enum(GRADES),
  })),
  reasons: z.string().optional(),
  lesson: z.string().optional(),
  user: z.string(),
});
export type EvalSubmission = z.infer<typeof EvalSubmission>;

/** Per-handler execution status, served by GET /v1/stats. Computed by the hub from stored rows. */
export interface HandlerStats {
  handler_id: string;
  states: Record<string, number>;
  severity: Record<string, number>;
  evals: { decided: number; corrected: number; agreement_rate: number | null };
  median_hours_to_done: number | null;
  weekly: { week: string; severity: string; n: number }[];
  last_run: { run_id: string; triggered_by: string; started_at: string; finished_at: string | null; stats: Record<string, unknown> } | null;
}

export const IngestRequest = z.object({
  source_id: z.string(),
  cases_root: z.string(),                 // the CLI's absolute cases dir; the hub derives content pointers from it
  items: z.array(IntakeItem).max(1000),   // clients chunk larger fetches
});
export type IngestRequest = z.infer<typeof IngestRequest>;

/** Per-item ingest outcome: the CLI uses case_id/content to place fetched content dirs. */
export interface IngestResult {
  external_id: string;
  case_id: string | null;                 // null when rejected
  generation: number;
  existed: boolean;                       // true → apply the source's on_existing policy
  content: string | null;                 // where source/ lives (pointer, may not exist yet)
  prior_meta?: Record<string, unknown>;   // existed only: the meta before this fetch (delta hooks compare against it)
  rejected?: boolean;                     // meta over the policy cap
}
