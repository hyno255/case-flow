"""A flow compiled into a graph: stages are nodes, and each way from one stage to another is an edge.

flow.yaml lists stages in order. Compiling adds what the list leaves unsaid and checks the result, the way
LangGraph compiles a StateGraph before it will run one:

    pass edge   a stage whose work is accepted goes to `next`: by default the stage written after it,
                and the last stage goes to the end. `next: end` ends the item early.
    fail edge   a stage whose validation is not accepted goes to `on_fail`: by default itself, so the
                same stage tries again with the reason. Naming an earlier stage makes a loop through
                several stages: that stage runs again with the reason, and the item works its way back.

Pass edges must lead to the end without a cycle. Loops exist only on fail edges, and every fail edge is
bounded by the `tries` of the stage it leaves, so a compiled graph cannot run for ever.

The same graph is what the engine walks, what a resumed item is placed on, and what the pages draw.
"""
from dataclasses import dataclass

END = "end"
VALIDATION = ("check", "done_when", "review")  # the keys that give a stage something that can fail


def stage_by(stage: dict) -> str:
    """Who does a stage: a script, a named agent, or the default agent following the stage's own file."""
    return "script" if "run" in stage else stage.get("agent", "default agent")


def validated(stage: dict) -> bool:
    return any(k in stage for k in VALIDATION)


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    on: str  # "pass" or "fail"


@dataclass
class Graph:
    stages: dict[str, dict]  # by id, in the order written
    edges: list[Edge]
    default_tries: int       # for a stage that names no `tries`: the flow's limit

    def tries(self, stage: str) -> int:
        return self.stages[stage].get("tries", self.default_tries)

    @property
    def order(self) -> list[str]:
        return list(self.stages)

    @property
    def start(self) -> str:
        return self.order[0] if self.stages else END

    def after(self, stage: str, on: str = "pass") -> str:
        """Where an item goes from a stage: on "pass", or on "fail" while the stage has tries left."""
        return next((e.dst for e in self.edges if e.src == stage and e.on == on), END if on == "pass" else stage)

    def senders(self, stage: str) -> set[str]:
        """The other stages whose fail edge leads back to this one."""
        return {e.src for e in self.edges if e.on == "fail" and e.dst == stage and e.src != stage}

    def position(self, last: dict | None, rejected: int = 0) -> str:
        """Where an item stands, from its latest try (`stage`, `outcome`) and how often that stage was rejected.
        This is how a resumed item is put back on the graph: from stored facts, not a saved pointer."""
        if last is None or last["stage"] not in self.stages:
            return self.start
        stage = last["stage"]
        if last["outcome"] == "passed":
            return self.after(stage)
        if last["outcome"] == "rejected" and rejected < self.tries(stage):
            return self.after(stage, "fail")
        return stage  # cut short, failed, asked for a person, or out of tries: this stage again


def compile_flow(flow: dict, limits: dict) -> tuple[Graph, list[str]]:
    """(graph, errors). The graph is usable only when there are no errors. `limits` are the ones the flow runs under:
    `tries` for a stage that names none, and `max_tries`, the most a stage may ask for."""
    errs: list[str] = []
    stages: dict[str, dict] = {}
    for s in flow.get("stages") or []:
        sid = s.get("id") if isinstance(s, dict) else None
        if not sid:
            errs.append("a stage has no id")
        elif sid == END:
            errs.append(f"a stage cannot be called '{END}'")
        elif sid in stages:
            errs.append(f"stage {sid}: the id is used twice")
        else:
            stages[sid] = s
    order, edges = list(stages), []
    for i, (sid, s) in enumerate(stages.items()):
        nxt = s.get("next", order[i + 1] if i + 1 < len(order) else END)
        if nxt != END and nxt not in stages:
            errs.append(f"stage {sid}: next names '{nxt}', which is not a stage")
            nxt = END
        edges.append(Edge(sid, nxt, "pass"))
        if "on_fail" in s and not validated(s):
            errs.append(f"stage {sid}: on_fail needs something that can fail: check, done_when or review")
        if validated(s):
            back = s.get("on_fail", sid)
            if back not in stages:
                errs.append(f"stage {sid}: on_fail names '{back}', which is not a stage")
                back = sid
            edges.append(Edge(sid, back, "fail"))
        tries = s.get("tries", limits["tries"])
        if isinstance(tries, bool) or not isinstance(tries, int) or not 1 <= tries <= limits["max_tries"]:
            errs.append(f"stage {sid}: 'tries' must be a number from 1 to {limits['max_tries']}")
    graph = Graph(stages, edges, limits["tries"])
    if not stages:
        return graph, errs + ["the flow has no stages"]

    # pass edges alone must lead from the first stage to the end, visiting nothing twice
    seen, at = [], graph.start
    while at != END and at not in seen:
        seen.append(at)
        at = graph.after(at)
    if at != END:
        errs.append(f"stage {seen[-1]}: next goes back to '{at}'. A loop must use on_fail, which is bounded by tries")
    errs += [f"stage {sid}: nothing leads to it" for sid in order if sid not in seen]
    for e in edges:  # a fail edge goes back along the way the item came, so the item passes the failed stage again
        if e.on == "fail" and e.src in seen and e.dst in seen and seen.index(e.dst) > seen.index(e.src):
            errs.append(f"stage {e.src}: on_fail names '{e.dst}', which comes later. It must be this stage or an earlier one")
    return graph, errs
