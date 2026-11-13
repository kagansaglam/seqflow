"""Write "all clusters passed filter" files for tiles of a run folder that has none.

    python scripts/make_pf_filters.py RUN_DIR [--dry-run]

Some reduced or archived run folders contain BCL files but no *.filter files. bcl2fastq
handled this with --ignore-missing-filter, which "assumes that all clusters in tiles with
missing filter files passed filter". BCL Convert instead produces no reads for such tiles.
This script writes the filter files that bcl2fastq assumed, so BCL Convert gives the same
result. Existing filter files are never overwritten.

File layouts follow the bcl2fastq2 v2.20 software guide (15051736 v03):
  BCL:    bytes 0-3 number of clusters (uint32), then one byte per cluster
  filter: bytes 0-3 zero, 4-7 format version, 8-11 number of clusters (uint32),
          then one byte per cluster, bit 0 = passed filter
Expected location: Data/Intensities/BaseCalls/L00<lane>/s_<lane>_<tile>.filter
"""
from __future__ import annotations

import argparse
import gzip
import re
import struct
import sys
from pathlib import Path

FILTER_VERSION = 3
_BCL_NAME = re.compile(r"^s_(\d+)_(\d+)\.bcl(\.gz)?$")


def bcl_cluster_count(path: Path) -> int:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as fh:
        head = fh.read(4)
    if len(head) != 4:
        raise SystemExit(f"{path}: too short to be a BCL file")
    return struct.unpack("<I", head)[0]


def filter_bytes(clusters: int) -> bytes:
    return struct.pack("<III", 0, FILTER_VERSION, clusters) + b"\x01" * clusters


def cycle_dirs(lane_dir: Path) -> list[Path]:
    dirs = [d for d in lane_dir.iterdir() if d.is_dir() and re.fullmatch(r"C\d+\.1", d.name)]
    return sorted(dirs, key=lambda d: int(d.name[1:-2]))


def plan(run_dir: Path) -> list[tuple[Path, int]]:
    basecalls = run_dir / "Data" / "Intensities" / "BaseCalls"
    lanes = sorted(d for d in basecalls.glob("L0*") if d.is_dir())
    if not lanes:
        raise SystemExit(f"no lane folders under {basecalls}")
    todo: list[tuple[Path, int]] = []
    for lane_dir in lanes:
        cycles = cycle_dirs(lane_dir)
        if not cycles:
            raise SystemExit(f"{lane_dir}: no cycle folders (C<n>.1)")
        for bcl in sorted(cycles[0].iterdir()):
            m = _BCL_NAME.match(bcl.name)
            if not m:
                continue
            lane, tile = m.group(1), m.group(2)
            counts = {bcl_cluster_count(c / bcl.name) for c in (cycles[0], cycles[-1])
                      if (c / bcl.name).exists()}
            if len(counts) != 1:
                raise SystemExit(f"tile {tile}: cluster counts differ between cycles {counts}")
            target = lane_dir / f"s_{lane}_{tile}.filter"
            if not target.exists():
                todo.append((target, counts.pop()))
    return todo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("run_dir")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    todo = plan(Path(args.run_dir))
    if not todo:
        print("every tile already has a filter file; nothing to do")
        return 0
    for target, clusters in todo:
        print(f"{'would write' if args.dry_run else 'writing'} {target} ({clusters} clusters)")
        if not args.dry_run:
            target.write_bytes(filter_bytes(clusters))
    return 0


if __name__ == "__main__":
    sys.exit(main())
