import os
import subprocess
import sys
import time

import pytest

from seqflow.exceptions import LockHeldError
from seqflow.lock import LockFile, pid_alive


def finished_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(finished_pid())
    assert not pid_alive(0) and not pid_alive(-5)


def test_acquire_writes_pid_and_release_removes_file(tmp_path):
    path = tmp_path / "sub" / "x.lock"
    lock = LockFile(path)
    lock.acquire()
    assert path.read_text() == str(os.getpid())
    lock.release()
    assert not path.exists()
    lock.release()  # releasing twice is harmless


def test_context_manager(tmp_path):
    path = tmp_path / "x.lock"
    with LockFile(path):
        assert path.exists()
    assert not path.exists()


def test_second_holder_is_refused_while_first_is_alive(tmp_path):
    path = tmp_path / "x.lock"
    with LockFile(path), pytest.raises(LockHeldError, match=str(os.getpid())):
        LockFile(path).acquire()
    assert not path.exists()


def test_stale_lock_of_dead_process_is_taken_over(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text(str(finished_pid()))
    with LockFile(path):
        assert path.read_text() == str(os.getpid())


def test_alive_function_can_be_injected(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("999999")
    with pytest.raises(LockHeldError):
        LockFile(path, alive=lambda pid: True).acquire()
    with LockFile(path, alive=lambda pid: False):
        pass


def test_empty_lock_file_is_respected_when_fresh_and_stale_when_old(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("")
    with pytest.raises(LockHeldError, match="being created"):
        LockFile(path).acquire()
    old = time.time() - 3600
    os.utime(path, (old, old))
    with LockFile(path):
        assert path.read_text() == str(os.getpid())


def test_corrupt_old_lock_file_is_stale(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("not a pid")
    old = time.time() - 3600
    os.utime(path, (old, old))
    with LockFile(path):
        pass


def test_release_leaves_a_lock_owned_by_someone_else(tmp_path):
    path = tmp_path / "x.lock"
    lock = LockFile(path)
    lock.acquire()
    path.write_text("424242")  # someone took over after we stalled
    lock.release()
    assert path.read_text() == "424242"


CHILD = """
import os, sys
from seqflow.lock import LockFile
with LockFile(sys.argv[1]):
    print("ready", os.getpid(), flush=True)
    sys.stdin.readline()
"""


def test_lock_works_across_real_processes(tmp_path):
    path = tmp_path / "x.lock"
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        word, holder_pid = child.stdout.readline().split()
        assert word == "ready"
        # On Windows a venv python.exe is a launcher, so Popen.pid is not the pid of the
        # process that holds the lock; the child reports its own.
        with pytest.raises(LockHeldError, match=holder_pid):
            LockFile(path).acquire()
    finally:
        child.communicate("\n", timeout=30)
    assert not path.exists()  # the child released it on exit
    with LockFile(path):
        pass
