import json

import pytest

from seqflow.bench import benchmark, format_markdown, speedup_warnings
from seqflow.checksum import ChecksumManager
from seqflow.cli import main
from seqflow.synth import MARKER, generate_fileset


def test_generate_is_deterministic(tmp_path):
    a = generate_fileset(tmp_path / "a", 5, min_bytes=100, max_bytes=200, seed=7)
    b = generate_fileset(tmp_path / "b", 5, min_bytes=100, max_bytes=200, seed=7)
    assert a.total_bytes == b.total_bytes
    sums_a = ChecksumManager().compute_many(sorted((tmp_path / "a").rglob("*.fastq.gz")))
    sums_b = ChecksumManager().compute_many(sorted((tmp_path / "b").rglob("*.fastq.gz")))
    assert list(sums_a.values()) == list(sums_b.values())


def test_generate_layout_and_marker(tmp_path):
    s = generate_fileset(tmp_path, 250, min_bytes=10, max_bytes=20, files_per_dir=100)
    assert s.n_files == 250
    assert len(list(tmp_path.glob("batch_*"))) == 3
    assert (tmp_path / MARKER).exists()


def test_generate_paired_names(tmp_path):
    generate_fileset(tmp_path, 4, min_bytes=1, max_bytes=2, paired=True)
    names = sorted(p.name for p in tmp_path.rglob("*.fastq.gz"))
    assert names == ["S000000_R1.fastq.gz", "S000000_R2.fastq.gz",
                     "S000001_R1.fastq.gz", "S000001_R2.fastq.gz"]


@pytest.mark.parametrize("kwargs", [
    {"n_files": 0}, {"n_files": 1, "min_bytes": 10, "max_bytes": 5},
    {"n_files": 1, "files_per_dir": 0},
])
def test_generate_rejects_bad_args(tmp_path, kwargs):
    with pytest.raises(ValueError):
        generate_fileset(tmp_path, **kwargs)


def test_benchmark_structure_and_markdown(tmp_path):
    generate_fileset(tmp_path / "d", 20, min_bytes=1000, max_bytes=2000)
    res = benchmark(tmp_path / "d", workers=(1, 2), repeats=2)
    assert res["n_files"] == 20 and [r["workers"] for r in res["md5"]] == [1, 2]
    assert res["archive"] is None
    assert "workers" in format_markdown(res)


def test_benchmark_warmup_speedup_and_progress(tmp_path):
    generate_fileset(tmp_path / "d", 10, min_bytes=1000, max_bytes=2000)
    msgs = []
    res = benchmark(tmp_path / "d", workers=(1, 2), repeats=1, progress=msgs.append)
    assert res["first_read_seconds"] is not None
    assert res["md5"][0]["speedup"] == 1.0
    assert any("warm-up" in m for m in msgs)
    assert "speedup" in format_markdown(res)


def test_benchmark_without_warmup_is_flagged(tmp_path):
    generate_fileset(tmp_path / "d", 3, min_bytes=100, max_bytes=200)
    res = benchmark(tmp_path / "d", workers=(1,), repeats=1, warmup=False)
    assert res["first_read_seconds"] is None
    assert any("no warm-up" in w for w in res["warnings"])


def test_speedup_warnings_flags_superlinear_only():
    ok = [{"workers": 1, "speedup": 1.0}, {"workers": 4, "speedup": 3.6}]
    bad = [{"workers": 1, "speedup": 1.0}, {"workers": 4, "speedup": 6.8}]
    assert speedup_warnings(ok) == []
    assert "6.8x speedup" in speedup_warnings(bad)[0]
    assert speedup_warnings([]) == []


def test_synth_progress_callback(tmp_path):
    calls = []
    generate_fileset(tmp_path, 1100, min_bytes=1, max_bytes=2,
                     progress=lambda d, t: calls.append((d, t)))
    assert calls == [(500, 1100), (1000, 1100), (1100, 1100)]


def test_benchmark_does_not_write_into_root(tmp_path):
    generate_fileset(tmp_path / "d", 5, min_bytes=100, max_bytes=200)
    before = sorted(p.name for p in (tmp_path / "d").rglob("*"))
    benchmark(tmp_path / "d", workers=(1,), repeats=1)
    assert before == sorted(p.name for p in (tmp_path / "d").rglob("*"))


def test_benchmark_with_archive(tmp_path):
    generate_fileset(tmp_path / "d", 5, min_bytes=100, max_bytes=200)
    res = benchmark(tmp_path / "d", workers=(2,), repeats=1, archive_to=tmp_path / "arch")
    assert res["archive"]["verified"] is True


def test_benchmark_rejects_zero_repeats(tmp_path):
    generate_fileset(tmp_path / "d", 1, min_bytes=1, max_bytes=1)
    with pytest.raises(ValueError):
        benchmark(tmp_path / "d", repeats=0)


def test_cli_file_commands(tmp_path, capsys):
    root = tmp_path / "d"
    assert main(["synth", str(root), "--files", "6", "--min-kb", "1", "--max-kb", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["n_files"] == 6

    assert main(["inventory", str(root), "--pattern", "*.fastq.gz"]) == 0
    assert json.loads(capsys.readouterr().out)["n_files"] == 6

    log = tmp_path / "audit.jsonl"
    assert main(["archive", str(root), str(tmp_path / "arch"), "--dry-run",
                 "--audit-log", str(log)]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert log.exists()

    assert main(["dedupe", str(root), "--pattern", "*.fastq.gz"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert main(["rename", str(root), "--regex", "_R1", "--repl", "_R1_x",
                 "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["renamed"] == 6

    assert main(["prune", str(root), "--older-than-days", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["n_files"] == 0

    assert main(["bench", str(root), "--workers", "1", "--repeats", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["n_files"] == 6
