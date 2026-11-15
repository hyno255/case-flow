"""How values are worded on a page, and how a folder is shown: one place, so every table says the same thing the same way."""
from datetime import datetime
from pathlib import Path

from ..layout import confined
from ..status import cost_text
from ..trace import seconds_text

DASH = "—"          # a value that is not known or does not apply; the cell's tooltip says which
PREVIEW_CAP = 100_000
HIDDEN = ("__pycache__",)


def when(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %H:%M") if iso else ""


def fetch_by(slot: str) -> str:
    """What started a fetch, from the slot it was recorded under: a schedule's slot is a time, the others say who."""
    return "schedule" if slot[:1].isdigit() else "trial" if slot.startswith("trial") else "by hand"


def seconds_between(start: str | None, end: str | None) -> float:
    if not start:
        return 0.0
    finish = datetime.fromisoformat(end) if end else datetime.now().astimezone()
    return max(0.0, (finish - datetime.fromisoformat(start)).total_seconds())


def duration(seconds: float) -> str:
    """A length of time taken from the store's clock times, which are kept to the second. The trace has finer ones."""
    s = round(seconds)
    return f"{s}s" if s < 60 else seconds_text(s)


def cost(u: dict, not_recorded: str = "cost was not recorded for this run") -> dict:
    """{text, tip} for a usage total (Store.usage, or the same three facts from a trace span).
    Not known is a dash with the reason, partly known is "≥", and a known zero is "$0"."""
    if not u["has_model_calls"]:
        return {"text": DASH, "tip": "no model was used"}
    partly = not u["cost_known"] and bool(u["cost_usd"])
    text = cost_text({**u, "cost_known": u["cost_known"] or partly}, unknown=DASH)
    if partly:
        return {"text": f"≥ {text}", "tip": "part of the cost was not recorded"}
    if text == DASH:
        return {"text": DASH, "tip": not_recorded}
    if not u["cost_usd"]:
        return {"text": "$0", "tip": "no charge"}
    return {"text": text if u["cost_usd"] >= 0.00005 else "< $0.0001", "tip": "estimated from the price table"}


def item_title(payload: dict) -> str:
    """A short line for an item from whatever the source sent: the first two short text values after the id."""
    texts = [str(v) for k, v in payload.items() if k != "id" and isinstance(v, str) and len(v) <= 40]
    return " · ".join(texts[:2])


# ---------- a folder as a tree, and one of its files ----------

def size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} kB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def file_tree(folder: Path, selected: str = "", engine: tuple = ()) -> list[dict]:
    """The folder as nested rows, folders first. A folder is open when it holds the selected file.
    `engine` names the top-level entries that belong to the engine, which the page marks."""
    def walk(d: Path, prefix: str) -> list[dict]:
        rows = []
        for p in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if p.is_symlink() or p.name.startswith(".") or p.name in HIDDEN:
                continue
            path = prefix + p.name
            row = {"name": p.name, "path": path, "engine": path.split("/")[0] in engine}
            if p.is_dir():
                row["children"] = walk(p, path + "/")
                row["open"] = selected.startswith(path + "/")
            else:
                row["size"] = size_text(p.stat().st_size)
            rows.append(row)
        return rows
    return walk(folder, "") if folder.is_dir() else []


def first_file(tree: list[dict], prefer: tuple = ()) -> str:
    """The file a Files tab opens with: a preferred one when the folder has it, else its first file."""
    top = [r["path"] for r in tree if "children" not in r]
    return next((p for p in prefer if p in top), top[0] if top else "")


def file_preview(folder: Path, rel: str, raw: str) -> dict | None:
    """One file of a folder as text, or why it cannot be shown. `raw` is the address that serves the folder's files as plain text."""
    if not rel:
        return None
    try:
        target = confined(folder, rel)
    except ValueError as e:
        return {"path": rel, "error": str(e)}
    if not target.is_file():
        return {"path": rel, "error": "no such file"}
    text = target.read_text(errors="replace")
    return {"path": rel, "abs": str(target), "size": size_text(target.stat().st_size), "text": text[:PREVIEW_CAP], "cut": len(text) > PREVIEW_CAP,
            "raw": f"{raw}/{rel}"}
