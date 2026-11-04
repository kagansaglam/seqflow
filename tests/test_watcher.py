import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from seqflow.notify import Notifier
from seqflow.watcher import RunWatcher, check_heartbeat, install_signal_handlers, write_heartbeat


class Recorder:
    def __init__(self):
        self.msgs = []

    def send(self, level, subject, body):
        self.msgs.append((level, subject, body))


class FakeOrchestrator:
    def __init__(self, notifier, fail=(), on_call=None):
        self.notifier, self.fail, self.on_call = notifier, set(fail), on_call
        self.calls = []

    def process_run(self, run_dir, platform):
        name = Path(run_dir).name
        self.calls.append(name)
        if self.on_call:
            self.on_call(name)
        if name in self.fail:
            raise RuntimeError("database unreachable")
        return "done"


@pytest.fixture
def setup(db, tmp_path):
    runs = tmp_path / "runs"
    for name in ("A", "B", "C"):
        (runs / name).mkdir(parents=True)
    (runs / "stray_file.txt").write_text("not a run")
    rec = Recorder()
    notifier = Notifier([rec])

    def make(**kw):
        orch = FakeOrchestrator(notifier, **kw.pop("orch", {}))
        return RunWatcher(runs, "illumina", db, orch, interval=0, **kw), orch

    return make, rec, runs


def test_scan_processes_run_directories_only(setup):
    make, _, _ = setup
    watcher, orch = make()
    assert watcher.scan_once() == ["A", "B", "C"]
    assert orch.calls == ["A", "B", "C"]


def test_scan_skips_runs_in_a_terminal_state(setup, db):
    make, _, runs = setup
    for name, status in [("B", "done"), ("C", "failed")]:
        db.get_or_create_run(name, "illumina", str(runs / name))
        db.set_status(name, status)
    watcher, _ = make()
    assert watcher.scan_once() == ["A"]


def test_one_crashing_run_does_not_stop_the_others(setup):
    make, rec, _ = setup
    watcher, _ = make(orch={"fail": ["B"]})
    assert watcher.scan_once() == ["A", "B", "C"]
    assert [(lvl, subj) for lvl, subj, _ in rec.msgs] == [("error", "B: processing crashed")]
    assert "database unreachable" in rec.msgs[0][2]


def test_stop_request_ends_the_scan_between_runs(setup):
    make, _, _ = setup
    stop = threading.Event()
    watcher, orch = make(stop=stop, orch={"on_call": lambda name: stop.set()})
    assert watcher.scan_once() == ["A"]
    assert orch.calls == ["A"]


def test_run_stops_when_requested_and_counts_scans(setup):
    make, _, _ = setup
    stop = threading.Event()
    watcher, _ = make(stop=stop, orch={"on_call": lambda n: stop.set() if n == "C" else None})
    assert watcher.run() == 1
    assert watcher.run(max_cycles=5) == 1  # already stopped: nothing more happens
    pre_stopped, orch2 = make(stop=threading.Event())
    pre_stopped.stop.set()
    assert pre_stopped.run() == 0 and orch2.calls == []


def test_run_honours_max_cycles(setup):
    make, _, _ = setup
    watcher, orch = make()
    assert watcher.run(max_cycles=3) == 3
    assert orch.calls == ["A", "B", "C"] * 3


def test_persistent_scan_failure_is_reported_once_then_recovery(setup, monkeypatch):
    make, rec, _ = setup
    watcher, _ = make()
    outcomes = iter([OSError("share unmounted"), OSError("share unmounted"),
                     OSError("share unmounted"), None, None])

    def flaky_scan():
        outcome = next(outcomes)
        if outcome:
            raise outcome
        return []

    monkeypatch.setattr(watcher, "scan_once", flaky_scan)
    assert watcher.run(max_cycles=5) == 5
    assert [(lvl, subj) for lvl, subj, _ in rec.msgs] == [
        ("error", "watcher scan failed"), ("info", "watcher recovered")]
    assert "share unmounted" in rec.msgs[0][2]


def test_missing_watch_directory_is_a_scan_failure_not_a_crash(db, tmp_path):
    rec = Recorder()
    watcher = RunWatcher(tmp_path / "gone", "illumina", db,
                         FakeOrchestrator(Notifier([rec])), interval=0)
    assert watcher.run(max_cycles=2) == 2
    assert len(rec.msgs) == 1 and rec.msgs[0][1] == "watcher scan failed"


def test_heartbeat_is_written_every_scan(setup, tmp_path):
    make, _, _ = setup
    beat = tmp_path / "state" / "beat.json"
    watcher, _ = make(heartbeat_path=beat)
    watcher.run(max_cycles=2)
    data = json.loads(beat.read_text())
    assert data["cycles"] == 2 and data["pid"] == os.getpid()
    assert not beat.with_name("beat.json.tmp").exists()
    assert check_heartbeat(beat, 60)[0] is True


def test_check_heartbeat_detects_missing_old_and_corrupt(tmp_path):
    beat = tmp_path / "beat.json"
    ok, msg = check_heartbeat(beat, 60)
    assert not ok and "no heartbeat file" in msg
    write_heartbeat(beat, 7)
    ok, msg = check_heartbeat(beat, 60)
    assert ok and "7 scans" in msg
    ok, msg = check_heartbeat(beat, 60, now=time.time() + 1000)
    assert not ok and "ago" in msg
    beat.write_text("{broken")
    ok, msg = check_heartbeat(beat, 60)
    assert not ok and "unreadable" in msg


posix_only = pytest.mark.skipif(os.name == "nt", reason="needs POSIX signals")


@posix_only
def test_signal_handler_requests_a_stop_and_restores_old_handlers():
    stop = threading.Event()
    before = signal.getsignal(signal.SIGTERM)
    restore = install_signal_handlers(stop)
    try:
        assert not stop.is_set()
        signal.raise_signal(signal.SIGTERM)
        assert stop.is_set()
    finally:
        restore()
    assert signal.getsignal(signal.SIGTERM) == before


@posix_only
def test_watch_process_shuts_down_cleanly_on_sigterm(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    env = {**os.environ, "SEQFLOW_DB": f"sqlite:///{tmp_path / 'w.db'}"}
    for key in ("SEQFLOW_SLACK_WEBHOOK", "SEQFLOW_SMTP_HOST", "SEQFLOW_MAIL_TO"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "seqflow.cli", "watch", str(runs), "--platform", "illumina",
         "--interval", "0.2", "--no-launch"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    beat, lock = runs / ".seqflow-heartbeat.json", runs / ".seqflow.lock"
    try:
        deadline = time.time() + 60
        while not beat.exists() and time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert beat.exists(), "watcher never wrote a heartbeat"
        assert lock.read_text() == str(proc.pid)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    assert not lock.exists()
    assert "stopped after" in err


@posix_only
def test_second_signal_kills_a_process_that_ignores_the_first(tmp_path):
    code = (
        "import threading, time\n"
        "from seqflow.watcher import install_signal_handlers\n"
        "install_signal_handlers(threading.Event())\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"  # a stuck run: it never looks at the stop flag
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        assert proc.poll() is None, "first signal must only request a stop"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
