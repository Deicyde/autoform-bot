"""Fence one Lean process group against its runtime daemon's lifetime.

The launched command remains the process-group leader so existing REPL and
``leanclient`` cleanup can signal it directly.  A watchdog in a separate
session holds the same shared lifetime lock, notices if the runtime daemon is
killed, and does not release ownership until the Lean process group is gone.
"""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import sys
import time
from pathlib import Path
from typing import NoReturn

import psutil

WATCHDOG_POLL_SECONDS = 0.1
WATCHDOG_START_SECONDS = 5.0


class ProcessSupervisorError(RuntimeError):
    """A supervised Lean process could not establish safe ownership."""


def _group_has_live_members(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        raise

    for candidate in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(candidate.info["pid"]) != process_group_id:
                continue
            if candidate.info["status"] != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, PermissionError, psutil.Error):
            continue
    return False


def _signal_group(process_group_id: int, signal_number: int) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        pass
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise


def _observe_group_until_gone(process_group_id: int) -> None:
    """Retain lifetime ownership without signaling an unanchored group ID."""
    while _group_has_live_members(process_group_id):
        time.sleep(WATCHDOG_POLL_SECONDS)


def _same_process(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _monitor_until_cleanup(
    parent: psutil.Process,
    target: psutil.Process,
    process_group_id: int,
) -> None:
    """Use cheap PID identity checks until full group cleanup is required."""
    while _same_process(target):
        if not _same_process(parent):
            # The original group leader is still identity-verified at this
            # point, so one immediate group kill cannot target a recycled PGID.
            _signal_group(process_group_id, signal.SIGKILL)
            _observe_group_until_gone(process_group_id)
            return
        time.sleep(WATCHDOG_POLL_SECONDS)
    # Once the leader identity is gone, never signal the numeric PGID again.
    # A replacement remains fenced until every residual member disappears.
    _observe_group_until_gone(process_group_id)


def _watch_parent(
    parent: psutil.Process,
    process_group_id: int,
    ready_fd: int,
) -> NoReturn:
    """Run outside the Lean group and clean it if the daemon disappears."""
    try:
        os.setsid()
        target = psutil.Process(process_group_id)
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        try:
            for descriptor in (0, 1, 2):
                os.dup2(devnull_fd, descriptor)
        finally:
            if devnull_fd > 2:
                os.close(devnull_fd)
        os.write(ready_fd, b"1")
        os.close(ready_fd)
        _monitor_until_cleanup(parent, target, process_group_id)
    except BaseException:
        # The watchdog must fail closed. Once identity checks have failed, do
        # not risk signaling a recycled process-group ID.
        try:
            _observe_group_until_gone(process_group_id)
        except BaseException:
            while _group_has_live_members(process_group_id):
                time.sleep(1.0)
    os._exit(0)


def _start_watchdog(parent: psutil.Process, process_group_id: int) -> None:
    ready_read, ready_write = os.pipe()
    watcher_pid = os.fork()
    if watcher_pid == 0:
        os.close(ready_read)
        _watch_parent(parent, process_group_id, ready_write)

    os.close(ready_write)
    try:
        readable, _, _ = select.select(
            [ready_read],
            [],
            [],
            WATCHDOG_START_SECONDS,
        )
        ready = os.read(ready_read, 1) if readable else b""
    finally:
        os.close(ready_read)
    if ready == b"1":
        return
    try:
        os.kill(watcher_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(watcher_pid, 0)
    except ChildProcessError:
        pass
    raise ProcessSupervisorError("Lean process watchdog failed to start")


def supervise_process(
    command: list[str],
    *,
    parent_pid: int,
    lifetime_locks: tuple[Path, ...],
    identity_path: Path | None = None,
    environment: dict[str, str] | None = None,
) -> NoReturn:
    """Acquire child ownership, start a watchdog, then exec ``command``."""
    if os.name != "posix":
        raise ProcessSupervisorError("Lean process supervision requires POSIX")
    if not command:
        raise ProcessSupervisorError("Lean process command must not be empty")
    if parent_pid <= 0 or os.getppid() != parent_pid:
        raise ProcessSupervisorError("Lean runtime parent exited before child startup")
    if os.getpgrp() != os.getpid():
        raise ProcessSupervisorError("Lean process supervisor must be started in its own process group")
    if not lifetime_locks:
        raise ProcessSupervisorError("Lean lifetime lock set must not be empty")
    if any(not path.is_absolute() for path in lifetime_locks):
        raise ProcessSupervisorError("Lean lifetime locks must be absolute")

    try:
        parent = psutil.Process(parent_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
        raise ProcessSupervisorError("Lean runtime parent exited before child startup") from error

    import fcntl

    lifetime_fds: list[int] = []
    try:
        for path in dict.fromkeys(lifetime_locks):
            lifetime_fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            lifetime_fds.append(lifetime_fd)
            while True:
                if os.getppid() != parent_pid or not _same_process(parent):
                    raise ProcessSupervisorError("Lean runtime parent exited before child startup")
                try:
                    fcntl.flock(lifetime_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(WATCHDOG_POLL_SECONDS)
                except OSError as error:
                    if error.errno != errno.EACCES:
                        raise
                    time.sleep(WATCHDOG_POLL_SECONDS)
        if os.getppid() != parent_pid or not _same_process(parent):
            raise ProcessSupervisorError("Lean runtime parent exited before child startup")
        _start_watchdog(parent, os.getpid())
        if os.getppid() != parent_pid or not _same_process(parent):
            raise ProcessSupervisorError("Lean runtime parent exited before child startup")
        if identity_path is not None:
            identity_path.write_text(
                json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}),
                encoding="utf-8",
            )
        for lifetime_fd in lifetime_fds:
            os.set_inheritable(lifetime_fd, True)
        if environment is None:
            os.execvp(command[0], command)
        os.execvpe(command[0], command, environment)
    finally:
        for lifetime_fd in reversed(lifetime_fds):
            os.close(lifetime_fd)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = arguments.index("--")
    except ValueError:
        separator = -1
    if separator < 3 or separator == len(arguments) - 1:
        raise SystemExit(
            "usage: python -m servers.process_supervisor "
            "PARENT_PID IDENTITY_FILE|- LIFETIME_LOCK [LIFETIME_LOCK ...] "
            "-- COMMAND [ARG ...]"
        )
    try:
        parent_pid = int(arguments[0])
    except ValueError as error:
        raise SystemExit("PARENT_PID must be an integer") from error
    identity_path = None if arguments[1] == "-" else Path(arguments[1])
    supervise_process(
        arguments[separator + 1 :],
        parent_pid=parent_pid,
        lifetime_locks=tuple(Path(path) for path in arguments[2:separator]),
        identity_path=identity_path,
    )


if __name__ == "__main__":
    main()
