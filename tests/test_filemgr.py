import json
import os
import shutil
import time

import pytest

from seqflow.exceptions import ChecksumMismatchError, FileOperationError
from seqflow.filemgr import AuditLog, FileManager
from tests.conftest import write_fastq


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "run"
    write_fastq(root / "a" / "S1_R1.fastq.gz", b"@r1\nACGT\n+\nIIII\n")
    write_fastq(root / "a" / "S1_R2.fastq.gz", b"@r2\nTTTT\n+\nIIII\n")
    write_fastq(root / "b" / "S2_R1.fastq.gz", b"@r3\nGGGG\n+\nIIII\n")
    (root / "b" / "notes.txt").write_text("hello")
    return root


def test_inventory_groups_double_extensions(tree):
    inv = FileManager().inventory(tree)
    assert inv.n_files == 4
    assert inv.by_extension[".fastq.gz"]["files"] == 3
    assert inv.by_extension[".txt"]["bytes"] == 5
    assert inv.total_bytes == sum(v["bytes"] for v in inv.by_extension.values())


def test_inventory_pattern_and_missing_dir(tree, tmp_path):
    assert FileManager().inventory(tree, "*.txt").n_files == 1
    with pytest.raises(FileOperationError):
        FileManager().inventory(tmp_path / "nope")


def test_symlinks_are_skipped(tree):
    try:
        os.symlink(tree / "a" / "S1_R1.fastq.gz", tree / "link.fastq.gz")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert FileManager().inventory(tree).n_files == 4


def test_find_files_by_age_and_size(tree):
    old = tree / "b" / "notes.txt"
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    fm = FileManager()
    assert fm.find_files(tree, older_than_days=30) == [old]
    assert fm.find_files(tree, min_bytes=10_000) == []


def test_find_duplicates(tree):
    shutil.copy(tree / "a" / "S1_R1.fastq.gz", tree / "b" / "copy.fastq.gz")
    groups = FileManager().find_duplicates(tree, "*.fastq.gz")
    assert len(groups) == 1
    assert {p.name for p in groups[0]} == {"S1_R1.fastq.gz", "copy.fastq.gz"}


def test_archive_copies_and_verifies(tree, tmp_path):
    dest = tmp_path / "archive"
    res = FileManager().archive(tree, dest)
    assert res.verified and not res.removed_source and res.n_files == 4
    assert (dest / "a" / "S1_R1.fastq.gz").exists()
    assert (tree / "a" / "S1_R1.fastq.gz").exists()  # source untouched


def test_archive_remove_source_prunes_empty_dirs(tree, tmp_path):
    res = FileManager().archive(tree, tmp_path / "archive", remove_source=True)
    assert res.removed_source
    assert tree.exists() and not any(tree.iterdir())


def test_archive_dry_run_writes_nothing(tree, tmp_path):
    res = FileManager().archive(tree, tmp_path / "archive", dry_run=True)
    assert res.dry_run and not (tmp_path / "archive").exists()


def test_archive_refuses_overwrite_and_nested_dest(tree, tmp_path):
    dest = tmp_path / "archive"
    FileManager().archive(tree, dest)
    with pytest.raises(FileOperationError, match="overwrite"):
        FileManager().archive(tree, dest)
    with pytest.raises(FileOperationError, match="inside the source"):
        FileManager().archive(tree, tree / "sub")


def test_archive_keeps_source_when_copy_is_corrupt(tree, tmp_path, monkeypatch):
    real_copy = shutil.copy2

    def bad_copy(s, d, *a, **k):
        real_copy(s, d, *a, **k)
        with open(d, "ab") as fh:
            fh.write(b"garbage")

    monkeypatch.setattr(shutil, "copy2", bad_copy)
    with pytest.raises(ChecksumMismatchError):
        FileManager().archive(tree, tmp_path / "archive", remove_source=True)
    assert (tree / "a" / "S1_R1.fastq.gz").exists()  # nothing was removed


def test_delete_is_dry_run_by_default(tree):
    f = tree / "b" / "notes.txt"
    res = FileManager().delete_files([f], allowed_root=tree)
    assert res.dry_run and res.n_files == 1 and f.exists()


def test_delete_executes_when_asked(tree):
    f = tree / "b" / "notes.txt"
    res = FileManager().delete_files([f], allowed_root=tree, dry_run=False)
    assert res.bytes_freed == 5 and not f.exists()


def test_delete_validates_everything_first(tree, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    good = tree / "b" / "notes.txt"
    with pytest.raises(FileOperationError, match="outside"):
        FileManager().delete_files([good, outside], allowed_root=tree, dry_run=False)
    assert good.exists() and outside.exists()


def test_delete_refuses_directories(tree):
    with pytest.raises(FileOperationError, match="regular file"):
        FileManager().delete_files([tree / "a"], allowed_root=tree, dry_run=False)


def test_prune_old(tree):
    f = tree / "b" / "notes.txt"
    past = time.time() - 40 * 86400
    os.utime(f, (past, past))
    fm = FileManager()
    assert fm.prune_old(tree, 30).dry_run and f.exists()
    fm.prune_old(tree, 30, execute=True)
    assert not f.exists()


def test_bulk_rename_and_dry_run(tree):
    fm = FileManager()
    plan = fm.bulk_rename(tree, r"_R(\d)\.fastq\.gz$", r"_L001_R\1_001.fastq.gz",
                          pattern="*.fastq.gz", dry_run=True)
    assert len(plan) == 3 and (tree / "a" / "S1_R1.fastq.gz").exists()
    fm.bulk_rename(tree, r"_R(\d)\.fastq\.gz$", r"_L001_R\1_001.fastq.gz", pattern="*.fastq.gz")
    assert (tree / "a" / "S1_L001_R1_001.fastq.gz").exists()
    assert not (tree / "a" / "S1_R1.fastq.gz").exists()


def test_bulk_rename_aborts_on_collision(tree):
    write_fastq(tree / "a" / "X_R1.fastq.gz")
    write_fastq(tree / "a" / "Y_R1.fastq.gz")
    with pytest.raises(FileOperationError, match="renamed to"):
        FileManager().bulk_rename(tree / "a", r"^[XY]_", "Z_", pattern="*.fastq.gz")
    assert (tree / "a" / "X_R1.fastq.gz").exists()


def test_audit_log_is_valid_jsonl(tree, tmp_path):
    log = tmp_path / "logs" / "audit.jsonl"
    fm = FileManager(audit=AuditLog(log))
    fm.delete_files([tree / "b" / "notes.txt"], allowed_root=tree, dry_run=False)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0]["action"] == "delete" and rows[0]["bytes"] == 5 and "ts" in rows[0]
