"""Command-line entry point.

Run management:  seqflow watch | process | summary
File management: seqflow inventory | archive | prune | rename | dedupe | synth | bench
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict

from .bench import benchmark, format_markdown
from .checksum import ChecksumManager
from .db import Database
from .filemgr import AuditLog, FileManager
from .launcher import PipelineLauncher
from .notify import Notifier
from .orchestrator import Orchestrator
from .synth import generate_fileset
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

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--pattern", default="*", help="file name glob (default: all files)")
        sp.add_argument("--audit-log", help="append actions to this JSONL file")
        sp.add_argument("--workers", type=int, default=4, help="md5 threads")

    inv = sub.add_parser("inventory", help="count files and bytes by extension")
    inv.add_argument("root")
    add_common(inv)

    ar = sub.add_parser("archive", help="copy a directory, verify by md5, optionally remove source")
    ar.add_argument("src")
    ar.add_argument("dest")
    ar.add_argument("--remove-source", action="store_true")
    ar.add_argument("--dry-run", action="store_true")
    add_common(ar)

    pn = sub.add_parser("prune", help="delete files older than N days (dry-run unless --execute)")
    pn.add_argument("root")
    pn.add_argument("--older-than-days", type=float, required=True)
    pn.add_argument("--execute", action="store_true")
    add_common(pn)

    rn = sub.add_parser("rename", help="regex rename of file names")
    rn.add_argument("root")
    rn.add_argument("--regex", required=True)
    rn.add_argument("--repl", required=True)
    rn.add_argument("--dry-run", action="store_true")
    add_common(rn)

    dd = sub.add_parser("dedupe", help="list identical files (never deletes)")
    dd.add_argument("root")
    add_common(dd)

    sy = sub.add_parser("synth", help="generate a synthetic FASTQ-like file set for benchmarks")
    sy.add_argument("root")
    sy.add_argument("--files", type=int, required=True)
    sy.add_argument("--min-kb", type=int, default=50)
    sy.add_argument("--max-kb", type=int, default=500)
    sy.add_argument("--seed", type=int, default=0)
    sy.add_argument("--paired", action="store_true")

    be = sub.add_parser("bench", help="benchmark md5 throughput (read-only)")
    be.add_argument("root")
    be.add_argument("--pattern", default="*.fastq.gz")
    be.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    be.add_argument("--repeats", type=int, default=3)
    be.add_argument("--no-warmup", action="store_true",
                    help="skip the warm-up read (first row then includes the cold read)")
    be.add_argument("--archive-to", help="also time a verified copy into this new directory")
    be.add_argument("--json", action="store_true", help="print JSON instead of a table")
    return p


FILE_COMMANDS = {"inventory", "archive", "prune", "rename", "dedupe", "synth", "bench"}


def _run_file_command(args: argparse.Namespace) -> int:
    if args.cmd == "synth":
        summary = generate_fileset(
            args.root, args.files, min_bytes=args.min_kb * 1000, max_bytes=args.max_kb * 1000,
            seed=args.seed, paired=args.paired,
            progress=lambda done, total: print(f"  generated {done}/{total} files", file=sys.stderr),
        )
        print(json.dumps(asdict(summary), indent=2))
        return 0

    if args.cmd == "bench":
        result = benchmark(args.root, workers=args.workers, pattern=args.pattern,
                           repeats=args.repeats, warmup=not args.no_warmup,
                           archive_to=args.archive_to,
                           progress=lambda msg: print(f"  {msg}", file=sys.stderr))
        print(json.dumps(result, indent=2) if args.json else format_markdown(result))
        return 0

    fm = FileManager(ChecksumManager(workers=args.workers), AuditLog(args.audit_log))
    if args.cmd == "inventory":
        print(json.dumps(asdict(fm.inventory(args.root, args.pattern)), indent=2))
    elif args.cmd == "archive":
        res = fm.archive(args.src, args.dest, pattern=args.pattern,
                         remove_source=args.remove_source, dry_run=args.dry_run)
        print(json.dumps(asdict(res), indent=2))
    elif args.cmd == "prune":
        res = fm.prune_old(args.root, args.older_than_days, pattern=args.pattern,
                           execute=args.execute)
        print(json.dumps(asdict(res), indent=2))
    elif args.cmd == "rename":
        plan = fm.bulk_rename(args.root, args.regex, args.repl, pattern=args.pattern,
                              dry_run=args.dry_run)
        print(json.dumps({"renamed": len(plan), "dry_run": args.dry_run}, indent=2))
    elif args.cmd == "dedupe":
        groups = fm.find_duplicates(args.root, args.pattern)
        print(json.dumps([[str(p) for p in g] for g in groups], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd in FILE_COMMANDS:
        return _run_file_command(args)

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
