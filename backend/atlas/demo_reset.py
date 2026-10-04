"""Demo mode: papers added during a server session are removed when the server stops.

The server snapshots data/graph/ at start-up and restores it on shutdown (Ctrl+C). If a previous
session ended without restoring (crash, killed terminal), the leftover snapshot is restored at the
next start-up. Set ATLAS_PERSIST=1 to keep added papers instead.
"""
import os
import shutil
from pathlib import Path

from .graph import OUT

SNAPSHOT = OUT.parent / "graph_snapshot"


def enabled() -> bool:
    return os.environ.get("ATLAS_PERSIST", "") not in ("1", "true", "yes")


def snapshot(out: Path = OUT, snap: Path = SNAPSHOT) -> None:
    if snap.exists():  # previous session never restored: undo its additions first
        restore(out, snap)
    shutil.copytree(out, snap)


def restore(out: Path = OUT, snap: Path = SNAPSHOT) -> None:
    if not snap.exists():
        return
    for f in snap.iterdir():
        shutil.copy2(f, out / f.name)
    for f in out.iterdir():
        if not (snap / f.name).exists():
            f.unlink()
    shutil.rmtree(snap)
