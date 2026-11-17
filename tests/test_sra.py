"""SraFetcher with stand-in prefetch / fasterq-dump scripts (POSIX only).

The stand-ins read a JSON file (STUB_CONFIG) that says, per run, how many reads to
write, whether the run is paired and how many times prefetch should fail first.
"""
import gzip
import json
import os
import sys
from pathlib import Path

import pytest

from seqflow.cli import main
from seqflow.db import Database
from seqflow.exceptions import FetchError
from seqflow.sra import RunSpec, SraFetcher, gzip_and_count, read_run_list

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stand-in tools are POSIX scripts")

PREFETCH = """#!{python}
import json, os, sys
from pathlib import Path
cfg_path = Path(os.environ["STUB_CONFIG"])
cfg = json.loads(cfg_path.read_text())
acc = sys.argv[1]
out = Path(sys.argv[sys.argv.index("-O") + 1])
run = cfg[acc]
if run.get("fail_prefetch", 0) > 0:
    run["fail_prefetch"] -= 1
    cfg_path.write_text(json.dumps(cfg))
    sys.stderr.write("connection reset by peer\\n")
    sys.exit(3)
(out / acc).mkdir(parents=True, exist_ok=True)
if not run.get("no_file"):
    (out / acc / f"{{acc}}.{{'sralite' if run.get('lite') else 'sra'}}").write_text(acc)
"""

DUMP = """#!{python}
import json, os, sys
from pathlib import Path
cfg = json.loads(Path(os.environ["STUB_CONFIG"]).read_text())
sra = Path(sys.argv[1])
if not sra.exists():
    sys.stderr.write(f"cannot find '{{sra}}'\\n")
    sys.exit(3)
out = Path(sys.argv[sys.argv.index("-O") + 1])
out.mkdir(parents=True, exist_ok=True)
acc = sra.stem
run = cfg[acc]
names = [f"{{acc}}_1.fastq", f"{{acc}}_2.fastq"] if run["paired"] else [f"{{acc}}.fastq"]
for name in names:
    with open(out / name, "w") as fh:
        for i in range(run["reads"]):
            fh.write(f"@{{acc}}.{{i}}\\nACGT\\n+\\nIIII\\n")
"""


@pytest.fixture
def stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("prefetch", PREFETCH), ("fasterq-dump", DUMP)):
        tool = bin_dir / name
        tool.write_text(body.format(python=sys.executable))
        tool.chmod(0o755)
    cfg = tmp_path / "stub.json"
    monkeypatch.setenv("STUB_CONFIG", str(cfg))

    def configure(runs: dict) -> Path:
        cfg.write_text(json.dumps(runs))
        return bin_dir

    return configure


def fetcher(tmp_path, bin_dir, **kw) -> SraFetcher:
    return SraFetcher(tmp_path / "out", tools_dir=bin_dir, workers=kw.pop("workers", 3),
                      backoff=0, **kw)


def records(path: Path) -> int:
    with gzip.open(path, "rt") as fh:
        return sum(1 for _ in fh) // 4


def test_single_and_paired_runs_are_fetched_counted_and_logged(stub, tmp_path):
    bin_dir = stub({"ERR1": {"reads": 7, "paired": False}, "ERR2": {"reads": 5, "paired": True}})
    specs = [RunSpec("ERR1", False, 7), RunSpec("ERR2", True, 5)]
    seen = []
    f = fetcher(tmp_path, bin_dir, on_result=lambda r, n, t: seen.append((r.accession, n, t)))
    summary = f.run(specs)
    assert (summary.ok, summary.failed, summary.reads) == (2, 0, 12)
    out = tmp_path / "out"
    assert records(out / "ERR1.fastq.gz") == 7
    assert records(out / "ERR2_1.fastq.gz") == records(out / "ERR2_2.fastq.gz") == 5
    assert sorted(a for a, _, _ in seen) == ["ERR1", "ERR2"] and {t for _, _, t in seen} == {2}
    assert not list((tmp_path / "sra_work").iterdir())  # scratch cleaned up
    log = [json.loads(x) for x in (out / "fetch_log.jsonl").read_text().splitlines()]
    assert {r["accession"]: r["status"] for r in log} == {"ERR1": "ok", "ERR2": "ok"}


def test_read_count_mismatch_fails_without_retry_and_leaves_no_files(stub, tmp_path):
    bin_dir = stub({"ERR3": {"reads": 9, "paired": False}})
    summary = fetcher(tmp_path, bin_dir).run([RunSpec("ERR3", False, 10)])
    assert summary.failed == 1 and summary.failed_runs == ["ERR3"]
    rec = json.loads((tmp_path / "out" / "fetch_log.jsonl").read_text())
    assert rec["attempts"] == 1 and "differs from the expected 10" in rec["error"]
    assert not (tmp_path / "out" / "ERR3.fastq.gz").exists()


def test_transient_failure_is_retried(stub, tmp_path):
    bin_dir = stub({"ERR4": {"reads": 3, "paired": False, "fail_prefetch": 2}})
    f = fetcher(tmp_path, bin_dir, retries=3)
    res = f.fetch(RunSpec("ERR4", False, 3))
    assert res.status == "ok" and res.attempts == 3
    bin_dir = stub({"ERR5": {"reads": 3, "paired": False, "fail_prefetch": 5}})
    res = fetcher(tmp_path, bin_dir, retries=2).fetch(RunSpec("ERR5", False, 3))
    assert res.status == "failed" and "connection reset" in res.error and res.attempts == 2


def test_an_interrupted_batch_resumes_and_refetches_missing_files(stub, tmp_path):
    bin_dir = stub({f"ERR{i}": {"reads": 2, "paired": False} for i in range(10, 14)})
    specs = [RunSpec(f"ERR{i}", False, 2) for i in range(10, 14)]
    assert fetcher(tmp_path, bin_dir).run(specs[:2]).ok == 2  # first, partial batch
    (tmp_path / "out" / "ERR10.fastq.gz").unlink()  # a file lost since then
    summary = fetcher(tmp_path, bin_dir).run(specs)
    assert (summary.skipped, summary.ok) == (1, 3)


def test_sra_lite_downloads_are_used(stub, tmp_path):
    bin_dir = stub({"ERR9": {"reads": 4, "paired": False, "lite": True},
                    "ERR0": {"reads": 4, "paired": False, "no_file": True}})
    f = fetcher(tmp_path, bin_dir, retries=1)
    assert f.fetch(RunSpec("ERR9", False, 4)).status == "ok"
    res = f.fetch(RunSpec("ERR0", False, 4))
    assert res.status == "failed" and "no .sra/.sralite file" in res.error


def test_wrong_layout_is_reported(stub, tmp_path):
    bin_dir = stub({"ERR6": {"reads": 2, "paired": False}})
    res = fetcher(tmp_path, bin_dir).fetch(RunSpec("ERR6", True, 2))
    assert res.status == "failed" and "check library_layout" in res.error


def test_gzip_and_count_rejects_truncated_fastq(tmp_path):
    src = tmp_path / "x.fastq"
    src.write_text("@r\nAC\n+\n")
    with pytest.raises(FetchError, match="multiple of 4"):
        gzip_and_count(src, tmp_path / "x.fastq.gz")
    assert not (tmp_path / "x.fastq.gz").exists()


def test_run_list_parsing(tmp_path):
    f = tmp_path / "runs.tsv"
    f.write_text("run_accession\tlibrary_layout\tread_count\nERR1\tSINGLE\t10\nERR2\tPAIRED\t\n")
    assert read_run_list(f) == [RunSpec("ERR1", False, 10), RunSpec("ERR2", True, None)]
    f.write_text("run_accession\nERR1\nERR1\n")
    with pytest.raises(FetchError, match="twice"):
        read_run_list(f)


def test_missing_tools_are_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FetchError, match="prefetch not found"):
        SraFetcher(tmp_path / "out")


def test_fetch_sra_command_registers_the_files(stub, tmp_path, capsys):
    bin_dir = stub({"ERR7": {"reads": 4, "paired": False}, "ERR8": {"reads": 6, "paired": False}})
    runs = tmp_path / "runs.tsv"
    runs.write_text("run_accession\tlibrary_layout\tread_count\nERR7\tSINGLE\t4\nERR8\tSINGLE\t6\n")
    url = f"sqlite:///{tmp_path / 'f.db'}"
    trial = ["--db", url, "fetch-sra", str(runs), "--out", str(tmp_path / "out"),
             "--tools-dir", str(bin_dir)]
    assert main([*trial, "--limit", "1"]) == 0
    assert "[1/1] ERR7 ok" in capsys.readouterr().out
    code = main([*trial, "--register-run", "AGP", "--project", "agp"])
    out = capsys.readouterr().out
    assert code == 0 and '"skipped": 1' in out and '"ok": 1' in out and "[2/2]" in out
    db = Database(url)
    try:
        assert db.run_summary("AGP")["fastq_files"] == 2
    finally:
        db.engine.dispose()
