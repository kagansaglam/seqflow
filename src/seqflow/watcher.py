"""Poll a directory for new run folders and hand them to the orchestrator.

The watcher is built to run unattended: one bad run never stops the others, a failing
scan (for example an unmounted share) is reported once and retried, shutdown on
SIGINT/SIGTERM happens between runs, and a heartbeat file lets an external monitor
notice a hung process.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .db import Database
from .notify import Notifier
from .orchestrator import Orchestrator

log = logging.getLogger("seqflow")

TERMINAL_STATES = ("done", "failed")


class RunWatcher:
    def __init__(self, watch_dir: str | Path, platform: str, db: Database, orchestrator: Orchestrator,
                 interval: float = 60.0, *, stop: threading.Event | None = None,
                 heartbeat_path: str | Path | None = None,
                 notifier: Notifier | None = None) -> None:
        self.watch_dir = Path(watch_dir)
        self.platform = platform
        self.db = db
        self.orchestrator = orchestrator
        self.interval = interval
        self.stop = stop or threading.Event()
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.notifier = notifier or orchestrator.notifier
        self.cycles = 0

    def scan_once(self) -> list[str]:
        """Process every run directory that has not reached a terminal state.

        An exception while handling one run is logged and reported, then the scan goes
        on with the next run. A stop request ends the scan before the next run starts.
        """
        finished = self.db.run_ids_with_status(TERMINAL_STATES)
        handled = []
        for d in sorted(p for p in self.watch_dir.iterdir() if p.is_dir()):
            if self.stop.is_set():
                break
            if d.name in finished:
                continue
            try:
                self.orchestrator.process_run(d, self.platform)
            except Exception as exc:
                log.exception("could not process %s", d.name)
                self.notifier.notify("error", f"{d.name}: processing crashed",
                                     f"{type(exc).__name__}: {exc}")
            handled.append(d.name)
        return handled

    def run(self, max_cycles: int | None = None) -> int:
        """Scan until stop is set (or max_cycles scans are done). Returns scans done."""
        last_error: str | None = None
        while not self.stop.is_set():
            try:
                self.scan_once()
                if last_error is not None:
                    self.notifier.notify("info", "watcher recovered", f"previous error: {last_error}")
                    last_error = None
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                log.exception("scan of %s failed", self.watch_dir)
                if message != last_error:  # report a persisting problem once, not every poll
                    self.notifier.notify("error", "watcher scan failed", message)
                last_error = message
            self.cycles += 1
            self._beat()
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            self.stop.wait(self.interval)
        return self.cycles

    def _beat(self) -> None:
        if self.heartbeat_path is None:
            return
        write_heartbeat(self.heartbeat_path, self.cycles)


def write_heartbeat(path: str | Path, cycles: int) -> None:
    """Atomically write {"pid", "time", "cycles"} so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "time": time.time(), "cycles": cycles}))
    os.replace(tmp, path)


def check_heartbeat(path: str | Path, max_age_seconds: float, *,
                    now: float | None = None) -> tuple[bool, str]:
    """(healthy, explanation). Unhealthy when the file is missing, unreadable or old."""
    try:
        beat = json.loads(Path(path).read_text())
        age = (time.time() if now is None else now) - float(beat["time"])
    except FileNotFoundError:
        return False, f"no heartbeat file at {path}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"unreadable heartbeat file {path}: {exc}"
    if age > max_age_seconds:
        return False, f"last heartbeat {age:.0f} s ago (limit {max_age_seconds:g} s), pid {beat.get('pid')}"
    return True, f"alive: last heartbeat {age:.0f} s ago, pid {beat.get('pid')}, {beat.get('cycles')} scans"


def install_signal_handlers(stop: threading.Event) -> Callable[[], None]:
    """Make SIGINT/SIGTERM (and SIGBREAK on Windows) request a graceful stop.

    The first signal lets the current run finish; a second one restores the default
    behaviour, so a stuck process can still be killed with a second Ctrl+C. Must be
    called from the main thread. Returns a function that restores the old handlers.
    """
    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    signals = [getattr(signal, n) for n in names if hasattr(signal, n)]
    previous = {sig: signal.getsignal(sig) for sig in signals}

    def handler(signum, _frame) -> None:
        if stop.is_set():
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        log.warning("signal %s received: finishing the current run, then stopping", signum)
        stop.set()

    for sig in signals:
        signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore
