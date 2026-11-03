"""Command-line entry point: `seqflow watch|process|summary`."""
from __future__ import annotations

import argparse
import json
import logging
import os

from .db import Database
from .launcher import PipelineLauncher
from .notify import Notifier
from .orchestrator import Orchestrator
from .watcher import RunWatcher


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seqflow")
    p.add_argument("--db", default=os.environ.get("SEQFLOW_DB", "sqlite:///seqflow.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="poll a directory and process new runs")
    w.add_argument("watch_dir")
    w.add_argument("--platform", choices=["illumina", "ont"], required=True)
    w.add_argument("--interval", type=float, default=60.0)
    w.add_argument("--no-launch", action="store_true")

    pr = sub.add_parser("process", help="process a single run directory")
    pr.add_argument("run_dir")
    pr.add_argument("--platform", choices=["illumina", "ont"], required=True)

    s = sub.add_parser("summary", help="print a JSON summary for a run")
    s.add_argument("run_id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = Database(args.db)
    db.create_all()
    if args.cmd == "summary":
        print(json.dumps(db.run_summary(args.run_id), indent=2))
        return 0
    launcher = None if getattr(args, "no_launch", False) else PipelineLauncher()
    orch = Orchestrator(db, Notifier(), launcher)
    if args.cmd == "process":
        return 0 if orch.process_run(args.run_dir, args.platform) == "done" else 1
    RunWatcher(args.watch_dir, args.platform, db, orch, args.interval).loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
