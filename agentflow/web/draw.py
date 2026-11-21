"""A flow drawn from its compiled graph: boxes and arrows with every position worked out here, so the template only draws.

One column, top to bottom: the source the flow accepts (with when it is fetched and which items are taken), each stage
in the order written (one done once for the run says so), then the run's output. A pass edge is a solid arrow,
a fail edge a dashed one: a loop at the right of the stage when it tries again itself, an arc up the left side when
`on_fail` names an earlier stage. A pass edge that skips stages arcs down the left side.
"""
from agentflow.plugins.graph import END, compile_flow, stage_by, validated
from .show import where_text

W, GAP, X0, LINE = 230, 34, 10, 15   # box width, space between boxes, margin, height of a line of text
SKIP, LOOP, BACK, NOTE = 40, 34, 56, 92  # how far a skipping arc, a loop and the shortest back arc swing out; room for a label beside an arc


def _cut(text, n: int = 32) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def graph(flow: dict, limits: dict, source: dict | None = None, stats: dict | None = None, base: str | None = "") -> dict:
    """`limits` are the ones the flow runs under (for a stage's tries). `source` is the flow's source as loaded (for the file it writes). `stats` adds a run's numbers: by stage id, or ":end" for the run's output,
    {state, lines, href}. `base` goes before each stage's `#stage-<id>` link; None draws the stages without links."""
    g, stats = compile_flow(flow, limits)[0], stats or {}
    order = g.order
    row = {sid: i + 1 for i, sid in enumerate(order)}
    row[END] = len(order) + 1
    span = lambda e: abs(row[e.dst] - row[e.src])
    skips = [e for e in g.edges if e.on == "pass" and row[e.dst] - row[e.src] > 1]
    backs = [e for e in g.edges if e.on == "fail" and e.dst != e.src]
    loops = [e for e in g.edges if e.on == "fail" and e.dst == e.src]
    swing = lambda e: (SKIP + 16 * (span(e) - 2)) if e.on == "pass" else (BACK + 22 * (span(e) - 1))  # a longer way swings further out
    left = X0 + max([0.75 * swing(e) + 4 for e in skips] + [0.75 * swing(e) + NOTE for e in backs] + [0])
    right = (0.75 * LOOP + 6 + NOTE) if loops else X0
    nodes: list[dict] = []
    y = X0

    def node(key: str, kicker: str, title: str, lines: list, href: str = "") -> dict:
        nonlocal y
        st = stats.get(key) or {}
        lines = [_cut(line) for line in [*lines, *(st.get("lines") or [])] if line]
        n = {"id": key, "x": round(left), "y": y, "w": W, "h": 44 + (LINE * len(lines) + 6 if lines else 0), "kicker": kicker, "title": _cut(title, 24),
             "tip": str(title), "lines": lines, "href": st.get("href") or href, "state": st.get("state", "")}
        y += n["h"] + GAP
        nodes.append(n)
        return n

    src = flow.get("source") or ""
    taken = where_text(flow.get("where"))
    node(":source", "source", src or "no source", [str(flow.get("schedule") or "started by hand"), (source or {}).get("output"), f"where {taken}" if taken else ""],
         f"/sources/{src}" if src else "")  # ":" keeps it apart from stage ids
    for i, (sid, s) in enumerate(g.stages.items(), 1):
        checks = " + ".join(filter(None, ["check" if "check" in s else "", "review" if "review" in s or "done_when" in s else ""]))
        node(sid, f"{i} · {'agent' if 'agent' in s else stage_by(s)}{' · once' if s.get('once') else ''}", sid, [s.get("run") or s.get("agent"), checks], f"{base}#stage-{sid}" if base is not None else "")
    node(":end", "end", "output", [])
    at = {n["id"]: n for n in nodes}
    at[END] = at[":end"]
    mid = left + W / 2
    edges = []

    def edge(src: str, dst: str, on: str, kind: str, d: str, label: str = "", lx: float = 0, ly: float = 0, anchor: str = "start") -> None:
        edges.append({"src": src, "dst": dst if dst != END else ":end", "on": on, "kind": kind, "d": d, "label": label, "lx": round(lx), "ly": round(ly), "anchor": anchor})

    def down(a: dict, b: dict) -> str:
        return f"M{mid},{a['y'] + a['h']} V{b['y'] - 3}"

    if order:
        edge(":source", order[0], "pass", "next", down(at[":source"], at[order[0]]))
    for e in g.edges:
        a, b = at[e.src], at[e.dst]
        if e.on == "pass" and e not in skips:
            edge(e.src, e.dst, "pass", "next", down(a, b))
        elif e.on == "pass":  # down the left side, from one box's left edge to the other's
            out, y1, y2 = left - swing(e), a["y"] + a["h"] * 0.35, b["y"] + b["h"] * 0.35
            edge(e.src, e.dst, "pass", "skip", f"M{left},{y1} C{out},{y1} {out},{y2} {left - 3},{y2}")
        else:
            text = f"fail · up to {g.tries(e.src)}"
            if e.dst == e.src:  # the same stage tries again: a loop at its right
                x, out, y1, y2 = left + W, left + W + LOOP, a["y"] + a["h"] * 0.3, a["y"] + a["h"] * 0.7
                edge(e.src, e.dst, "fail", "loop", f"M{x},{y1} C{out},{y1} {out},{y2} {x + 3},{y2}", text, x + 0.75 * LOOP + 6, a["y"] + a["h"] / 2 + 3)
            else:  # back to an earlier stage, up the left side
                out, y1, y2 = left - swing(e), a["y"] + a["h"] * 0.65, b["y"] + b["h"] * 0.65
                edge(e.src, e.dst, "fail", "back", f"M{left},{y1} C{out},{y1} {out},{y2} {left - 3},{y2}", text, left - 0.75 * swing(e) - 6, (y1 + y2) / 2 + 3, "end")
    return {"nodes": nodes, "edges": edges, "width": round(left + W + right), "height": y - GAP + X0, "line": LINE,
            "validated": any(validated(s) for s in g.stages.values())}
