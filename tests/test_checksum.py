import hashlib

import pytest

from seqflow.checksum import ChecksumManager
from seqflow.exceptions import ChecksumMismatchError
from tests.conftest import write_fastq


def test_md5_matches_hashlib(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    assert ChecksumManager().md5(f) == hashlib.md5(b"hello").hexdigest()


def test_manifest_roundtrip(tmp_path):
    for i in range(25):
        write_fastq(tmp_path / f"s{i}" / f"s{i}_R1.fastq.gz", f"@r{i}\nAC\n+\nII\n".encode())
    cm = ChecksumManager(workers=4)
    manifest = cm.write_manifest(tmp_path)
    assert len(manifest.read_text().splitlines()) == 25
    assert cm.verify_manifest(manifest) == []


def test_detects_corruption_and_missing(tmp_path):
    write_fastq(tmp_path / "a.fastq.gz")
    write_fastq(tmp_path / "b.fastq.gz")
    cm = ChecksumManager()
    manifest = cm.write_manifest(tmp_path)
    (tmp_path / "a.fastq.gz").write_bytes(b"corrupted")
    (tmp_path / "b.fastq.gz").unlink()
    failures = cm.verify_manifest(manifest)
    assert any("mismatch: a.fastq.gz" in f for f in failures)
    assert any("missing: b.fastq.gz" in f for f in failures)
    with pytest.raises(ChecksumMismatchError):
        cm.verify_or_raise(manifest)


def test_rename_many_is_all_or_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "exists.txt").write_text("x")
    with pytest.raises(FileExistsError):
        ChecksumManager.rename_many({tmp_path / "a.txt": tmp_path / "a2.txt",
                                     tmp_path / "b.txt": tmp_path / "exists.txt"})
    assert (tmp_path / "a.txt").exists()  # nothing moved


def test_rename_many_dry_run(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    n = ChecksumManager.rename_many({tmp_path / "a.txt": tmp_path / "z.txt"}, dry_run=True)
    assert n == 1 and (tmp_path / "a.txt").exists() and not (tmp_path / "z.txt").exists()
