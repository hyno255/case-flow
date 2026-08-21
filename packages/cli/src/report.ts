import type { HandlerStats } from "@caseflow/protocol";

/**
 * Render the stats payload as a single self-contained HTML page — the
 * artifact for managers (`caseflow status --html`). No scripts, no external
 * assets: safe to email or publish on an internal page.
 */
export function renderStatusHtml(generatedAt: string, handlers: HandlerStats[]): string {
  const esc = (s: unknown) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
  const pct = (n: number | null) => (n === null ? "—" : `${Math.round(n * 100)}%`);

  const bars = (dist: Record<string, number>) => {
    const total = Object.values(dist).reduce((a, b) => a + b, 0);
    if (!total) return "<p class='muted'>no data yet</p>";
    return Object.entries(dist).sort()
      .map(([k, n]) => `<div class="bar"><span class="label">${esc(k)}</span>
        <span class="track"><span class="fill" style="width:${Math.max(2, Math.round((n / total) * 100))}%"></span></span>
        <span class="n">${Number(n)}</span></div>`).join("");
  };

  const weeklyTable = (weekly: HandlerStats["weekly"]) => {
    if (!weekly.length) return "<p class='muted'>no intake in the last 8 weeks</p>";
    const weeks = [...new Set(weekly.map((w) => w.week))];
    const sevs = [...new Set(weekly.map((w) => w.severity))].sort();
    const cell = (wk: string, sev: string) => weekly.find((w) => w.week === wk && w.severity === sev)?.n ?? "";
    return `<table><tr><th>week</th>${sevs.map((s) => `<th>${esc(s)}</th>`).join("")}</tr>
      ${weeks.map((wk) => `<tr><td>${esc(wk)}</td>${sevs.map((s) => `<td>${cell(wk, s)}</td>`).join("")}</tr>`).join("")}</table>`;
  };

  const card = (h: HandlerStats) => `
  <section>
    <h2>${esc(h.handler_id)}</h2>
    <div class="metrics">
      <div class="metric"><b>${pct(h.evals.agreement_rate)}</b><span>agreement<br>(${Number(h.evals.corrected)} of ${Number(h.evals.decided)} decisions corrected)</span></div>
      <div class="metric"><b>${h.median_hours_to_done ?? "—"}</b><span>median hours to done</span></div>
      <div class="metric"><b>${Number(h.states.needs_eval ?? 0)}</b><span>need your decision</span></div>
      <div class="metric"><b>${Number(h.states.done ?? 0)}</b><span>done</span></div>
    </div>
    <h3>Cases by state</h3>${bars(h.states)}
    <h3>Severity</h3>${bars(h.severity)}
    <h3>Intake by week</h3>${weeklyTable(h.weekly)}
    ${h.last_run ? `<p class="muted">last run ${esc(h.last_run.run_id)} by ${esc(h.last_run.triggered_by)},
      finished ${esc(h.last_run.finished_at ?? "(in progress)")}</p>` : ""}
  </section>`;

  return `<!doctype html><meta charset="utf-8"><title>Caseflow status</title>
<style>
  body{font:15px/1.5 system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#1a1a2e;background:#fff}
  h1{font-size:1.5rem} h2{margin:2rem 0 .5rem;border-bottom:2px solid #e0e0ef;padding-bottom:.3rem}
  h3{font-size:.95rem;margin:1.2rem 0 .4rem} .muted{color:#888;font-size:.85rem}
  .metrics{display:flex;gap:1rem;flex-wrap:wrap}
  .metric{flex:1;min-width:140px;background:#f4f4fb;border-radius:8px;padding:.8rem}
  .metric b{display:block;font-size:1.6rem} .metric span{font-size:.8rem;color:#555}
  .bar{display:flex;align-items:center;gap:.6rem;margin:.2rem 0}
  .label{width:9rem;font-size:.85rem;text-align:right} .n{font-size:.85rem;color:#555}
  .track{flex:1;background:#eee;border-radius:4px;height:14px}
  .fill{display:block;height:100%;background:#5b6abf;border-radius:4px}
  table{border-collapse:collapse;font-size:.85rem} td,th{border:1px solid #ddd;padding:.25rem .6rem;text-align:right}
  th:first-child,td:first-child{text-align:left}
</style>
<h1>Caseflow status</h1>
<p class="muted">generated ${esc(generatedAt)} · derived from the store (decisions, promoted fields, runs)</p>
${handlers.length ? handlers.map(card).join("") : "<p>No handlers with cases yet.</p>"}`;
}
