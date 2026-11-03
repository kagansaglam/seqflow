import gzip
from pathlib import Path

import pytest

from seqflow.db import Database

SHEET = """[Header]
IEMFileVersion,5

[Data]
Sample_ID,Sample_Project,index,index2
S1,projA,ACGTACGT,TTGGCCAA
S2,projA,GGCCTTAA,AACCGGTT
S3,projB,CCAATTGG,GGTTAACC
"""


@pytest.fixture
def db():
    d = Database("sqlite:///:memory:")
    d.create_all()
    yield d
    d.engine.dispose()


@pytest.fixture
def illumina_run(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "240101_A00001_0001_TESTFC"
    run.mkdir(parents=True)
    (run / "SampleSheet.csv").write_text(SHEET)
    (run / "RTAComplete.txt").write_text("done")
    return run


def write_fastq(path: Path, content: bytes = b"@r\nACGT\n+\nIIII\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(content)
