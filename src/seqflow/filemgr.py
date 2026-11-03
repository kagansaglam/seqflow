"""Bulk file management for sequencing output: inventory, verified archiving,
guarded deletion, batch renaming and duplicate detection.

Design rules:
* Destructive operations default to dry-run or need an explicit flag.
* Nothing is removed from the source until every copy has been verified by md5.
* Every action is appended to a JSONL audit log.
* Symlinks are never followed or counted.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .checksum import ChecksumManager
from .exceptions import ChecksumMismatchError, FileOperationError


def _ext(name: str) -> str:
    """Extension used for grouping; keeps double extensions such as .fastq.gz."""
    low = name.lower()
    if low.endswith(".gz"):
        inner = Path(low[:-3]).suffix
        return f"{inner}.gz" if inner else ".gz"
    return Path(low).suffix or "(none)"


class AuditLog:
    """Append-only JSONL log. With no path it only keeps entries in memory."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, **fields) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "action": action, **fields}
        self.entries.append(entry)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")


@dataclass
class Inventory:
    root: str
    n_files: int = 0
    total_bytes: int = 0
    by_extension: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class ArchiveResult:
    n_files: int = 0
    total_bytes: int = 0
    verified: bool = False
    removed_source: bool = False
    dry_run: bool = False


@dataclass
class DeleteResult:
    n_files: int = 0
    bytes_freed: int = 0
    dry_run: bool = True


class FileManager:
    def __init__(self, checksums: ChecksumManager | None = None,
                 audit: AuditLog | None = None) -> None:
        self.checksums = checksums or ChecksumManager()
        self.audit = audit or AuditLog()

    # ---- discovery ----------------------------------------------------
    @staticmethod
    def iter_files(root: str | Path, pattern: str = "*") -> Iterator[Path]:
        """Yield regular files below root in deterministic order (symlinks skipped)."""
        root = Path(root)
        if not root.is_dir():
            raise FileOperationError(f"not a directory: {root}")
        for dirpath, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not fnmatch.fnmatch(name, pattern):
                    continue
                yield Path(full)

    def inventory(self, root: str | Path, pattern: str = "*") -> Inventory:
        inv = Inventory(root=str(root))
        for f in self.iter_files(root, pattern):
            size = f.stat().st_size
            inv.n_files += 1
            inv.total_bytes += size
            bucket = inv.by_extension.setdefault(_ext(f.name), {"files": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += size
        return inv

    def find_files(self, root: str | Path, pattern: str = "*",
                   older_than_days: float | None = None,
                   min_bytes: int | None = None) -> list[Path]:
        cutoff = time.time() - older_than_days * 86400 if older_than_days is not None else None
        found = []
        for f in self.iter_files(root, pattern):
            st = f.stat()
            if cutoff is not None and st.st_mtime > cutoff:
                continue
            if min_bytes is not None and st.st_size < min_bytes:
                continue
            found.append(f)
        return found

    def find_duplicates(self, root: str | Path, pattern: str = "*") -> list[list[Path]]:
        """Group identical files: cheap size filter first, md5 only for same-size files."""
        by_size: dict[int, list[Path]] = defaultdict(list)
        for f in self.iter_files(root, pattern):
            by_size[f.stat().st_size].append(f)
        candidates = [f for grp in by_size.values() if len(grp) > 1 for f in grp]
        sums = self.checksums.compute_many(candidates)
        by_md5: dict[str, list[Path]] = defaultdict(list)
        for f in candidates:
            by_md5[sums[str(f)]].append(f)
        return sorted(sorted(g) for g in by_md5.values() if len(g) > 1)

    # ---- archiving ----------------------------------------------------
    def archive(self, src: str | Path, dest: str | Path, *, pattern: str = "*",
                remove_source: bool = False, dry_run: bool = False) -> ArchiveResult:
        """Copy src -> dest, verify every file by md5, optionally remove the source.

        The source is only touched after all copies verified. Existing files in
        dest are never overwritten.
        """
        src, dest = Path(src), Path(dest)
        files = list(self.iter_files(src, pattern))
        if not files:
            raise FileOperationError(f"no files matching {pattern!r} in {src}")
        if dest.resolve() == src.resolve() or src.resolve() in dest.resolve().parents:
            raise FileOperationError("destination must not be inside the source")
        pairs = [(f, dest / f.relative_to(src)) for f in files]
        clashes = [str(d) for _, d in pairs if d.exists()]
        if clashes:
            raise FileOperationError(
                f"refusing to overwrite {len(clashes)} existing file(s), e.g. {clashes[0]}"
            )
        total = sum(f.stat().st_size for f in files)
        result = ArchiveResult(n_files=len(files), total_bytes=total, dry_run=dry_run)
        ctx = {"src": str(src), "dest": str(dest), "files": len(files), "bytes": total}
        if dry_run:
            self.audit.record("archive_dry_run", **ctx)
            return result

        for s, d in pairs:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        src_sums = self.checksums.compute_many(s for s, _ in pairs)
        dst_sums = self.checksums.compute_many(d for _, d in pairs)
        bad = [str(s) for s, d in pairs if src_sums[str(s)] != dst_sums[str(d)]]
        if bad:
            self.audit.record("archive_verify_failed", n_failed=len(bad), failed=bad[:20], **ctx)
            raise ChecksumMismatchError(
                f"{len(bad)} copied file(s) failed md5 verification; source left untouched "
                f"(first: {bad[0]})"
            )
        result.verified = True
        self.audit.record("archive_verified", **ctx)

        if remove_source:
            for s, _ in pairs:
                size = s.stat().st_size
                s.unlink()
                self.audit.record("remove_source", path=str(s), bytes=size)
            self._prune_empty_dirs(src)
            result.removed_source = True
        return result

    @staticmethod
    def _prune_empty_dirs(root: Path) -> None:
        """Remove empty sub-directories below root (root itself is kept)."""
        for dirpath, _dirs, _files in os.walk(root, topdown=False):
            p = Path(dirpath)
            if p != root and not any(p.iterdir()):
                p.rmdir()

    # ---- deletion -----------------------------------------------------
    def delete_files(self, paths: Iterable[str | Path], *, allowed_root: str | Path,
                     dry_run: bool = True) -> DeleteResult:
        """Delete files, but only regular files inside allowed_root.

        Everything is validated before the first deletion, so a bad path in the
        list means nothing is removed. Dry-run is the default.
        """
        root = Path(allowed_root).resolve()
        targets: list[tuple[Path, int]] = []
        for p in map(Path, paths):
            resolved = p.resolve()
            if root not in resolved.parents:
                raise FileOperationError(f"refusing to touch path outside {root}: {p}")
            if not resolved.is_file():
                raise FileOperationError(f"not a regular file: {p}")
            targets.append((p, resolved.stat().st_size))

        result = DeleteResult(dry_run=dry_run)
        for p, size in targets:
            self.audit.record("delete_dry_run" if dry_run else "delete", path=str(p), bytes=size)
            if not dry_run:
                p.unlink()
            result.n_files += 1
            result.bytes_freed += size
        return result

    def prune_old(self, root: str | Path, older_than_days: float, *, pattern: str = "*",
                  execute: bool = False) -> DeleteResult:
        """Delete files older than N days below root. Dry-run unless execute=True."""
        old = self.find_files(root, pattern, older_than_days=older_than_days)
        return self.delete_files(old, allowed_root=root, dry_run=not execute)

    # ---- renaming -----------------------------------------------------
    def bulk_rename(self, root: str | Path, regex: str, replacement: str, *,
                    pattern: str = "*", dry_run: bool = False) -> dict[Path, Path]:
        """Rename files by regex substitution on the file name.

        The full plan is checked first: two files mapping to one name, or a target
        that already exists, abort the whole batch before anything is moved.
        """
        rx = re.compile(regex)
        plan: dict[Path, Path] = {}
        for f in self.iter_files(root, pattern):
            new_name = rx.sub(replacement, f.name)
            if new_name != f.name:
                plan[f] = f.with_name(new_name)
        clashes = [str(d) for d, n in Counter(plan.values()).items() if n > 1]
        if clashes:
            raise FileOperationError(f"several files would be renamed to: {clashes[:3]}")
        self.checksums.rename_many(plan, dry_run=dry_run)
        action = "rename_dry_run" if dry_run else "rename"
        for s, d in plan.items():
            self.audit.record(action, src=str(s), dest=str(d))
        return plan
