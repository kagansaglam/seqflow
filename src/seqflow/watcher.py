"""Poll a directory for new run folders and hand them to the orchestrator."""
from __future__ import annotations

import time
from pathlib import Path

from .db import Database
from .orchestrator import Orchestrator


class RunWatcher:
    def __init__(self, watch_dir: str | Path, platform: str, db: Database, orchestrator: Orchestrator,
                 interval: float = 60.0) -> None:
        self.watch_dir = Path(watch_dir)
        self.platform = platform
        self.db = db
        self.orchestrator = orchestrator
        self.interval = interval

    def scan_once(self) -> list[str]:
        """Process every run directory that has not reached a terminal state."""
        handled = []
        for d in sorted(p for p in self.watch_dir.iterdir() if p.is_dir()):
            known = [r for st in ("done", "failed") for r in self.db.runs_by_status(st) if r.run_id == d.name]
            if known:
                continue
            self.orchestrator.process_run(d, self.platform)
            handled.append(d.name)
        return handled

    def loop(self) -> None:  # pragma: no cover - long-running
        while True:
            self.scan_once()
            time.sleep(self.interval)
