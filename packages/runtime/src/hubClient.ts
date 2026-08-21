import type {
  CapabilityReport, ClaimedItem, ResultSubmission, EvalSubmission, HandlerStats, KnowledgeFields,
  IngestResult,
} from "@caseflow/protocol";

export interface IngestResponse {
  new: number; existing: number; rejected: number; routed: number; unrouted: number;
  results: IngestResult[];
}

/** Thin typed client for the hub protocol. Anything speaking this API is a valid runtime. */
export class HubClient {
  constructor(readonly baseUrl = process.env.CASEFLOW_HUB_URL ?? "http://127.0.0.1:7377") {}

  private async request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await res.json()) as T & { error?: unknown };
    if (!res.ok) throw new Error(`${path} -> ${res.status}: ${JSON.stringify(data.error ?? data)}`);
    return data;
  }
  private post<T>(path: string, body: unknown) { return this.request<T>("POST", path, body); }
  private get<T>(path: string) { return this.request<T>("GET", path); }

  handshake(report: CapabilityReport) { return this.post<{ accepted: string }>("/v1/handshake", report); }
  ingest(sourceId: string, casesRoot: string, items: unknown[]) {
    return this.post<IngestResponse>("/v1/ingest", { source_id: sourceId, cases_root: casesRoot, items });
  }
  reopen(caseId: string, casesRoot: string) {
    return this.post<{ reopened: boolean; generation: number; content: string | null }>(
      `/v1/cases/${caseId}/reopen`, { cases_root: casesRoot });
  }
  registerHandler(manifest: unknown, packageRef: string) { return this.post("/v1/handlers", { manifest, package_ref: packageRef }); }
  getHandler(id: string) { return this.get<{ package_ref: string }>(`/v1/handlers/${id}`); }
  registerSource(manifest: unknown, dirRef: string) { return this.post("/v1/sources", { manifest, dir_ref: dirRef }); }
  listSources() { return this.get<{ sources: { source_id: string; config: Record<string, unknown> }[] }>("/v1/sources"); }
  addRoute(sourceId: string, handlerId: string) { return this.post("/v1/routes", { source_id: sourceId, handler_id: handlerId }); }
  claim(req: { runtime_id: string; handler_id: string; phase?: "pipeline" | "output"; case_id?: string; limit?: number }) {
    return this.post<{ items: ClaimedItem[] }>("/v1/claims", req);
  }
  heartbeat(runtimeId: string) { return this.post<{ extended: number }>("/v1/heartbeat", { runtime_id: runtimeId }); }
  submitResult(r: ResultSubmission) { return this.post<{ ok: boolean; recorded_status: string }>("/v1/results", r); }
  submitEval(e: EvalSubmission) { return this.post<{ ok: boolean }>("/v1/evals", e); }
  startRun(handlerId: string, triggeredBy = "user") { return this.post<{ run_id: string }>("/v1/runs", { handler_id: handlerId, triggered_by: triggeredBy }); }
  finishRun(runId: string) { return this.post<{ stats: Record<string, unknown> }>(`/v1/runs/${runId}/finish`, {}); }
  status(handlerId?: string) {
    return this.get<{
      counts: Record<string, number>;
      needs_eval: StatusRow[]; in_flight: StatusRow[]; problems: StatusRow[]; done: StatusRow[];
    }>(`/v1/status${handlerId ? `?handler_id=${encodeURIComponent(handlerId)}` : ""}`);
  }
  stats(handlerId?: string) {
    return this.get<{ generated_at: string; handlers: HandlerStats[] }>(
      `/v1/stats${handlerId ? `?handler_id=${encodeURIComponent(handlerId)}` : ""}`);
  }
  searchKnowledge(query: string, k = 5, handler?: string) {
    const params = new URLSearchParams({ q: query, k: String(k) });
    if (handler) params.set("handler", handler);
    return this.get<{ results: KnowledgeHit[] }>(`/v1/knowledge/search?${params}`);
  }
  item(id: string) { return this.get<Record<string, unknown>>(`/v1/items/${id}`); }
}

export interface StatusRow {
  item_id: string; external_id: string; title: string; handler_id: string | null;
  state: string; current_stage: string | null; p_severity: string | null; p_owner: string | null;
  first_seen_at: string;
}

export interface KnowledgeHit {
  dir: string; title: string; handler_id: string; tags: string[];
  lesson: string; fields: KnowledgeFields; banked_at: string; score: number;
}

/**
 * Keep leases alive during long agent runs.
 * Returns a stop function; always call it in a finally block.
 */
export function startHeartbeat(hub: HubClient, runtimeId: string, intervalMs = 60_000): () => void {
  const timer = setInterval(() => { hub.heartbeat(runtimeId).catch(() => { /* hub restart: lease expiry will recover */ }); }, intervalMs);
  timer.unref?.();
  return () => clearInterval(timer);
}
