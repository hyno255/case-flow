"""The renderer: one index page per run and one page per item, built from the work folders and the store.

Model-written text is treated as untrusted: raw HTML in report.md is shown as text, and links keep only http(s) targets.
"""
import html
import re
from pathlib import Path

import markdown

from .layout import REPORT, visible_files
from .status import ENDINGS, cost_text, label



def ending(name: str | None) -> str:
    """The word a reader of the report sees for how an item ended."""
    text = {"done": "triaged", None: "not finished"}.get(name) or label(ENDINGS, name)
    return text[:1].upper() + text[1:]

CSS = """
:root{--bg:#fbfaf7;--ink:#1d1c1a;--mute:#6b675f;--line:#dedad1;--card:#fff;--ok:#2e6b4a;--warn:#9a5b00;--bad:#a23b2d}
@media (prefers-color-scheme:dark){:root{--bg:#181715;--ink:#ece9e2;--mute:#a09b90;--line:#35322d;--card:#201f1c;--ok:#7fc79f;--warn:#e0a94a;--bad:#ec8f80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;line-height:1.2;margin:0 0 4px}h2{font-size:19px;margin:32px 0 8px}
p.sub{color:var(--mute);margin:0 0 24px}a{color:inherit}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14.5px}th{color:var(--mute);font-weight:600;font-size:13px}
.wrap{overflow-x:auto}.tag{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-size:12.5px;white-space:nowrap}
.done{color:var(--ok)}.needs_person,.carried_over{color:var(--warn)}.failed,.timed_out,.over_budget,.cancelled{color:var(--bad)}
.report{background:var(--card);border:1px solid var(--line);padding:8px 20px 16px}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{font:13.5px ui-monospace,monospace}.note{border-left:3px solid var(--warn);padding:8px 14px;background:var(--card);margin:16px 0}
"""


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def md_to_html(text: str) -> str:
    md = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br"])  # nl2br keeps the Finding and Fix lines apart
    md.preprocessors.deregister("html_block")  # raw HTML in a report is shown as text, never run
    md.inlinePatterns.deregister("html")
    out = md.convert(text)
    return re.sub(r'(href|src)="(?!https?://)[^"]*"', r'\1="#"', out)


def header_line(report: str, key: str) -> str:
    m = re.search(rf"^\**{key}:\**\s*(.+)$", report, flags=re.M)
    return m.group(1).strip() if m else ""


def inline(text: str) -> str:
    """Escaped text for a table cell, with `code` spans kept as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))


def item_facts(store, row) -> dict:
    """Where an item's files and numbers really live: its own run, or the earlier run it was carried over from."""
    origin_run = row["carried_from"] or row["run_id"]
    origin = store.item(origin_run, row["item_id"]) if row["carried_from"] else row
    folder = store.path(row)
    report = (folder / "report.md").read_text() if (folder / "report.md").is_file() else ""
    return {"id": row["item_id"], "ended_as": row["ended_as"], "origin_as": origin["ended_as"], "origin_run": origin_run,
            "reason": row["reason"] or "", "folder": folder, "report": report,
            "finding": header_line(report, "Finding"), "fix": header_line(report, "Fix")}


def render_item(store, flow: str, f: dict) -> str:
    run, item = f["origin_run"], f["id"]
    body = f'<p class="sub"><a href="index.html">← {html.escape(flow)}</a></p>'
    if f["ended_as"] == "carried_over":
        body += f'<div class="note">Carried over: this item has not changed since run <code>{html.escape(run)}</code>, so it was not triaged again.</div>'
    if f["origin_as"] != "done":
        body += f'<h1>{html.escape(item)}</h1><div class="note"><strong>{ending(f["origin_as"])}.</strong> {html.escape(f["reason"])}</div>'
    if f["report"]:
        body += f'<div class="report">{md_to_html(f["report"])}</div>'
    rows = ""
    for s in store.q("SELECT stage, MIN(by) by, COUNT(*) tries FROM attempts WHERE run_id = ? AND item_id = ? GROUP BY stage ORDER BY MIN(id)", run, item):
        u = store.usage(run, item, s["stage"])
        last = store.one("SELECT by, passed, reason FROM verdicts WHERE run_id = ? AND item_id = ? AND stage = ? ORDER BY id DESC LIMIT 1", run, item, s["stage"])
        verdict = f'{"pass" if last["passed"] else "fail"} ({html.escape(last["by"])}): {html.escape(last["reason"])}' if last else "no validation"
        rows += (f'<tr><td>{html.escape(s["stage"])}</td><td>{html.escape(s["by"])}</td><td class="num">{s["tries"]}</td><td class="num">{u["tool_calls"]}</td>'
                 f'<td class="num">{u["tokens"]:,}</td><td class="num">{cost_text(u, " est.")}</td><td class="num">{u["seconds"]:.1f}s</td><td>{verdict}</td></tr>')
    body += ('<h2>How this was produced</h2><div class="wrap"><table><tr><th>Stage</th><th>Done by</th><th class="num">Tries</th><th class="num">Tool calls</th>'
             f'<th class="num">Tokens</th><th class="num">Cost</th><th class="num">Time</th><th>Last verdict</th></tr>{rows}</table></div>')
    files = ", ".join(f"<code>{html.escape(p)}</code>" for p in visible_files(f["folder"])) if f["folder"].is_dir() else ""
    body += f'<h2>Evidence</h2><p>Work folder <code>{html.escape(str(f["folder"]))}</code>: {files}. Every attempt and verdict is kept beside them.</p>'
    return page(f"{item} · {flow}", body)


def summary_counts(facts: list[dict]) -> dict:
    c = {"total": len(facts), "triaged": 0, "needs_person": 0, "carried_over": 0, "not_finished": 0}
    for f in facts:
        key = {"done": "triaged", "needs_person": "needs_person", "carried_over": "carried_over"}.get(f["ended_as"], "not_finished")
        c[key] += 1
    return c


def summary_text(c: dict) -> str:
    parts = [f"{c['triaged']} triaged", f"{c['needs_person']} need a person", f"{c['carried_over']} carried over"]
    if c["not_finished"]:
        parts.append(f"{c['not_finished']} not finished")
    return f"{c['total']} items: " + ", ".join(parts)


def render_run(store, run_id: str) -> tuple[Path, dict]:
    """Writes <run folder>/report/. Returns (index path, counts)."""
    run = store.run(run_id)
    out = store.path(run) / REPORT
    out.mkdir(parents=True, exist_ok=True)
    facts = [item_facts(store, r) for r in store.items(run_id)]
    counts, u = summary_counts(facts), store.usage(run_id)
    rows = ""
    for f in facts:
        (out / f"{f['id']}.html").write_text(render_item(store, run["flow"], f))
        state = ending(f["ended_as"])
        if f["ended_as"] == "carried_over":
            state += f" · {ending(f['origin_as']).lower()}"
        finding = f["finding"] if f["origin_as"] == "done" and f["finding"] else f["reason"]
        rows += (f'<tr><td><a href="{html.escape(f["id"])}.html">{html.escape(f["id"])}</a></td><td><span class="tag {f["ended_as"]}">{html.escape(state)}</span></td>'
                 f'<td>{inline(finding)}</td><td>{inline(f["fix"] if f["origin_as"] == "done" else "")}</td></tr>')
    kind = "Trial run. " if run["trial"] else ""
    body = (f'<h1>{html.escape(run["flow"])}</h1><p class="sub">{kind}Run <code>{html.escape(run_id)}</code> · started {html.escape(run["started"] or "")} · '
            f'{summary_text(counts)} · {u["tokens"]:,} tokens · {cost_text(u, " est.")}</p>'
            f'<div class="wrap"><table><tr><th>Item</th><th>State</th><th>Finding</th><th>Suggested fix</th></tr>{rows}</table></div>')
    (out / "index.html").write_text(page(f"{run['flow']} · {run_id}", body))
    return out / "index.html", counts
