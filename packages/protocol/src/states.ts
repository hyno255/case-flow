/**
 * Case lifecycle:
 *   new -> routed -> processing -> needs_eval -> writing_back -> done
 * with visible side exits: unrouted (no matching route), dismissed (screened
 * out), and error (invalid/failed stage). Every state renders in a status
 * view — silence is never success.
 */
export const ITEM_STATES = [
  "new",
  "routed",
  "unrouted",
  "processing",
  "needs_eval",
  "writing_back",
  "done",
  "dismissed",
  "error",
] as const;
export type ItemState = (typeof ITEM_STATES)[number];

/**
 * Statuses a RUNTIME may submit. Every outcome must be reported explicitly
 * with one of these — silence is never success.
 */
export const RUNTIME_RESULT_STATUSES = [
  "ok",
  "invalid_output",
  "agent_error",
  "precheck_failed",
  "aborted",
] as const;

/**
 * Everything that can appear in stored history: the runtime statuses plus two
 * the hub writes itself — `lost` (lease expired with no result) and
 * `overridden` (a human eval corrected a field). Runtimes cannot submit these.
 */
export const RESULT_STATUSES = [...RUNTIME_RESULT_STATUSES, "lost", "overridden"] as const;
export type ResultStatus = (typeof RESULT_STATUSES)[number];

/**
 * Trust grade of a banked field, reading as what happened:
 *   approved  — a human confirmed the agent's value
 *   corrected — a human supplied the value (directly or via instruction)
 *   auto      — no human involved (reserved for the future auto tier)
 * Only approved/corrected fields feed recall and the benchmark.
 */
export const GRADES = ["approved", "corrected", "auto"] as const;
export type Grade = (typeof GRADES)[number];
