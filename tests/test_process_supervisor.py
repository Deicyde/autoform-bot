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
    lock_paths: tuple[Path, ...],
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
            "-",
            *(str(path) for path in lock_paths),
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
        *(str(path) for path in lock_paths),
        "--",
        *child_command,
    ]


def _spawn_parent(
    backend: str,
    lock_paths: tuple[Path, ...],
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

backend, identity_path, marker_path, pid_path, *lock_paths = sys.argv[1:]
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
        str(os.getpid()), "-", *lock_paths, "--",
        sys.executable, "-c", child_code, marker_path,
    ]
else:
    command = [
        sys.executable, "-I", "-m", "servers.lsp.launcher",
        identity_path, str(os.getpid()), *lock_paths, "--",
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
            str(identity_path),
            str(marker_path),
            str(pid_path),
            *(str(path) for path in lock_paths),
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
    signals = []
    observed = []

    monkeypatch.setattr(
        process_supervisor,
        "_same_process",
        lambda process: next(states[id(process)]),
    )
    monkeypatch.setattr(process_supervisor.time, "sleep", lambda delay: None)
    monkeypatch.setattr(
        process_supervisor,
        "_signal_group",
        lambda process_group_id, signal_number: signals.append((process_group_id, signal_number)),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_observe_group_until_gone",
        lambda process_group_id: observed.append(process_group_id),
    )

    process_supervisor._monitor_until_cleanup(parent, target, 1234)

    assert signals == [(1234, signal.SIGKILL)]
    assert observed == [1234]


def test_watchdog_never_signals_after_target_identity_is_lost(monkeypatch) -> None:
    parent = object()
    target = object()
    observed = []

    monkeypatch.setattr(
        process_supervisor,
        "_same_process",
        lambda process: process is parent,
    )
    monkeypatch.setattr(
        process_supervisor,
        "_signal_group",
        lambda *args: pytest.fail("an unanchored process group must not be signaled"),
    )
    monkeypatch.setattr(
        process_supervisor,
        "_observe_group_until_gone",
        lambda process_group_id: observed.append(process_group_id),
    )

    process_supervisor._monitor_until_cleanup(parent, target, 1234)

    assert observed == [1234]


@pytest.mark.parametrize("backend", ["repl", "lsp"])
def test_parent_death_before_child_lock_never_execs_command(
    tmp_path: Path,
    backend: str,
) -> None:
    import fcntl

    lock_paths = (
        tmp_path / "runtime.lifetime.lock",
        tmp_path / "legacy.lifetime.lock",
    )
    identity_path = tmp_path / "identity.json"
    marker_path = tmp_path / "child-ready"
    pid_path = tmp_path / "wrapper-pid"
    first_fd = os.open(lock_paths[0], os.O_CREAT | os.O_RDWR, 0o600)
    blocked_fd = os.open(lock_paths[1], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(blocked_fd, fcntl.LOCK_EX)
    parent = _spawn_parent(
        backend,
        lock_paths,
        identity_path,
        marker_path,
        pid_path,
    )
    try:
        _wait_for_path(pid_path, parent)
        wrapper_pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fcntl.flock(first_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                break
            else:
                fcntl.flock(first_fd, fcntl.LOCK_UN)
                time.sleep(0.01)
        else:
            pytest.fail("launcher did not acquire its first shared lifetime lock")

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=2)

        deadline = time.monotonic() + 5
        while _live_process_group(wrapper_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _live_process_group(wrapper_pid)
        assert not marker_path.exists()
        fcntl.flock(first_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(blocked_fd)
        os.close(first_fd)
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

    lock_paths = (
        tmp_path / "runtime.lifetime.lock",
        tmp_path / "legacy.lifetime.lock",
    )
    identity_path = tmp_path / "identity.json"
    marker_path = tmp_path / "child-ready"
    pid_path = tmp_path / "wrapper-pid"
    parent = _spawn_parent(
        backend,
        lock_paths,
        identity_path,
        marker_path,
        pid_path,
    )
    fence_fds = [os.open(path, os.O_CREAT | os.O_RDWR, 0o600) for path in lock_paths]
    watcher_pid = None
    try:
        _wait_for_path(marker_path, parent)
        wrapper_pid = int(pid_path.read_text(encoding="utf-8"))
        if backend == "lsp":
            assert identity_path.exists()
        for fence_fd in fence_fds:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            children = psutil.Process(wrapper_pid).children()
            if children:
                watcher_pid = children[0].pid
                break
            time.sleep(0.01)
        assert watcher_pid is not None
        os.kill(watcher_pid, signal.SIGSTOP)
        deadline = time.monotonic() + 2
        while psutil.Process(watcher_pid).status() != psutil.STATUS_STOPPED and time.monotonic() < deadline:
            time.sleep(0.01)
        assert psutil.Process(watcher_pid).status() == psutil.STATUS_STOPPED
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=2)
        assert _live_process_group(wrapper_pid)
        for fence_fd in fence_fds:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        os.kill(watcher_pid, signal.SIGCONT)
        watcher_pid = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                for fence_fd in fence_fds:
                    fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                for fence_fd in fence_fds:
                    fcntl.flock(fence_fd, fcntl.LOCK_UN)
                time.sleep(0.01)
        else:
            pytest.fail("replacement remained fenced after orphan cleanup")
        assert not _live_process_group(wrapper_pid)
    finally:
        if watcher_pid is not None:
            try:
                os.kill(watcher_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        for fence_fd in fence_fds:
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

    lock_paths = (
        tmp_path / "runtime.lifetime.lock",
        tmp_path / "legacy.lifetime.lock",
    )
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
            lock_paths=lock_paths,
            identity_path=identity_path,
            child_command=[sys.executable, "-c", child_code, str(marker_path)],
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    fence_fds = [os.open(path, os.O_CREAT | os.O_RDWR, 0o600) for path in lock_paths]
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
                for fence_fd in fence_fds:
                    fcntl.flock(fence_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                for fence_fd in fence_fds:
                    fcntl.flock(fence_fd, fcntl.LOCK_UN)
                time.sleep(0.01)
        else:
            pytest.fail("watchdog retained the child fence after process exit")
        for fence_fd in fence_fds:
            os.close(fence_fd)
