"""Where things are on disk. Every folder and file name the engine writes is named here and nowhere else.

    data/db/agentflow.db          the store
    data/fetches/<date>-<n>-<source>/   one folder per fetch: what the source's script wrote, as it wrote it
    data/runs/<date>-<run id>/    one folder per run, trial or not; everything the run writes is inside
        plugins/                  the plugin files exactly as the run first saw them
        items/<item id>/          each item's work folder
            events.jsonl           the engine's record of what happened to this item
            attempts/             what each try was told, and what a CLI that did the try printed
            verdicts/             what each validation said
            ...                   the files the stages wrote
        output/                   what the flow puts out for this run: its scripts write here, the engine only serves it

The store keeps folders relative to the data folder, so the data folder can be moved.
"""
from datetime import datetime
from pathlib import Path

DB = Path("db") / "agentflow.db"
RUNS, FETCHES = "runs", "fetches"
PLUGINS, ITEMS, OUTPUT = "plugins", "items", "output"                     # inside a run folder
EVENTS, ATTEMPTS, VERDICTS = "events.jsonl", "attempts", "verdicts"            # inside an item's folder
ENGINE_OWNED = (EVENTS, VERDICTS, ATTEMPTS)                                   # not writable by agents or scripts



def new_run_folder(data: Path, run_id: str) -> Path:
    return data / RUNS / f"{datetime.now():%Y-%m-%d}-{run_id}"


def new_fetch_folder(data: Path, source: str, fetch_id: int) -> Path:
    return data / FETCHES / f"{datetime.now():%Y-%m-%d}-{fetch_id}-{source}"


def prompt_file(stage: str, n: int) -> str:
    """Relative to the item's folder: what try `n` of a stage was told."""
    return f"{ATTEMPTS}/{stage}-{n}.prompt.md"


def cli_file(stage: str, n: int, harness: str, kind: str) -> str:
    """What a CLI left of try `n`: everything it printed (kind "jsonl"), or its last message (kind "md")."""
    return f"{ATTEMPTS}/{stage}-{n}.{harness}.{kind}"


def verdict_file(stage: str, n: int) -> str:
    return f"{VERDICTS}/{stage}-{n}.md"


def confined(work: Path, rel: str, writing: bool = False) -> Path:
    """A path inside a folder, or ValueError. No absolute paths, no '..', no symbolic links, no writing the engine's files."""
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or not parts:
        raise ValueError("path must be relative and inside the work folder")
    cur = work
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError("symbolic links are refused")
    if writing and parts[0] in ENGINE_OWNED:
        raise ValueError(f"{parts[0]} belongs to the engine")
    return work / rel


def visible_files(work: Path) -> list[str]:
    """The files of an item's folder that the stages wrote, without the engine's own."""
    return sorted(str(p.relative_to(work)) for p in work.rglob("*")
                  if p.is_file() and p.relative_to(work).parts[0] not in ENGINE_OWNED)
