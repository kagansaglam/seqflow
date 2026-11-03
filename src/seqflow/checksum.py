"""Bulk file management: md5 generation/verification, safe rename, move, archive."""
from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .exceptions import ChecksumMismatchError


class ChecksumManager:
    def __init__(self, workers: int = 4, chunk_size: int = 1024 * 1024) -> None:
        self.workers = workers
        self.chunk_size = chunk_size

    def md5(self, path: str | Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(self.chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()

    def compute_many(self, paths: Iterable[str | Path]) -> dict[str, str]:
        paths = [str(p) for p in paths]
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            return dict(zip(paths, ex.map(self.md5, paths)))

    def write_manifest(self, directory: str | Path, pattern: str = "*.fastq.gz") -> Path:
        directory = Path(directory)
        files = sorted(directory.rglob(pattern))
        sums = self.compute_many(files)
        manifest = directory / "md5sum.txt"
        manifest.write_text(
            "".join(f"{sums[str(f)]}  {f.relative_to(directory)}\n" for f in files)
        )
        return manifest

    def verify_manifest(self, manifest: str | Path) -> list[str]:
        """Return list of failure descriptions; empty list means all OK."""
        manifest = Path(manifest)
        base = manifest.parent
        failures = []
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            expected, rel = line.split(None, 1)
            f = base / rel.strip()
            if not f.is_file():
                failures.append(f"missing: {rel}")
            elif self.md5(f) != expected:
                failures.append(f"mismatch: {rel}")
        return failures

    def verify_or_raise(self, manifest: str | Path) -> None:
        failures = self.verify_manifest(manifest)
        if failures:
            raise ChecksumMismatchError("; ".join(failures))

    @staticmethod
    def rename_many(mapping: dict[str | Path, str | Path], dry_run: bool = False) -> int:
        """Rename files; refuses to overwrite and validates everything before touching disk."""
        for src, dst in mapping.items():
            if not Path(src).exists():
                raise FileNotFoundError(src)
            if Path(dst).exists():
                raise FileExistsError(dst)
        if not dry_run:
            for src, dst in mapping.items():
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        return len(mapping)
