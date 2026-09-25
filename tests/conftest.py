"""Pytest configuration for autoform tests."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from servers.lean_client import LeanRuntimeClient, LeanRuntimeError


def _terminate_test_runtime(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
        processes = [*parent.children(recursive=True), parent]
        parent.terminate()
    except psutil.NoSuchProcess:
        return
    _, alive = psutil.wait_procs(processes, timeout=10)
    for process in reversed(alive):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=2)
    if alive:
        raise RuntimeError(
            "test Lean runtime processes survived cleanup: "
            + ", ".join(str(process.pid) for process in alive)
        )


def _stop_test_runtimes(directory: Path) -> None:
    for path in directory.iterdir():
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(metadata.st_mode):
            continue
        client = LeanRuntimeClient(
            socket_path=path,
            autostart=False,
            connect_timeout=0.25,
            response_timeout=15.0,
            startup_timeout=2.0,
        )
        pid = None
        try:
            status = client.ping()
            candidate = status.get("pid")
            if isinstance(candidate, int) and candidate != os.getpid():
                pid = candidate
            client.stop()
        except LeanRuntimeError:
            if pid is not None:
                _terminate_test_runtime(pid)


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def runtime_dir() -> Iterator[Path]:
    """A directory short enough to hold a Unix-domain socket.

    `sockaddr_un` caps a socket path near 108 bytes. On macOS `tmp_path` lands
    under `/private/var/folders/<hash>/T/pytest-of-<user>/pytest-<n>/<test-name>/`,
    which spends most of that budget before the runtime appends its own
    `lean-v<protocol>-<install>.sock`, so shared-runtime tests failed on the
    path rather than on the behaviour they assert. The limit is real and the
    production code handles it by keeping its own paths short, so the tests
    move instead of the check.

    Mode 0700 because the runtime refuses a directory any other user can read.
    """
    directory = Path(tempfile.mkdtemp(prefix="af-", dir="/tmp"))
    directory.chmod(0o700)
    try:
        yield directory
    finally:
        _stop_test_runtimes(directory)
        shutil.rmtree(directory, ignore_errors=True)
