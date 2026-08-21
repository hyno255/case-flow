import { z } from "zod";

/**
 * The intake contract. A source reports each case as one small line of
 * METADATA — the platform never carries content:
 *
 *   {external_id, title, meta}
 *
 * - external_id: the case's stable ID in the source system (identity is
 *   (source_id, external_id); the same tuple later is the same case).
 * - title: one human-readable line for queues.
 * - meta: small source-specific JSON (labels, author, links…) — the search
 *   seed and prompt seed. Metadata by definition, so it stays small (policy
 *   cap below), and it is the ONLY case data the hub stores.
 *
 * Full content (descriptions, attachments, logs — any size, any shape) is
 * FILES: the fetch script writes a directory per case, the platform moves it
 * into the case home's source/ zone and stores only the path. Whether a
 * changed source record re-opens a case is the source plugin's decision
 * (`on_existing` in source.yaml) — the platform only triggers and applies it.
 */
export const IntakeItem = z.object({
  external_id: z.string().min(1),
  title: z.string().min(1),
  meta: z.record(z.unknown()).default({}),
});
export type IntakeItem = z.infer<typeof IntakeItem>;

/** Policy cap for `meta` (it is metadata by definition, not content). */
export const META_CAP_BYTES = 64 * 1024;
