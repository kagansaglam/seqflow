import json

import pytest

from seqflow import cli
from seqflow.cli import main
from seqflow.db import Database
from seqflow.lock import LockFile
from seqflow.notify import Notifier
from seqflow.watcher import write_heartbeat


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'ops.db'}"


def run_cli(url, *args):
    return main(["--db", url, *args])


def seed(url, **statuses):
    db = Database(url)
    db.upgrade()
    for run_id, status in statuses.items():
        db.get_or_create_run(run_id, "illumina", f"/x/{run_id}")
        db.add_sample(run_id, "S1", "projA", "AAAA", "CCCC", 1)
        db.add_fastq(run_id, "S1", f"/o/{run_id}.fastq.gz", "R1", 2_000_000_000, "a" * 32)
        if status == "failed":
            db.log_event(run_id, "error", "bcl-convert exited 3")
        db.set_status(run_id, status)
    db.engine.dispose()


def test_health_exit_codes(tmp_path, capsys):
    beat = tmp_path / "beat.json"
    assert main(["health", "--heartbeat-file", str(beat)]) == 1
    assert "no heartbeat file" in capsys.readouterr().err
    write_heartbeat(beat, 3)
    assert main(["health", "--heartbeat-file", str(beat)]) == 0
    assert "alive" in capsys.readouterr().out
    assert main(["health", "--heartbeat-file", str(beat), "--max-age", "0"]) == 1


def test_requeue_a_failed_run(url, capsys):
    seed(url, R1="failed")
    assert run_cli(url, "requeue", "R1") == 0
    assert "failed -> detected" in capsys.readouterr().out
    assert run_cli(url, "report", "--format", "json", "status") == 0
    assert json.loads(capsys.readouterr().out) == [{"status": "detected", "runs": 1}]
    assert run_cli(url, "report", "events", "R1", "--level", "info") == 0
    assert "requeued from failed" in capsys.readouterr().out


def test_requeue_refuses_done_and_in_flight_runs_unless_forced(url, capsys):
    seed(url, DONE="done", BUSY="demultiplexing")
    assert run_cli(url, "requeue", "DONE") == 1
    assert "use --force" in capsys.readouterr().err
    assert run_cli(url, "requeue", "BUSY") == 1
    assert "only failed runs" in capsys.readouterr().err
    assert run_cli(url, "requeue", "BUSY", "--force") == 1  # in flight is never allowed
    capsys.readouterr()
    assert run_cli(url, "requeue", "DONE", "--force") == 0
    assert run_cli(url, "requeue", "NOPE") == 1
    assert "unknown run" in capsys.readouterr().err


def test_digest_print(url, capsys):
    seed(url, R1="failed", R2="done")
    assert run_cli(url, "digest", "--print") == 0
    out = capsys.readouterr().out
    assert "attention needed" in out and "R1 (illumina): bcl-convert exited 3" in out


def test_digest_without_channels_explains_what_to_configure(url, monkeypatch, capsys):
    for key in ("SEQFLOW_SLACK_WEBHOOK", "SEQFLOW_SMTP_HOST", "SEQFLOW_MAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    seed(url, R1="done")
    assert run_cli(url, "digest") == 1
    assert "SEQFLOW_SLACK_WEBHOOK" in capsys.readouterr().err


def test_digest_is_sent_to_the_alert_channels(url, monkeypatch, capsys):
    class Recorder:
        msgs: list

        def __init__(self):
            self.msgs = []

        def send(self, level, subject, body):
            self.msgs.append((level, subject, body))

    alert = Recorder()
    monkeypatch.setattr(cli, "notifier_from_env", lambda: Notifier([], [alert]))
    seed(url, R1="failed")
    assert run_cli(url, "digest") == 0
    assert alert.msgs[0][0] == "error" and "R1" in alert.msgs[0][2]
    assert capsys.readouterr().out.startswith("sent: digest: attention needed")


def test_watch_refuses_to_start_when_another_instance_holds_the_lock(url, tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    with LockFile(runs / ".seqflow.lock"):
        assert run_cli(url, "watch", str(runs), "--platform", "illumina") == 1
    assert "holds" in capsys.readouterr().err
