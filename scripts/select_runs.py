"""Pick a reproducible random subset of runs from an ENA 'read_run' report.

    python scripts/select_runs.py all_runs.tsv selected.tsv --n 2000 \\
        --instrument "Illumina MiSeq" --min-reads 10000 --max-reads 100000 --seed 2026

Runs outside the read-count range (empty or failed libraries at the low end, a few very
deep runs at the high end) are left out before sampling. The same input, filters and seed
always give the same selection.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys

COLUMNS = ["run_accession", "library_layout", "read_count", "fastq_bytes"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("runs")
    p.add_argument("out")
    p.add_argument("--n", type=int, default=2000)
    p.add_argument("--instrument")
    p.add_argument("--min-reads", type=int, default=0)
    p.add_argument("--max-reads", type=int)
    p.add_argument("--seed", type=int, default=2026)
    args = p.parse_args(argv)
    with open(args.runs, newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    pool = [r for r in rows
            if (not args.instrument or r.get("instrument_model") == args.instrument)
            and int(r["read_count"] or 0) >= args.min_reads
            and (args.max_reads is None or int(r["read_count"] or 0) <= args.max_reads)]
    if len(pool) < args.n:
        raise SystemExit(f"only {len(pool)} runs pass the filters, {args.n} requested")
    pool.sort(key=lambda r: r["run_accession"])
    chosen = sorted(random.Random(args.seed).sample(pool, args.n),
                    key=lambda r: r["run_accession"])
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(COLUMNS)
        for r in chosen:
            w.writerow([r[c] for c in COLUMNS])
    size = sum(int(b) for r in chosen for b in r["fastq_bytes"].split(";") if b)
    reads = sum(int(r["read_count"]) for r in chosen)
    print(f"{len(rows)} runs, {len(pool)} pass the filters, {args.n} selected: "
          f"{reads} reads, {size / 1e9:.2f} GB as gzipped FASTQ at ENA")
    return 0


if __name__ == "__main__":
    sys.exit(main())
