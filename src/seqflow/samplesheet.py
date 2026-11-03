"""Illumina-style SampleSheet parsing and validation."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .exceptions import SampleSheetError

_INDEX_RE = re.compile(r"^[ACGTN]+$")


@dataclass(frozen=True)
class SampleRecord:
    sample_id: str
    index1: str | None
    index2: str | None
    lane: int | None = None
    project: str | None = None


class SampleSheet:
    """Parses the [Data] section of an Illumina SampleSheet.csv and validates it."""

    REQUIRED = ("Sample_ID",)

    def __init__(self, records: list[SampleRecord]) -> None:
        self.records = records

    @classmethod
    def from_file(cls, path: str | Path) -> SampleSheet:
        path = Path(path)
        if not path.is_file():
            raise SampleSheetError(f"sample sheet not found: {path}")
        lines = path.read_text().splitlines()
        try:
            start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("[Data]"))
        except StopIteration:
            raise SampleSheetError("no [Data] section found") from None
        rows = [ln for ln in lines[start + 1:] if ln.strip() and not ln.startswith("[")]
        reader = csv.DictReader(rows)
        header = reader.fieldnames or []
        for col in cls.REQUIRED:
            if col not in header:
                raise SampleSheetError(f"missing required column: {col}")
        records = []
        for row in reader:
            lane = row.get("Lane")
            records.append(SampleRecord(
                sample_id=row["Sample_ID"].strip(),
                index1=(row.get("index") or "").strip().upper() or None,
                index2=(row.get("index2") or "").strip().upper() or None,
                lane=int(lane) if lane and lane.strip().isdigit() else None,
                project=(row.get("Sample_Project") or "").strip() or None,
            ))
        sheet = cls(records)
        sheet.validate()
        return sheet

    def validate(self) -> None:
        if not self.records:
            raise SampleSheetError("sample sheet has no samples")
        ids = [r.sample_id for r in self.records]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise SampleSheetError(f"duplicate Sample_ID: {sorted(dup)}")
        seen: dict[tuple, str] = {}
        for r in self.records:
            for idx in (r.index1, r.index2):
                if idx and not _INDEX_RE.match(idx):
                    raise SampleSheetError(f"invalid index '{idx}' for {r.sample_id}")
            key = (r.lane, r.index1, r.index2)
            if r.index1 and key in seen:
                raise SampleSheetError(
                    f"index collision between {seen[key]} and {r.sample_id}: {key}"
                )
            seen[key] = r.sample_id
        lengths = {len(r.index1) for r in self.records if r.index1}
        if len(lengths) > 1:
            raise SampleSheetError(f"mixed index1 lengths: {sorted(lengths)}")

    def __len__(self) -> int:
        return len(self.records)
