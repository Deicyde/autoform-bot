"""Process-level tests for crash-safe Lean child supervision."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from servers import process_supervisor


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="the shared runtime requires POSIX process groups and flock",
)


def _live_process_group(process_group_id: int) -> bool:
    for candidate in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(candidate.info["pid"]) != process_group_id:
                continue
            if candidate.info["status"] != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, PermissionError, psutil.Error):
            continue
    return False


def _wait_for_path(path: Path, process: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else b""
            pytest.fail(f"process exited before publishing {path}: {stderr.decode(errors='replace')}")
        time.sleep(0.01)
    pytest.fail(f"process did not publish {path}")


def _launcher_command(
    backend: str,
    *,
    parent_pid: int,
    lock_path: Path,
    identity_path: Path,
    child_command: list[str],
) -> list[str]:
    if backend == "repl":
        return [
            sys.executable,
            "-I",
            "-m",
            "servers.process_supervisor",
            str(parent_pid),
            str(lock_path),
            "-",
            "--",
            *child_command,
        ]
    return [
        sys.executable,
        "-I",
        "-m",
        "servers.lsp.launcher",
        str(identity_path),
        str(parent_pid),
        str(lock_path),
        "--",
        *child_command,
    ]


def _spawn_parent(
    backend: str,
    lock_path: Path,
    identity_path: Path,
    marker_path: Path,
    pid_path: Path,
) -> subprocess.Popen:
    helper = """
import os
import subprocess
import sys
import time
from pathlib import Path

backend, lock_path, identity_path, marker_path, pid_path = sys.argv[1:]
child_code = '''
import signal
import sys
import time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
'''
if backend == "repl":
    command = [
        sys.executable, "-I", "-m", "servers.process_supervisor",
        str(os.getpid()), lock_path, "-", "--",
        sys.executable, "-c", child_code, marker_path,
    ]
else:
    command = [
        sys.executable, "-I", "-m", "servers.lsp.launcher",
        identity_path, str(os.getpid()), lock_path, "--",
        sys.executable, "-c", child_code, marker_path,
    ]
process = subprocess.Popen(
    command,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
Path(pid_path).write_text(str(process.pid), encoding="utf-8")
while True:
    time.sleep(1)
"""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            helper,
            backend,
            str(lock_path),
            str(identity_path),
            str(marker_path),
            str(pid_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def test_watchdog_steady_state_does_not_scan_the_process_table(monkeypatch) -> None:
    parent = object()
    target = object()
    states = {
        id(parent): iter((True, False)),
        id(target): iter((True, True)),
    }
    cleaned = []

    monkeypatch.setattr(
        process_supervisor,
        "_same_process",
        lambda process: next(states[id(process)]),
    )
    monkeypatch.setattr(process_supervisor.time, "sleep", lambda delay: None)
    monkeypatch.setattr(
        process_supervisor,
        "_group_has_live_members",
        lambda process_group_id: pytest.fail("steady-state supervision must not scan the process table"),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_terminate_group_until_gone",
        lambda process_group_id: cleaned.append(process_group_id),
    )

    process_supervisor._monitor_until_cleanup(parent, target, 1234)

    assert cleaned == [1234]


@pytest.mark.parametrize("backend", ["repl", "lsp"])
def test_parent_death_before_child_lock_never_execs_command(
    tmp_path: Path,
    backend: str,
) -> None:
    import fcntl

    lock_path = tmp_path / "children.lock"
    identity_path = tmp_path / "identity.json"
    marker_path = tmp_path / "child-ready"
    pid_path = tmp_path / "wrapper-pid"
    fence_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fence_fd, fcntl.LOCK_EX)
    parent = _spawn_parent(
        backend,
        lock_path,
        identity_path,
        marker_path,
        pid_path,
    )
    try:
        _wait_for_path(pid_path, parent)
        wrapper_pid = int(pid_path.read_text(encoding="utf-8"))
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=2)
        os.close(fence_fd)
        fence_fd = -1

        deadline = time.monotonic() + 5
        while _live_process_group(wrapper_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _live_process_group(wrapper_pid)
        assert not marker_path.exists()
    finally:
        if fence_fd >= 0:
            os.close(fence_fd)
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        try:
            os.killpg(wrapper_pid, signal.SIGKILL)
        except (UnboundLocalError, ProcessLookupError):
            pass


@pytest.mark.parametrize("backend", ["repl", "lsp"])
def test_parent_sigkill_holds_fence_until_child_group_is_gone(
    tmp_path: Path,
    backend: str,
) -> None:
    import fcntl

    lock_path = tmp_path / "children.lock"
    identity_path = tmp_path / "identity.json"
    marker_path = tmp_path / "child-ready"
    pid_path = tmp_path / "wrapper-pid"
    parent = _spawn_parent(
        backend,
        lock_path,
        identity_path,
        marker_path,
        pid_path,
    )
    fence_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _wait_for_path(marker_path, parent)
        wrapper_pid = int(pid_path.read_text(encoding="utf-8"))
        if backend == "lsp":
            assert identity_path.exists()
        with pytest.raises(BlockingIOError):
            fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=2)
        assert _live_process_group(wrapper_pid)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        else:
            pytest.fail("replacement remained fenced after orphan cleanup")
        assert not _live_process_group(wrapper_pid)
    finally:
        os.close(fence_fd)
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        try:
            os.killpg(wrapper_pid, signal.SIGKILL)
        except (UnboundLocalError, ProcessLookupError):
            pass


def test_watchdog_does_not_keep_protocol_pipes_open(tmp_path: Path) -> None:
    import fcntl

    lock_path = tmp_path / "children.lock"
    marker_path = tmp_path / "closed-protocol-fds"
    identity_path = tmp_path / "unused.json"
    child_code = """
import os
import sys
import time
from pathlib import Path
os.close(0)
os.close(1)
os.close(2)
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
time.sleep(30)
"""
    process = subprocess.Popen(
        _launcher_command(
            "repl",
            parent_pid=os.getpid(),
            lock_path=lock_path,
            identity_path=identity_path,
            child_command=[sys.executable, "-c", child_code, str(marker_path)],
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    fence_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _wait_for_path(marker_path, process)
        assert process.stdout is not None
        assert process.stderr is not None
        readable, _, _ = select.select(
            [process.stdout, process.stderr],
            [],
            [],
            1,
        )
        assert set(readable) == {process.stdout, process.stderr}
        assert process.stdout.read() == b""
        assert process.stderr.read() == b""
        assert process.poll() is None

        assert process.stdin is not None
        with pytest.raises(BrokenPipeError):
            process.stdin.write(b"request\n")
            process.stdin.flush()
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        else:
            pytest.fail("watchdog retained the child fence after process exit")
        os.close(fence_fd)
