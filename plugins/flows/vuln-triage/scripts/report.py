#!/usr/bin/env python3
"""The flow's finish script: the report of one run, as pages in the run's output folder.

The engine runs it once, when every ticket of the run has ended. The current directory is the run's output folder.
On stdin: {"run", "flow", "trial", "started", "items": [{"id", "ended_as", "reason", "folder", "item"}]}.
It writes index.html, and one page per ticket from the report.md in the ticket's work folder.
Text a model wrote is shown as text: everything is escaped, and no link or HTML of its is kept.
"""
import html
import json
import re
import sys
from pathlib import Path

CSS = """
:root{--bg:#fbfaf7;--ink:#1d1c1a;--mute:#6b675f;--line:#dedad1;--card:#fff;--ok:#2e6b4a;--warn:#9a5b00;--bad:#a23b2d}
@media (prefers-color-scheme:dark){:root{--bg:#181715;--ink:#ece9e2;--mute:#a09b90;--line:#35322d;--card:#201f1c;--ok:#7fc79f;--warn:#e0a94a;--bad:#ec8f80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;line-height:1.2;margin:0 0 4px}h2{font-size:19px;margin:32px 0 8px}
p.sub{color:var(--mute);margin:0 0 24px}a{color:inherit}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14.5px}th{color:var(--mute);font-weight:600;font-size:13px}
.wrap{overflow-x:auto}.tag{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-size:12.5px;white-space:nowrap}
.done{color:var(--ok)}.failed,.timed_out,.over_budget,.cancelled{color:var(--bad)}
.report{background:var(--card);border:1px solid var(--line);padding:8px 20px 16px}
code{font:13.5px ui-monospace,monospace}.note{border-left:3px solid var(--warn);padding:8px 14px;background:var(--card);margin:16px 0}
"""
STATES = {"done": "Triaged", "failed": "Failed", "timed_out": "Timed out",
          "over_budget": "Stopped at cost cap", "cancelled": "Cancelled", None: "Not finished"}


def inline(text: str) -> str:
    """One line of markdown: escaped, with `code` and **bold** kept."""
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def markdown(text: str) -> str:
    """The little markdown a report.md uses: headings, lists, and paragraphs whose lines stay apart."""
    out, para, items = [], [], []

    def close() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
        para.clear()
        items.clear()

    for line in text.splitlines():
        head = re.match(r"(#{1,3}) +(.*)", line)
        if head:
            close()
            out.append(f"<h{len(head[1])}>{inline(head[2])}</h{len(head[1])}>")
        elif re.match(r"[-*] +", line):
            if para:
                close()
            items.append(inline(line[1:].strip()))
        elif line.strip():
            if items:
                close()
            para.append(inline(line))
        else:
            close()
    close()
    return "".join(out)


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def header_line(report: str, key: str) -> str:
    """The `Finding: ...` or `Fix: ...` line the report stage is asked to write."""
    found = re.search(rf"^\**{key}:\**\s*(.+)$", report, flags=re.M)
    return found.group(1).strip() if found else ""


def main() -> None:
    run = json.load(sys.stdin)
    rows, counts = "", {}
    for it in run["items"]:
        ticket, state = it["id"], STATES.get(it["ended_as"], str(it["ended_as"]).replace("_", " "))
        counts[state] = counts.get(state, 0) + 1
        file = Path(it["folder"]) / "report.md"
        report = file.read_text(errors="replace") if file.is_file() else ""
        done = it["ended_as"] == "done"
        body = f'<p class="sub"><a href="index.html">← {html.escape(run["flow"])}</a></p>'
        if not done:
            body += f'<h1>{html.escape(ticket)}</h1><div class="note"><strong>{state}.</strong> {html.escape(it["reason"])}</div>'
        if report:
            body += f'<div class="report">{markdown(report)}</div>'
        body += f'<h2>Evidence</h2><p>Work folder <code>{html.escape(it["folder"])}</code></p>'
        Path(f"{ticket}.html").write_text(page(f"{ticket} · {run['flow']}", body))
        finding = header_line(report, "Finding") if done else it["reason"]
        rows += (f'<tr><td><a href="{html.escape(ticket)}.html">{html.escape(ticket)}</a></td><td><span class="tag {html.escape(str(it["ended_as"]))}">{state}</span></td>'
                 f'<td>{inline(finding)}</td><td>{inline(header_line(report, "Fix") if done else "")}</td></tr>')
    summary = ", ".join(f"{n} {state.lower()}" for state, n in counts.items())
    body = (f'<h1>{html.escape(run["flow"])}</h1><p class="sub">{"Trial run. " if run["trial"] else ""}Run <code>{html.escape(run["run"])}</code> · '
            f'started {html.escape(run["started"] or "")} · {len(run["items"])} tickets: {summary}</p>'
            f'<div class="wrap"><table><tr><th>Ticket</th><th>State</th><th>Finding</th><th>Suggested fix</th></tr>{rows}</table></div>')
    Path("index.html").write_text(page(f"{run['flow']} · {run['run']}", body))
    print(f"wrote index.html and {len(run['items'])} ticket pages")


if __name__ == "__main__":
    main()
