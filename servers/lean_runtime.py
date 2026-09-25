"""Persistent, node-local owner of Autoform's Lean REPL and LSP processes.

This is an internal runtime rather than a third MCP server.  The public
``autoform-repl`` and ``autoform-lsp`` stdio servers proxy their four tools to
this process through a private Unix-domain socket.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import json
import logging
import math
import os
import shlex
import signal
import socketserver
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Generic, TypeVar

from servers import (
    ProjectFingerprint,
    is_initial_manifest_materialization,
    lean_project_fingerprint,
    resolve_lean_file,
    resolve_lean_project_dir,
)
from servers.lean_client import (
    BUILD_GENERATION,
    INSTALL_ID,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    DEFAULT_RESPONSE_TIMEOUT,
    LeanRuntimeClient,
    LeanRuntimeError,
    LeanRuntimeUnavailable,
    RuntimePaths,
    default_runtime_paths,
    runtime_paths_for_socket,
)
from servers.lsp.server import (
    DEFAULT_LSP_TIMEOUT,
    LspConfig,
    LspBusyError,
    LeanLspSession,
    LspProtocolError,
    format_lsp_diagnostics,
)
from servers.repl.core import format_repl_response
from servers.repl.pool import (
    DEFAULT_POOL_CLEANUP_SECONDS,
    DEFAULT_RAM_FRACTION,
    LeanReplPool,
    LeanReplPoolConfig,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_PROJECTS = 4
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_MAX_LSP_REQUEST_SECONDS = 600.0
DEFAULT_REPL_REQUEST_TIMEOUT = 180.0
DEFAULT_MAX_REPL_REQUEST_SECONDS = 240.0
DEFAULT_RPC_READ_TIMEOUT = 10.0
DEFAULT_MAX_CONNECTIONS = 64
RUNTIME_SAFETY_SECONDS = 30.0
TERMINAL_CLEANUP_RETRY_SECONDS = 0.05
MAX_TERMINAL_CLEANUP_RETRY_SECONDS = 1.0
# Conservative cleanup headroom after the end-to-end LSP work deadline.
LSP_CLOSE_BUDGET = 65.0


class ProjectResourceBusyError(TimeoutError):
    """A shared project slot could not be admitted within the RPC budget."""


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    raw = str(default) if raw is None or not raw.strip() else raw
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    raw = str(default) if raw is None or not raw.strip() else raw
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, got {raw!r}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number, got {value}")
    return value


def _positive_float(name: str, default: float) -> float:
    value = _nonnegative_float(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _default_total_repl_workers() -> int:
    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024**3)
        return max(1, int(total_gb * DEFAULT_RAM_FRACTION / 16))
    except ImportError:  # pragma: no cover - psutil is a runtime dependency
        return 1


@dataclass(frozen=True)
class LeanRuntimeConfig:
    """Per-installation limits; the first runtime starter owns them until stop."""

    max_projects: int
    idle_seconds: float
    total_repl_workers: int
    repl_workers_per_project: int
    repl_project_limit: int
    repl_command: tuple[str, ...]
    lsp_command: tuple[str, ...]
    lsp_timeout: float
    max_lsp_request_seconds: float
    repl_request_timeout: float
    max_repl_request_seconds: float
    rpc_read_timeout: float
    max_connections: int
    response_timeout: float

    @classmethod
    def from_environment(cls) -> "LeanRuntimeConfig":
        max_projects = _positive_int("AUTOFORM_MAX_LEAN_PROJECTS", DEFAULT_MAX_PROJECTS)
        idle_seconds = _nonnegative_float("AUTOFORM_LEAN_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)
        total_workers = _positive_int(
            "AUTOFORM_REPL_TOTAL_WORKERS",
            _default_total_repl_workers(),
        )
        legacy_raw = os.environ.get("LEAN_NUM_REPLS", "0") or "0"
        try:
            legacy_workers = int(legacy_raw)
        except ValueError as error:
            raise ValueError(
                f"LEAN_NUM_REPLS must be a nonnegative integer, got {legacy_raw!r}"
            ) from error
        if legacy_workers < 0:
            raise ValueError(
                f"LEAN_NUM_REPLS must be a nonnegative integer, got {legacy_workers}"
            )
        workers_per_project = _positive_int(
            "AUTOFORM_REPL_WORKERS_PER_PROJECT",
            legacy_workers or 1,
        )
        if workers_per_project > total_workers:
            raise ValueError(
                "AUTOFORM_REPL_WORKERS_PER_PROJECT cannot exceed "
                "AUTOFORM_REPL_TOTAL_WORKERS"
            )
        repl_project_limit = min(max_projects, total_workers // workers_per_project)
        repl_command = tuple(shlex.split(os.environ.get("LEAN_REPL_CMD", "lake exe repl")))
        lsp_command = tuple(shlex.split(os.environ.get("LEAN_LSP_CMD", "lake serve")))
        if not repl_command:
            raise ValueError("LEAN_REPL_CMD must not be empty")
        if not lsp_command:
            raise ValueError("LEAN_LSP_CMD must not be empty")
        repl_request_timeout = _positive_float(
            "AUTOFORM_REPL_REQUEST_TIMEOUT",
            DEFAULT_REPL_REQUEST_TIMEOUT,
        )
        max_repl_request_seconds = _positive_float(
            "AUTOFORM_MAX_REPL_REQUEST_SECONDS",
            DEFAULT_MAX_REPL_REQUEST_SECONDS,
        )
        if repl_request_timeout > max_repl_request_seconds:
            raise ValueError(
                "AUTOFORM_REPL_REQUEST_TIMEOUT cannot exceed "
                "AUTOFORM_MAX_REPL_REQUEST_SECONDS"
            )
        lsp_timeout = _positive_float("LEAN_LSP_TIMEOUT", DEFAULT_LSP_TIMEOUT)
        max_lsp_request_seconds = _positive_float(
            "AUTOFORM_MAX_LSP_REQUEST_SECONDS",
            DEFAULT_MAX_LSP_REQUEST_SECONDS,
        )
        if lsp_timeout > max_lsp_request_seconds:
            raise ValueError(
                "LEAN_LSP_TIMEOUT cannot exceed AUTOFORM_MAX_LSP_REQUEST_SECONDS"
            )
        response_timeout = _positive_float(
            "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT",
            DEFAULT_RESPONSE_TIMEOUT,
        )
        if (
            max_repl_request_seconds
            + DEFAULT_POOL_CLEANUP_SECONDS
            + RUNTIME_SAFETY_SECONDS
            >= response_timeout
        ):
            raise ValueError(
                "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT is too small for the configured "
                "REPL request and cleanup limits"
            )
        if (
            LSP_CLOSE_BUDGET
            + max_lsp_request_seconds
            + RUNTIME_SAFETY_SECONDS
            > response_timeout
        ):
            raise ValueError(
                "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT is too small for "
                "AUTOFORM_MAX_LSP_REQUEST_SECONDS"
            )
        return cls(
            max_projects=max_projects,
            idle_seconds=idle_seconds,
            total_repl_workers=total_workers,
            repl_workers_per_project=workers_per_project,
            repl_project_limit=max(1, repl_project_limit),
            repl_command=repl_command,
            lsp_command=lsp_command,
            lsp_timeout=lsp_timeout,
            max_lsp_request_seconds=max_lsp_request_seconds,
            repl_request_timeout=repl_request_timeout,
            max_repl_request_seconds=max_repl_request_seconds,
            rpc_read_timeout=_positive_float(
                "AUTOFORM_RUNTIME_READ_TIMEOUT",
                DEFAULT_RPC_READ_TIMEOUT,
            ),
            max_connections=_positive_int(
                "AUTOFORM_RUNTIME_MAX_CONNECTIONS",
                DEFAULT_MAX_CONNECTIONS,
            ),
            response_timeout=response_timeout,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_projects": self.max_projects,
            "idle_seconds": self.idle_seconds,
            "total_repl_workers": self.total_repl_workers,
            "repl_workers_per_project": self.repl_workers_per_project,
            "repl_project_limit": self.repl_project_limit,
            "repl_command": list(self.repl_command),
            "lsp_command": list(self.lsp_command),
            "lsp_timeout": self.lsp_timeout,
            "max_lsp_request_seconds": self.max_lsp_request_seconds,
            "repl_request_timeout": self.repl_request_timeout,
            "max_repl_request_seconds": self.max_repl_request_seconds,
            "rpc_read_timeout": self.rpc_read_timeout,
            "max_connections": self.max_connections,
            "response_timeout": self.response_timeout,
        }


@dataclass
class _CacheEntry(Generic[T]):
    resource: T
    fingerprint: ProjectFingerprint
    last_used: float
    active: set[object] = field(default_factory=set)
    invalid: bool = False


@dataclass
class _CacheValidation(Generic[T]):
    resource: T
    reservation: object
    future: Future[tuple[bool, BaseException | None]]
    waiters: set[object] = field(default_factory=set)
    started: bool = False
    abandoned: bool = False
    error_logged: bool = False


class ProjectResourceCache(Generic[T]):
    """Bounded project cache with active leases and idle/LRU eviction.

    ``deadline_factory`` receives the lease's absolute monotonic deadline.  It
    lets a resource's own startup protocol share the cache admission deadline
    without breaking existing one-argument factories.

    ``accept_startup_fingerprint`` permits a narrow, caller-defined transition
    caused by startup itself. The accepted fingerprint becomes the resource's
    generation; later validation remains strict.
    """

    def __init__(
        self,
        factory: Callable[[Path], T],
        close_resource: Callable[[T], None],
        *,
        max_entries: int,
        idle_seconds: float,
        is_valid: Callable[[T], bool] | None = None,
        deadline_factory: Callable[[Path, float], T] | None = None,
        accept_startup_fingerprint: (
            Callable[[ProjectFingerprint, ProjectFingerprint], bool] | None
        ) = None,
        start_sweeper: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._factory = factory
        self._close_resource = close_resource
        self._max_entries = max_entries
        self._idle_seconds = idle_seconds
        self._is_valid = is_valid
        self._deadline_factory = deadline_factory
        self._accept_startup_fingerprint = accept_startup_fingerprint
        self._clock = clock
        self._entries: dict[Path, _CacheEntry[T]] = {}
        self._retiring: dict[Path, T] = {}
        self._retiring_active: dict[Path, object] = {}
        self._creating: set[Path] = set()
        self._validating: dict[Path, _CacheValidation[T]] = {}
        self._condition = threading.Condition()
        self._closed = False
        self._stop_sweeper = threading.Event()
        self._sweeper: threading.Thread | None = None
        if start_sweeper and idle_seconds > 0:
            interval = min(60.0, max(1.0, idle_seconds / 2))
            self._sweeper = threading.Thread(
                target=self._sweep,
                args=(interval,),
                name="autoform-project-eviction",
                daemon=True,
            )
            self._sweeper.start()

    @contextmanager
    def lease(
        self,
        project_dir: str,
        *,
        create: bool = True,
        acquisition_timeout: float | None = None,
        deadline: float | None = None,
        creation_budget: float = 0.0,
        required_fingerprint: ProjectFingerprint | None = None,
    ) -> Iterator[T | None]:
        """Keep a project resource alive for the complete operation."""
        if deadline is not None and acquisition_timeout is not None:
            raise TypeError("pass acquisition_timeout or deadline, not both")
        root = resolve_lean_project_dir(project_dir)
        effective_deadline = deadline
        if effective_deadline is None and acquisition_timeout is not None:
            effective_deadline = self._clock() + acquisition_timeout
        lease_token = object()
        resource: T | None = None
        operation_error: BaseException | None = None
        try:
            resource = self._acquire(
                root,
                lease_token=lease_token,
                create=create,
                acquisition_timeout=acquisition_timeout,
                deadline=effective_deadline,
                creation_budget=creation_budget,
                required_fingerprint=required_fingerprint,
            )
            yield resource
        except BaseException as error:
            operation_error = error
        try:
            self._release(root, lease_token, deadline=effective_deadline)
        except BaseException as cleanup_error:
            if operation_error is None:
                raise
            note = f"Lean project resource release also failed: {cleanup_error}"
            add_note = getattr(operation_error, "add_note", None)
            if add_note is not None:
                add_note(note)
            else:  # pragma: no cover - Python 3.10 compatibility
                logger.error("%s", note)
        if operation_error is not None:
            raise operation_error.with_traceback(operation_error.__traceback__)

    def stats(self) -> dict[str, Any]:
        with self._condition:
            now = self._clock()
            entries = [
                (
                    root,
                    len(entry.active),
                    entry.invalid,
                    entry.last_used,
                )
                for root, entry in sorted(
                    self._entries.items(), key=lambda item: str(item[0])
                )
            ]
            retiring = sorted(str(root) for root in self._retiring)
            creating = sorted(str(root) for root in self._creating)
        resident = []
        for root, active, invalid, last_used in entries:
            resident.append(
                {
                    "project_dir": str(root),
                    "active": active,
                    "valid": not invalid,
                    "idle_seconds": round(max(0.0, now - last_used), 3),
                }
            )
        return {
            "limit": self._max_entries,
            "resident": resident,
            "retiring": retiring,
            "creating": creating,
        }

    @contextmanager
    def inspect(self, project_dir: str) -> Iterator[T | None]:
        """Borrow existing state without validating, replacing, or creating it."""
        root = resolve_lean_project_dir(project_dir)
        lease_token = object()
        resource: T | None = None
        retirement: tuple[Path, T] | None = None
        try:
            with self._condition:
                if self._closed:
                    raise RuntimeError("project resource cache is closed")
                entry = self._entries.get(root)
                resource = None if entry is None else entry.resource
                if entry is not None:
                    entry.active.add(lease_token)
            yield resource
        finally:
            if resource is not None:
                with self._condition:
                    entry = self._entries.get(root)
                    if (
                        entry is not None
                        and entry.resource is resource
                        and lease_token in entry.active
                    ):
                        entry.active.remove(lease_token)
                        entry.last_used = self._clock()
                        if not entry.active and entry.invalid:
                            self._entries.pop(root)
                            self._retiring[root] = resource
                            retirement = (root, resource)
                        self._condition.notify_all()
                if retirement is not None:
                    self._ensure_retirement_started(*retirement)

    def state(self, project_dir: str) -> str:
        """Return the current project-resource lifecycle state."""
        root = resolve_lean_project_dir(project_dir)
        with self._condition:
            entry = self._entries.get(root)
            if entry is not None:
                return "retiring" if entry.invalid else "warm"
            if root in self._retiring:
                return "retiring"
            if root in self._creating:
                return "warming"
            return "cold"

    def invalidate(self, project_dir: str, resource: T) -> None:
        """Arrange to replace a failed resource after its active calls finish."""
        root = resolve_lean_project_dir(project_dir)
        with self._condition:
            entry = self._entries.get(root)
            if entry is not None and entry.resource is resource:
                entry.invalid = True
                self._condition.notify_all()

    def leased_fingerprint(self, root: Path, resource: T) -> ProjectFingerprint:
        """Return the exact project generation attached to an active lease."""
        with self._condition:
            entry = self._entries.get(root)
            if (
                entry is None
                or entry.resource is not resource
                or entry.invalid
                or not entry.active
            ):
                raise ProjectResourceBusyError(
                    f"shared Lean project lease became unavailable: {root}"
                )
            return entry.fingerprint

    def evict_idle(self) -> int:
        """Close every inactive entry older than the configured TTL."""
        if self._idle_seconds <= 0:
            return 0
        with self._condition:
            if self._closed:
                return 0
            now = self._clock()
            victims = [
                root
                for root, entry in self._entries.items()
                if not entry.active and now - entry.last_used >= self._idle_seconds
            ]
            for root in victims:
                self._retiring[root] = self._entries.pop(root).resource
            retiring = [
                (root, resource)
                for root, resource in self._retiring.items()
                if root not in self._retiring_active
            ]
            if victims:
                self._condition.notify_all()
        for root, resource in retiring:
            self._retire_until_deadline(root, resource, deadline=None)
        return len(victims)

    def close(self) -> None:
        """Stop admission, wait for active leases, then close all resources."""
        self._stop_sweeper.set()
        with self._condition:
            self._closed = True
            while (
                self._creating
                or self._retiring_active
                or any(entry.active for entry in self._entries.values())
            ):
                self._condition.wait(timeout=0.5)
            for root, entry in self._entries.items():
                self._retiring[root] = entry.resource
            self._entries.clear()
            retiring = list(self._retiring.items())
            self._condition.notify_all()
        first_error: BaseException | None = None
        try:
            for root, resource in retiring:
                try:
                    self._retire_until_deadline(root, resource, deadline=None)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
        finally:
            if self._sweeper and self._sweeper is not threading.current_thread():
                self._sweeper.join()
        if first_error is not None:
            raise first_error.with_traceback(first_error.__traceback__)
        with self._condition:
            failed = len(self._retiring)
        if failed:
            raise RuntimeError(
                f"failed to retire {failed} Lean project resource(s)"
            )

    def _acquire(
        self,
        root: Path,
        *,
        lease_token: object,
        create: bool,
        acquisition_timeout: float | None,
        deadline: float | None,
        creation_budget: float,
        required_fingerprint: ProjectFingerprint | None,
    ) -> T | None:
        if acquisition_timeout is not None and acquisition_timeout <= 0:
            raise ProjectResourceBusyError(
                "no response budget remains for a shared Lean project slot"
            )
        if creation_budget < 0:
            raise ValueError("creation_budget must be nonnegative")
        if deadline is None and acquisition_timeout is not None:
            deadline = self._clock() + acquisition_timeout
        while True:
            if deadline is not None and self._clock() >= deadline:
                raise ProjectResourceBusyError(
                    "timed out waiting for a shared Lean project slot because the "
                    f"response budget expired: {root}"
                )
            try:
                fingerprint = lean_project_fingerprint(root)
            except OSError as error:
                if required_fingerprint is not None:
                    raise ProjectResourceBusyError(
                        f"shared Lean project changed after validation: {root}"
                    ) from error
                raise
            if (
                required_fingerprint is not None
                and fingerprint != required_fingerprint
            ):
                raise ProjectResourceBusyError(
                    f"shared Lean project changed after validation: {root}"
                )
            wait = False
            retirement: tuple[Path, T] | None = None
            candidate: T | None = None
            validation: _CacheValidation[T] | None = None
            validation_waiter: object | None = None
            with self._condition:
                if self._closed:
                    raise RuntimeError("project resource cache is closed")

                if root in self._retiring:
                    if not create:
                        return None
                    if root in self._retiring_active:
                        wait = True
                    else:
                        self._require_creation_budget(
                            root,
                            deadline=deadline,
                            creation_budget=creation_budget,
                        )
                        retirement = (root, self._retiring[root])
                    entry = None
                else:
                    entry = self._entries.get(root)
                    entry_is_stale = (
                        entry is not None
                        and (
                            entry.invalid
                            or entry.fingerprint != fingerprint
                        )
                    )
                    if entry_is_stale:
                        assert entry is not None
                        if entry.active:
                            if not create:
                                return None
                            self._require_creation_budget(
                                root,
                                deadline=deadline,
                                creation_budget=creation_budget,
                            )
                            wait = True
                        else:
                            self._require_creation_budget(
                                root,
                                deadline=deadline,
                                creation_budget=creation_budget,
                            )
                            resource = self._entries.pop(root).resource
                            self._retiring[root] = resource
                            retirement = (root, resource)
                            self._condition.notify_all()
                            entry = None

                if retirement is None and not wait and entry is not None:
                    if deadline is not None and self._clock() >= deadline:
                        raise ProjectResourceBusyError(
                            f"timed out waiting for a shared Lean project slot: {root}"
                        )
                    candidate = entry.resource
                    entry.last_used = self._clock()
                    if self._is_valid is None:
                        entry.active.add(lease_token)
                    else:
                        validation_waiter = object()
                        validation = self._validating.get(root)
                        if validation is not None and validation.future.done():
                            if self._validating.get(root) is validation:
                                self._validating.pop(root)
                            validation = None
                        if validation is None:
                            validation = self._reserve_validation(
                                root,
                                entry,
                                validation_waiter,
                            )
                        else:
                            try:
                                validation.waiters.add(validation_waiter)
                            except BaseException:
                                validation.waiters.discard(validation_waiter)
                                raise

                if (
                    candidate is None
                    and retirement is None
                    and not wait
                    and entry is None
                    and not create
                ):
                    return None

                if (
                    candidate is None
                    and retirement is None
                    and not wait
                    and root in self._creating
                ):
                    wait = True

                if candidate is None and retirement is None and not wait:
                    self._require_creation_budget(
                        root,
                        deadline=deadline,
                        creation_budget=creation_budget,
                    )
                    occupied = (
                        len(self._entries)
                        + len(self._creating)
                        + len(self._retiring)
                    )
                    if occupied >= self._max_entries:
                        inactive = [
                            (candidate.last_used, path)
                            for path, candidate in self._entries.items()
                            if not candidate.active
                        ]
                        if inactive:
                            _, victim = min(inactive)
                            resource = self._entries.pop(victim).resource
                            self._retiring[victim] = resource
                            retirement = (victim, resource)
                            self._condition.notify_all()
                        elif any(
                            path not in self._retiring_active
                            for path in self._retiring
                        ):
                            retiring_root = next(
                                path
                                for path in self._retiring
                                if path not in self._retiring_active
                            )
                            retirement = (
                                retiring_root,
                                self._retiring[retiring_root],
                            )
                        else:
                            wait = True

                if candidate is None and retirement is None and not wait:
                    self._creating.add(root)
                    self._condition.notify_all()
                    break

                if candidate is None and retirement is None:
                    wait_seconds = 0.5
                    if deadline is not None:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            raise ProjectResourceBusyError(
                                f"timed out waiting for a shared Lean project slot: {root}"
                            )
                        wait_seconds = min(wait_seconds, remaining)
                    self._condition.wait(timeout=wait_seconds)

            if candidate is not None:
                if validation is not None:
                    assert validation_waiter is not None
                    try:
                        validation_result = self._await_validation(
                            root,
                            validation,
                            validation_waiter,
                            deadline=deadline,
                        )
                        if validation_result is None:
                            raise ProjectResourceBusyError(
                                "shared Lean project validation exceeded its response "
                                f"budget: {root}"
                            )
                        valid, validation_error = validation_result
                        if validation_error is not None:
                            raise validation_error.with_traceback(
                                validation_error.__traceback__
                            )
                        if not valid:
                            if not create:
                                return None
                            continue
                        claimed = self._finish_validation_waiter(
                            root,
                            validation,
                            validation_waiter,
                            lease_token=lease_token,
                            deadline=deadline,
                        )
                        if not claimed:
                            if deadline is not None and self._clock() >= deadline:
                                raise ProjectResourceBusyError(
                                    "shared Lean project validation exceeded its "
                                    f"response budget: {root}"
                                )
                            if not create:
                                return None
                            continue
                    finally:
                        self._finish_validation_waiter(
                            root,
                            validation,
                            validation_waiter,
                        )
                try:
                    current_fingerprint = lean_project_fingerprint(root)
                except OSError as error:
                    self._invalidate_and_release(
                        root,
                        candidate,
                        lease_token,
                        deadline=deadline,
                    )
                    raise ProjectResourceBusyError(
                        f"shared Lean project changed after validation: {root}"
                    ) from error
                if current_fingerprint == fingerprint:
                    if deadline is not None and self._clock() >= deadline:
                        self._release_without_validation(
                            root,
                            candidate,
                            lease_token,
                            deadline=deadline,
                        )
                        raise ProjectResourceBusyError(
                            "shared Lean project validation exceeded its response "
                            f"budget: {root}"
                        )
                    return candidate
                self._invalidate_and_release(
                    root,
                    candidate,
                    lease_token,
                    deadline=deadline,
                )
                if required_fingerprint is not None:
                    raise ProjectResourceBusyError(
                        f"shared Lean project changed after validation: {root}"
                    )
                continue

            if retirement is not None:
                retiring_root, retiring_resource = retirement
                retired = self._retire_until_deadline(
                    retiring_root,
                    retiring_resource,
                    deadline=deadline,
                )
                if retired is None:
                    raise ProjectResourceBusyError(
                        "response budget expired while retiring a displaced Lean "
                        f"project resource: {retiring_root}"
                    )
                if not retired:
                    raise ProjectResourceBusyError(
                        "failed to retire a stale Lean project resource: "
                        f"{retiring_root}"
                    )

        try:
            current_fingerprint = lean_project_fingerprint(root)
            if current_fingerprint != fingerprint:
                raise ProjectResourceBusyError(
                    f"shared Lean project changed before startup: {root}"
                )
            if deadline is not None and self._clock() >= deadline:
                raise ProjectResourceBusyError(
                    "shared Lean project startup exceeded its response budget: "
                    f"{root}"
                )
        except BaseException:
            with self._condition:
                self._creating.discard(root)
                self._condition.notify_all()
            raise

        future: Future[None] = Future()
        factory_outcome: list[tuple[bool, Any]] = []
        start_gate = threading.Event()
        start_decision = {"run": False}

        def log_abandoned_failure(completed: Future[None]) -> None:
            del completed
            succeeded, outcome = factory_outcome[0]
            if not succeeded:
                assert isinstance(outcome, BaseException)
                logger.error(
                    "Lean project resource startup failed after its caller "
                    "stopped waiting",
                    exc_info=(type(outcome), outcome, outcome.__traceback__),
                )

        def create_resource() -> None:
            start_gate.wait()
            if not start_decision["run"]:
                return
            try:
                factory = self._deadline_factory if deadline is not None else None
                resource = (
                    self._factory(root)
                    if factory is None
                    else factory(root, deadline)
                )
            except BaseException as error:
                with self._condition:
                    self._creating.discard(root)
                    self._condition.notify_all()
                factory_outcome.append((False, error))
            else:
                try:
                    disposition, settled_fingerprint = self._settle_created_resource(
                        root,
                        resource,
                        fingerprint=fingerprint,
                        required_fingerprint=required_fingerprint,
                    )
                except BaseException as error:
                    factory_outcome.append((False, error))
                else:
                    factory_outcome.append(
                        (True, (resource, disposition, settled_fingerprint))
                    )
            future.set_result(None)

        creator: threading.Thread | None = None
        try:
            creator = threading.Thread(
                target=create_resource,
                name="autoform-project-startup",
                daemon=False,
            )
            creator.start()
            start_decision["run"] = True
            start_gate.set()
        except BaseException:
            # The worker cannot touch the factory until this thread makes
            # an explicit decision.  This removes Thread.start's ambiguous
            # interruption window: a possibly launched worker observes
            # ``run == False`` and exits without creating a resource.
            start_gate.set()
            if not start_decision["run"]:
                with self._condition:
                    self._creating.discard(root)
                    self._condition.notify_all()
            raise

        try:
            if deadline is None:
                future.result()
            else:
                future.result(timeout=max(0.0, deadline - self._clock()))
        except FutureTimeoutError:
            future.add_done_callback(log_abandoned_failure)
            raise ProjectResourceBusyError(
                "shared Lean project startup exceeded its response "
                f"budget: {root}"
            ) from None
        except BaseException:
            future.add_done_callback(log_abandoned_failure)
            raise
        succeeded, outcome = factory_outcome[0]
        if not succeeded:
            assert isinstance(outcome, BaseException)
            raise outcome.with_traceback(outcome.__traceback__)
        created, disposition, settled_fingerprint = outcome

        if disposition == "closed":
            raise RuntimeError("project resource cache closed during startup")
        if disposition == "changed":
            raise ProjectResourceBusyError(
                f"shared Lean project changed during startup: {root}"
            )
        return self._claim_created_resource(
            root,
            created,
            lease_token=lease_token,
            fingerprint=settled_fingerprint,
            deadline=deadline,
        )

    def _settle_created_resource(
        self,
        root: Path,
        resource: T,
        *,
        fingerprint: ProjectFingerprint,
        required_fingerprint: ProjectFingerprint | None,
    ) -> tuple[str, ProjectFingerprint]:
        """Publish a valid result or retain ownership until it is retired."""
        fingerprint_error: BaseException | None = None
        try:
            current_fingerprint = lean_project_fingerprint(root)
            accepted_fingerprint = fingerprint
            if (
                current_fingerprint != fingerprint
                and self._accept_startup_fingerprint is not None
                and self._accept_startup_fingerprint(
                    fingerprint, current_fingerprint
                )
            ):
                accepted_fingerprint = current_fingerprint
        except OSError:
            current_fingerprint = None
            accepted_fingerprint = fingerprint
        except BaseException as error:
            current_fingerprint = None
            accepted_fingerprint = fingerprint
            fingerprint_error = error
        try:
            with self._condition:
                if self._closed:
                    disposition = "closed"
                elif current_fingerprint != accepted_fingerprint or (
                    required_fingerprint is not None
                    and required_fingerprint != fingerprint
                ):
                    disposition = "changed"
                elif root in self._entries or root in self._retiring:
                    disposition = "closed"
                else:
                    disposition = "published"
                    self._entries[root] = _CacheEntry(
                        resource=resource,
                        fingerprint=accepted_fingerprint,
                        last_used=self._clock(),
                    )
                if disposition != "published":
                    self._retiring[root] = resource
                self._creating.discard(root)
                self._condition.notify_all()
        except BaseException as error:
            try:
                self._retain_created_resource(root, resource)
            except BaseException as cleanup_error:
                self._add_cleanup_note(error, cleanup_error)
            raise error.with_traceback(error.__traceback__)
        if disposition != "published":
            cleanup_error: BaseException | None = None
            try:
                self._ensure_retirement_started(root, resource)
            except BaseException as error:
                cleanup_error = error
            if fingerprint_error is not None:
                if cleanup_error is not None:
                    self._add_cleanup_note(fingerprint_error, cleanup_error)
                raise fingerprint_error.with_traceback(
                    fingerprint_error.__traceback__
                )
            if cleanup_error is not None:
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        return disposition, accepted_fingerprint

    def _retain_created_resource(self, root: Path, resource: T) -> None:
        """Recover ownership when settlement is interrupted mid-publication."""
        start_retirement = False
        with self._condition:
            self._creating.discard(root)
            entry = self._entries.get(root)
            retiring = self._retiring.get(root)
            if entry is not None and entry.resource is resource:
                pass
            elif retiring is resource:
                if root not in self._retiring_active:
                    start_retirement = True
            else:
                if entry is not None or retiring is not None:
                    raise RuntimeError(
                        "created Lean project resource collided with another owner"
                    )
                self._retiring[root] = resource
                start_retirement = True
            self._condition.notify_all()
        if start_retirement:
            self._ensure_retirement_started(root, resource)

    def _claim_created_resource(
        self,
        root: Path,
        resource: T,
        *,
        lease_token: object,
        fingerprint: ProjectFingerprint,
        deadline: float | None,
    ) -> T:
        """Turn an idle published startup into the caller's active lease."""
        try:
            current_fingerprint = lean_project_fingerprint(root)
        except OSError:
            current_fingerprint = None
        if current_fingerprint != fingerprint:
            error = ProjectResourceBusyError(
                f"shared Lean project changed during startup: {root}"
            )
            try:
                self._invalidate_and_retire_if_idle(
                    root,
                    resource,
                    deadline=deadline,
                )
            except BaseException as cleanup_error:
                self._add_cleanup_note(error, cleanup_error)
            raise error

        try:
            with self._condition:
                if self._closed:
                    raise RuntimeError("project resource cache is closed")
                entry = self._entries.get(root)
                if (
                    entry is None
                    or entry.resource is not resource
                    or entry.invalid
                    or entry.fingerprint != fingerprint
                ):
                    raise ProjectResourceBusyError(
                        "created Lean project resource became unavailable before "
                        f"its lease was claimed: {root}"
                    )
                if deadline is not None and self._clock() >= deadline:
                    raise ProjectResourceBusyError(
                        "shared Lean project startup exceeded its response "
                        f"budget: {root}"
                    )
                entry.active.add(lease_token)
                entry.last_used = self._clock()
                return resource
        except BaseException as error:
            with self._condition:
                entry = self._entries.get(root)
                claimed = entry is not None and lease_token in entry.active
            if claimed:
                try:
                    self._release(root, lease_token, deadline=deadline)
                except BaseException as cleanup_error:
                    self._add_cleanup_note(error, cleanup_error)
            raise error.with_traceback(error.__traceback__)

    def _invalidate_and_retire_if_idle(
        self,
        root: Path,
        resource: T,
        *,
        deadline: float | None,
    ) -> None:
        retirement: tuple[Path, T] | None = None
        with self._condition:
            entry = self._entries.get(root)
            if entry is None or entry.resource is not resource:
                return
            entry.invalid = True
            if not entry.active:
                self._entries.pop(root)
                self._retiring[root] = resource
                retirement = (root, resource)
            self._condition.notify_all()
        if retirement is not None:
            self._retire_until_deadline(*retirement, deadline=deadline)

    @staticmethod
    def _add_cleanup_note(error: BaseException, cleanup_error: BaseException) -> None:
        note = f"Lean project resource cleanup also failed: {cleanup_error}"
        add_note = getattr(error, "add_note", None)
        if add_note is not None:
            add_note(note)
        else:  # pragma: no cover - Python 3.10 compatibility
            logger.error("%s", note)

    def _require_creation_budget(
        self,
        root: Path,
        *,
        deadline: float | None,
        creation_budget: float,
    ) -> None:
        if deadline is None:
            return
        if deadline - self._clock() < creation_budget:
            raise ProjectResourceBusyError(
                f"not enough response budget to start a shared Lean project slot: {root}"
            )

    def _reserve_validation(
        self,
        root: Path,
        entry: _CacheEntry[T],
        waiter: object,
        *,
        release_token: object | None = None,
    ) -> _CacheValidation[T]:
        """Reserve an entry for one shared validation while holding the lock."""
        validation = _CacheValidation(
            resource=entry.resource,
            reservation=object(),
            future=Future(),
        )
        try:
            entry.active.add(validation.reservation)
            self._validating[root] = validation
            validation.waiters.add(waiter)
            if release_token is not None:
                entry.active.remove(release_token)
                entry.last_used = self._clock()
            self._condition.notify_all()
            self._start_validation(root, validation)
        except BaseException as error:
            validation.waiters.discard(waiter)
            if release_token is not None and release_token in entry.active:
                entry.active.remove(release_token)
                entry.last_used = self._clock()
            if not validation.started and not validation.future.done():
                self._fail_validation_start(root, validation, error)
            self._condition.notify_all()
            raise
        return validation

    def _settle_validation(
        self,
        root: Path,
        validation: _CacheValidation[T],
        *,
        valid: bool,
    ) -> None:
        retirement: tuple[Path, T] | None = None
        with self._condition:
            entry = self._entries.get(root)
            if (
                self._validating.get(root) is validation
                and entry is not None
                and entry.resource is validation.resource
                and validation.reservation in entry.active
            ):
                if not valid:
                    entry.invalid = True
                entry.active.remove(validation.reservation)
                entry.last_used = self._clock()
                if not entry.active and entry.invalid:
                    self._entries.pop(root)
                    self._retiring[root] = validation.resource
                    retirement = (root, validation.resource)
            self._condition.notify_all()
        if retirement is not None:
            self._ensure_retirement_started(*retirement)

    def _fail_validation_start(
        self,
        root: Path,
        validation: _CacheValidation[T],
        error: BaseException,
    ) -> None:
        with self._condition:
            entry = self._entries.get(root)
            if (
                entry is not None
                and entry.resource is validation.resource
                and validation.reservation in entry.active
            ):
                entry.active.remove(validation.reservation)
                entry.last_used = self._clock()
            self._condition.notify_all()
        if not validation.future.done():
            validation.future.set_exception(error)
        self._drop_completed_validation(root, validation)

    def _quarantine_failed_validation(
        self,
        root: Path,
        validation: _CacheValidation[T],
    ) -> None:
        """Recover ownership if publishing a validator result is interrupted."""
        retirement: tuple[Path, T] | None = None
        with self._condition:
            entry = self._entries.get(root)
            if entry is not None and entry.resource is validation.resource:
                entry.invalid = True
                entry.active.discard(validation.reservation)
                if not entry.active:
                    self._entries.pop(root)
                    self._retiring[root] = validation.resource
                    retirement = (root, validation.resource)
            elif self._retiring.get(root) is validation.resource:
                retirement = (root, validation.resource)
            self._condition.notify_all()
        if retirement is not None:
            self._ensure_retirement_started(*retirement)

    def _drop_completed_validation(
        self,
        root: Path,
        validation: _CacheValidation[T],
    ) -> None:
        with self._condition:
            if (
                self._validating.get(root) is validation
                and validation.future.done()
            ):
                self._validating.pop(root)
            self._condition.notify_all()

    def _start_validation(
        self,
        root: Path,
        validation: _CacheValidation[T],
    ) -> None:
        assert self._is_valid is not None
        start_gate = threading.Event()

        def validate() -> None:
            start_gate.wait()
            if not validation.started:
                return
            validation_error: BaseException | None = None
            try:
                valid = bool(self._is_valid(validation.resource))
            except BaseException as error:
                validation_error = error
                valid = False
            try:
                self._settle_validation(root, validation, valid=valid)
            except BaseException as error:
                try:
                    self._quarantine_failed_validation(root, validation)
                except BaseException as recovery_error:
                    self._add_cleanup_note(error, recovery_error)
                if validation_error is not None:
                    self._add_cleanup_note(validation_error, error)
                    error = validation_error
                if not validation.future.done():
                    validation.future.set_exception(error)
            else:
                validation.future.set_result((valid, validation_error))
            finally:
                self._log_abandoned_validation_error(validation)
                self._drop_completed_validation(root, validation)

        try:
            worker = threading.Thread(
                target=validate,
                name="autoform-project-validation",
                daemon=False,
            )
            worker.start()
            validation.started = True
            start_gate.set()
        except BaseException as error:
            start_gate.set()
            if not validation.started:
                self._fail_validation_start(root, validation, error)
                return
            raise error.with_traceback(error.__traceback__)

    def _finish_validation_waiter(
        self,
        root: Path,
        validation: _CacheValidation[T],
        waiter: object,
        *,
        lease_token: object | None = None,
        deadline: float | None = None,
    ) -> bool:
        claimed = False
        with self._condition:
            entry = self._entries.get(root)
            current_validation = self._validating.get(root)
            if (
                lease_token is not None
                and (deadline is None or self._clock() < deadline)
                and not self._closed
                and entry is not None
                and entry.resource is validation.resource
                and not entry.invalid
                and (
                    current_validation is None
                    or current_validation is validation
                )
            ):
                entry.active.add(lease_token)
                entry.last_used = self._clock()
                claimed = True
            validation.waiters.discard(waiter)
            if (
                self._validating.get(root) is validation
                and validation.future.done()
            ):
                self._validating.pop(root)
            self._condition.notify_all()
        return claimed

    def _mark_validation_abandoned(
        self,
        validation: _CacheValidation[T],
    ) -> None:
        with self._condition:
            validation.abandoned = True

    def _log_abandoned_validation_error(
        self,
        validation: _CacheValidation[T],
    ) -> None:
        validation_error: BaseException | None = None
        with self._condition:
            if (
                not validation.abandoned
                or validation.waiters
                or not validation.future.done()
                or validation.error_logged
            ):
                return
            try:
                _, validation_error = validation.future.result()
            except BaseException as error:
                validation_error = error
            if validation_error is None:
                return
            validation.error_logged = True
        logger.error(
            "Lean project resource validation failed after every caller "
            "stopped waiting",
            exc_info=(
                type(validation_error),
                validation_error,
                validation_error.__traceback__,
            ),
        )

    def _await_validation(
        self,
        root: Path,
        validation: _CacheValidation[T],
        waiter: object,
        *,
        deadline: float | None,
    ) -> tuple[bool, BaseException | None] | None:
        try:
            if deadline is None:
                result = validation.future.result()
            else:
                result = validation.future.result(
                    timeout=max(0.0, deadline - self._clock())
                )
        except FutureTimeoutError:
            if (
                validation.future.done()
                and validation.future.exception() is not None
            ):
                self._finish_validation_waiter(root, validation, waiter)
                raise
            self._mark_validation_abandoned(validation)
            self._finish_validation_waiter(root, validation, waiter)
            self._log_abandoned_validation_error(validation)
            return None
        except BaseException:
            if not validation.future.done() or validation.future.exception() is None:
                self._mark_validation_abandoned(validation)
            self._finish_validation_waiter(root, validation, waiter)
            self._log_abandoned_validation_error(validation)
            raise
        if deadline is not None and self._clock() >= deadline:
            self._mark_validation_abandoned(validation)
            self._finish_validation_waiter(root, validation, waiter)
            self._log_abandoned_validation_error(validation)
            return None
        valid, validation_error = result
        if not valid or validation_error is not None:
            self._finish_validation_waiter(root, validation, waiter)
        return result

    def _release_without_validation(
        self,
        root: Path,
        resource: T,
        lease_token: object,
        *,
        deadline: float | None,
        invalid: bool = False,
    ) -> bool | None:
        retirement: tuple[Path, T] | None = None
        with self._condition:
            entry = self._entries.get(root)
            if (
                entry is None
                or entry.resource is not resource
                or lease_token not in entry.active
            ):
                return True
            if invalid:
                entry.invalid = True
            entry.active.remove(lease_token)
            entry.last_used = self._clock()
            if not entry.active and entry.invalid:
                self._entries.pop(root)
                self._retiring[root] = resource
                retirement = (root, resource)
            self._condition.notify_all()
        if retirement is None:
            return True
        return self._retire_until_deadline(
            *retirement,
            deadline=deadline,
        )

    def _invalidate_and_release(
        self,
        root: Path,
        resource: T,
        lease_token: object,
        *,
        deadline: float | None,
    ) -> bool | None:
        return self._release_without_validation(
            root,
            resource,
            lease_token,
            deadline=deadline,
            invalid=True,
        )

    def _release(
        self,
        root: Path,
        lease_token: object,
        *,
        deadline: float | None = None,
    ) -> None:
        retirement: tuple[Path, T] | None = None
        resource: T | None = None
        validation: _CacheValidation[T] | None = None
        validation_waiter: object | None = None
        with self._condition:
            entry = self._entries.get(root)
            if entry is None or lease_token not in entry.active:
                return
            resource = entry.resource
            if not entry.invalid and self._is_valid is not None:
                validation_waiter = object()
                validation = self._validating.get(root)
                if validation is not None and validation.future.done():
                    if self._validating.get(root) is validation:
                        self._validating.pop(root)
                    validation = None
                if validation is None:
                    validation = self._reserve_validation(
                        root,
                        entry,
                        validation_waiter,
                        release_token=lease_token,
                    )
                else:
                    try:
                        validation.waiters.add(validation_waiter)
                        entry.active.remove(lease_token)
                        entry.last_used = self._clock()
                        self._condition.notify_all()
                    except BaseException:
                        validation.waiters.discard(validation_waiter)
                        if lease_token in entry.active:
                            entry.active.remove(lease_token)
                            entry.last_used = self._clock()
                        self._condition.notify_all()
                        raise
            else:
                entry.active.remove(lease_token)
                entry.last_used = self._clock()
                if not entry.active and entry.invalid:
                    self._entries.pop(root)
                    self._retiring[root] = resource
                    retirement = (root, resource)
                self._condition.notify_all()
        if validation is not None:
            assert validation_waiter is not None
            try:
                validation_result = self._await_validation(
                    root,
                    validation,
                    validation_waiter,
                    deadline=deadline,
                )
                if validation_result is None:
                    return
                valid, validation_error = validation_result
                if not valid:
                    try:
                        self._retire_until_deadline(
                            root,
                            validation.resource,
                            deadline=deadline,
                        )
                    except BaseException as cleanup_error:
                        if validation_error is None:
                            raise
                        self._add_cleanup_note(validation_error, cleanup_error)
                if validation_error is not None:
                    raise validation_error.with_traceback(
                        validation_error.__traceback__
                    )
                return
            finally:
                self._finish_validation_waiter(
                    root,
                    validation,
                    validation_waiter,
                )
        if retirement is not None:
            self._retire_until_deadline(*retirement, deadline=deadline)

    def _sweep(self, interval: float) -> None:
        while not self._stop_sweeper.wait(interval):
            try:
                self.evict_idle()
            except Exception:
                logger.exception("failed to evict idle Lean project resources")

    def _retirement_future(
        self, root: Path, resource: T
    ) -> Future[bool] | None:
        """Claim and start one retirement, or report another current owner."""
        future: Future[bool] = Future()
        start_gate = threading.Event()
        start_decision = {"run": False}
        owner = object()

        def retire() -> None:
            start_gate.wait()
            if not start_decision["run"]:
                return
            try:
                future.set_result(self._retire(root, resource, owner))
            except BaseException as error:
                future.set_exception(error)

        worker: threading.Thread | None = None
        try:
            with self._condition:
                if self._retiring.get(root) is not resource:
                    return None
                if root in self._retiring_active:
                    return None
                self._retiring_active[root] = owner
                self._condition.notify_all()
            worker = threading.Thread(
                target=retire,
                name="autoform-project-retirement",
                daemon=False,
            )
            worker.start()
            start_decision["run"] = True
            start_gate.set()
        except BaseException:
            start_gate.set()
            if not start_decision["run"]:
                with self._condition:
                    if self._retiring_active.get(root) is owner:
                        self._retiring_active.pop(root)
                    self._condition.notify_all()
            raise
        return future

    def _start_retirement(self, root: Path, resource: T) -> None:
        """Retire in the background, leaving failures quarantined for retry."""
        try:
            self._retirement_future(root, resource)
        except Exception:
            logger.exception("failed to start Lean project resource retirement")

    def _ensure_retirement_started(self, root: Path, resource: T) -> None:
        """Claim an inactive quarantine entry and start exactly one closer."""
        self._start_retirement(root, resource)

    def _retire_until_deadline(
        self,
        root: Path,
        resource: T,
        *,
        deadline: float | None,
    ) -> bool | None:
        """Return a retirement result, or ``None`` while it remains owned."""
        waited_for_owner = False
        while True:
            with self._condition:
                if self._retiring.get(root) is not resource:
                    return True
                if root in self._retiring_active:
                    waited_for_owner = True
                    wait_seconds = 0.5
                    if deadline is not None:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            return None
                        wait_seconds = min(wait_seconds, remaining)
                    self._condition.wait(timeout=wait_seconds)
                    continue
                if waited_for_owner:
                    return False
            try:
                future = self._retirement_future(root, resource)
            except Exception:
                logger.exception("failed to start Lean project resource retirement")
                return False
            if future is None:
                waited_for_owner = True
                continue
            try:
                if deadline is None:
                    return future.result()
                return future.result(
                    timeout=max(0.0, deadline - self._clock())
                )
            except FutureTimeoutError:
                if future.done():
                    return future.result()
                return None

    def _retire(self, root: Path, resource: T, owner: object) -> bool:
        """Try one bounded close while retaining failed ownership in quarantine."""
        succeeded = False
        try:
            self._close_resource(resource)
        except Exception:
            logger.exception("failed to close Lean project resource")
            return False
        else:
            succeeded = True
            return True
        finally:
            with self._condition:
                if self._retiring_active.get(root) is owner:
                    self._retiring_active.pop(root)
                if succeeded and self._retiring.get(root) is resource:
                    self._retiring.pop(root)
                self._condition.notify_all()


class LeanRuntimeServices:
    """Runtime dispatch and ownership for all shared Lean subprocesses."""

    def __init__(
        self,
        config: LeanRuntimeConfig | None = None,
        *,
        repl_factory: Callable[[Path], LeanReplPool] | None = None,
        lsp_factory: Callable[[Path], LeanLspSession] | None = None,
        start_sweepers: bool = True,
        lifetime_locks: tuple[Path, ...] = (),
    ) -> None:
        self.config = config or LeanRuntimeConfig.from_environment()
        self.started_at = time.monotonic()

        def default_repl_factory(project_dir: Path) -> LeanReplPool:
            return LeanReplPool(
                LeanReplPoolConfig(
                    cwd=str(project_dir),
                    repl_command=list(self.config.repl_command),
                    num_repls=self.config.repl_workers_per_project,
                    max_retries=0,
                    lifetime_locks=tuple(str(path) for path in lifetime_locks),
                )
            )

        def new_lsp_session(project_dir: Path) -> LeanLspSession:
            return LeanLspSession(
                LspConfig(
                    cwd=str(project_dir),
                    lake_command=list(self.config.lsp_command),
                    timeout=self.config.lsp_timeout,
                    lifetime_locks=tuple(str(path) for path in lifetime_locks),
                )
            )

        def default_lsp_factory(project_dir: Path) -> LeanLspSession:
            session = new_lsp_session(project_dir)
            session.start()
            return session

        def default_lsp_deadline_factory(
            project_dir: Path,
            deadline: float,
        ) -> LeanLspSession:
            session = new_lsp_session(project_dir)
            session.start(deadline=deadline)
            return session

        def close_repl_pool(pool: LeanReplPool) -> None:
            pool.shutdown()

        self.repl_projects = ProjectResourceCache(
            repl_factory or default_repl_factory,
            close_repl_pool,
            max_entries=self.config.repl_project_limit,
            idle_seconds=self.config.idle_seconds,
            is_valid=lambda pool: getattr(pool, "is_usable", lambda: True)(),
            start_sweeper=start_sweepers,
        )
        self.lsp_projects = ProjectResourceCache(
            lsp_factory or default_lsp_factory,
            lambda session: session.close(),
            max_entries=self.config.max_projects,
            idle_seconds=self.config.idle_seconds,
            is_valid=lambda session: session.is_alive(),
            deadline_factory=(
                None if lsp_factory is not None else default_lsp_deadline_factory
            ),
            accept_startup_fingerprint=is_initial_manifest_materialization,
            start_sweeper=start_sweepers,
        )

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "daemon.ping":
            return self.status(include_projects=False)
        if method == "daemon.status":
            return self.status(include_projects=True)
        if method == "repl.run":
            project_dir = self._string_param(params, "project_dir")
            code = self._string_param(params, "code", allow_empty=True)
            timeout = params.get("timeout")
            if timeout is None:
                effective_timeout = self.config.repl_request_timeout
            else:
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or timeout <= 0
                ):
                    raise ValueError("timeout must be a finite positive number or null")
                effective_timeout = float(timeout)
            if effective_timeout > self.config.max_repl_request_seconds:
                raise ValueError(
                    "timeout exceeds the configured runtime limit of "
                    f"{self.config.max_repl_request_seconds:g} seconds"
                )
            deadline = time.monotonic() + effective_timeout
            root = resolve_lean_project_dir(project_dir)
            with self.repl_projects.lease(
                str(root),
                deadline=deadline,
                creation_budget=0,
            ) as pool:
                assert pool is not None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProjectResourceBusyError(
                        f"Lean REPL request budget expired before execution: {root}"
                    )
                return format_repl_response(pool.run(code, timeout=remaining))
        if method == "repl.status":
            project_dir = self._string_param(params, "project_dir")
            with self.repl_projects.inspect(project_dir) as pool:
                state = (
                    (
                        "warm"
                        if getattr(pool, "is_usable", lambda: True)()
                        else "retiring"
                    )
                    if pool is not None
                    else self.repl_projects.state(project_dir)
                )
                return {
                    "state": state,
                    "capacity": (
                        pool.capacity
                        if pool is not None
                        else self.config.repl_workers_per_project
                    ),
                    "memory_usage_gb": (
                        round(pool.get_memory_usage(), 2) if pool is not None else 0.0
                    ),
                    "shutdown": (
                        pool._shutdown if pool is not None else state == "retiring"
                    ),
                    "daemon_pid": os.getpid(),
                    "node_total_workers": self.config.total_repl_workers,
                }
        if method == "lsp.diagnostics":
            deadline = time.monotonic() + self.config.lsp_timeout
            project_dir = self._string_param(params, "project_dir")
            file_path = self._string_param(params, "file_path")
            root, path = resolve_lean_file(project_dir, file_path)
            fingerprint = lean_project_fingerprint(root)
            file_fingerprint = self._lsp_file_fingerprint(path)
            with self.lsp_projects.lease(
                str(root),
                deadline=deadline,
                creation_budget=0,
                required_fingerprint=fingerprint,
            ) as session:
                assert session is not None
                leased_fingerprint = self.lsp_projects.leased_fingerprint(
                    root, session
                )
                try:
                    diagnostics = session.get_diagnostics(
                        str(path),
                        deadline=deadline,
                    )
                    self._require_lsp_fingerprint(
                        root,
                        path,
                        leased_fingerprint,
                        file_fingerprint,
                        deadline=deadline,
                    )
                except LspBusyError:
                    raise
                except (LspProtocolError, TimeoutError, OSError):
                    session.abort()
                    self.lsp_projects.invalidate(str(root), session)
                    raise
            return format_lsp_diagnostics(diagnostics)
        if method == "lsp.hover":
            deadline = time.monotonic() + self.config.lsp_timeout
            project_dir = self._string_param(params, "project_dir")
            file_path = self._string_param(params, "file_path")
            line = self._integer_param(params, "line")
            character = self._integer_param(params, "character")
            if line < 0 or character < 0:
                raise ValueError("line and character must be nonnegative")
            root, path = resolve_lean_file(project_dir, file_path)
            fingerprint = lean_project_fingerprint(root)
            file_fingerprint = self._lsp_file_fingerprint(path)
            with self.lsp_projects.lease(
                str(root),
                deadline=deadline,
                creation_budget=0,
                required_fingerprint=fingerprint,
            ) as session:
                assert session is not None
                leased_fingerprint = self.lsp_projects.leased_fingerprint(
                    root, session
                )
                try:
                    result = session.hover(
                        str(path),
                        line,
                        character,
                        deadline=deadline,
                    )
                    self._require_lsp_fingerprint(
                        root,
                        path,
                        leased_fingerprint,
                        file_fingerprint,
                        deadline=deadline,
                    )
                except LspBusyError:
                    raise
                except (LspProtocolError, TimeoutError, OSError):
                    session.abort()
                    self.lsp_projects.invalidate(str(root), session)
                    raise
            return result or "No hover information at this position."
        raise ValueError(f"unknown Lean runtime method: {method}")

    @staticmethod
    def _require_lsp_fingerprint(
        root: Path,
        path: Path,
        expected_project: ProjectFingerprint,
        expected_file: tuple[int, int, int, int, int, int],
        *,
        deadline: float,
    ) -> None:
        # Leanclient's diagnostics barrier owns imported-module freshness. This
        # fence separately prevents returning a result after the request's
        # project configuration or target file was replaced underneath it.
        if time.monotonic() >= deadline:
            raise ProjectResourceBusyError(
                f"Lean LSP request budget expired before result validation: {root}"
            )
        try:
            current_project = lean_project_fingerprint(root)
            current_file = LeanRuntimeServices._lsp_file_fingerprint(path)
        except OSError as error:
            raise ProjectResourceBusyError(
                f"shared Lean project or target changed during LSP request: {root}"
            ) from error
        if time.monotonic() >= deadline:
            raise ProjectResourceBusyError(
                f"Lean LSP request budget expired during result validation: {root}"
            )
        if current_project != expected_project or current_file != expected_file:
            raise ProjectResourceBusyError(
                f"shared Lean project or target changed during LSP request: {root}"
            )

    @staticmethod
    def _lsp_file_fingerprint(path: Path) -> tuple[int, int, int, int, int, int]:
        info = path.stat()
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def status(self, *, include_projects: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "running": True,
            "pid": os.getpid(),
            "protocol": PROTOCOL_VERSION,
            "install_id": INSTALL_ID,
            "build_generation": BUILD_GENERATION,
            "uptime_seconds": round(time.monotonic() - self.started_at, 3),
            "config": self.config.as_dict(),
        }
        if include_projects:
            result["repl_projects"] = self.repl_projects.stats()
            result["lsp_projects"] = self.lsp_projects.stats()
        return result

    def close(self) -> None:
        repl_error: BaseException | None = None
        try:
            self.repl_projects.close()
        except BaseException as error:
            repl_error = error
        try:
            self.lsp_projects.close()
        except BaseException as lsp_error:
            if repl_error is None:
                raise
            add_note = getattr(repl_error, "add_note", None)
            if add_note is not None:
                add_note(f"Lean LSP cleanup also failed: {lsp_error}")
            else:  # pragma: no cover - Python 3.10 compatibility
                logger.error("Lean LSP cleanup also failed: %s", lsp_error)
        if repl_error is not None:
            raise repl_error.with_traceback(repl_error.__traceback__)

    @staticmethod
    def _string_param(
        params: dict[str, Any],
        name: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = params.get(name)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _integer_param(params: dict[str, Any], name: str) -> int:
        value = params.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        return value


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True


class LeanRuntimeServer(_ThreadingUnixServer):
    """Bounded JSON-lines RPC server on a user-private Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        services: LeanRuntimeServices,
    ) -> None:
        self.socket_path = socket_path
        self.services = services
        self._shutdown_started = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(
            services.config.max_connections
        )
        super().__init__(str(socket_path), LeanRuntimeRequestHandler)
        socket_path.chmod(0o600)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def request_shutdown(self) -> None:
        if self._shutdown_started.is_set():
            return
        self._shutdown_started.set()
        threading.Thread(
            target=self.shutdown,
            name="autoform-runtime-shutdown",
            daemon=True,
        ).start()


class LeanRuntimeRequestHandler(socketserver.StreamRequestHandler):
    """Decode exactly one request and return exactly one response."""

    server: LeanRuntimeServer

    def setup(self) -> None:
        self.request.settimeout(self.server.services.config.rpc_read_timeout)
        super().setup()

    def handle(self) -> None:
        request_id: Any = None
        shutdown = False
        try:
            raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("request is empty, unterminated, or too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            request_id = request.get("id")
            if request.get("v") != PROTOCOL_VERSION:
                raise ValueError(
                    f"protocol mismatch: expected {PROTOCOL_VERSION}, got {request.get('v')!r}"
                )
            method = request.get("method")
            params = request.get("params")
            if not isinstance(method, str) or not method:
                raise ValueError("method must be a non-empty string")
            if not isinstance(params, dict):
                raise ValueError("params must be an object")

            if method == "daemon.shutdown":
                result = {"stopping": True, "pid": os.getpid()}
                shutdown = True
            else:
                result = self.server.services.dispatch(method, params)
            response = {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except Exception as error:
            logger.exception("Lean runtime request failed")
            response = {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }

        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            encoded = json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "type": "ValueError",
                        "message": "response exceeds the message limit",
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        try:
            self.wfile.write(encoded)
            self.wfile.flush()
        except BrokenPipeError:
            logger.warning("Lean runtime client disconnected before receiving its response")
        if shutdown:
            self.server.request_shutdown()


def _close_services_until_clean(services: LeanRuntimeServices) -> None:
    """Keep terminal ownership until every quarantined child is gone."""
    delay = TERMINAL_CLEANUP_RETRY_SECONDS
    while True:
        try:
            services.close()
            return
        except Exception:
            logger.exception(
                "Lean runtime cleanup remains incomplete; retaining ownership"
            )
            time.sleep(delay)
            delay = min(delay * 2, MAX_TERMINAL_CLEANUP_RETRY_SECONDS)


def _configure_logging(log_path: Path | None) -> None:
    handlers: list[logging.Handler] = []
    if log_path is not None:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=2 * 1024 * 1024,
                backupCount=2,
            )
        )
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=os.environ.get("AUTOFORM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def serve(
    paths: RuntimePaths,
    *,
    inherited_bootstrap_fds: tuple[int, ...] = (),
    inherited_lifetime_fds: tuple[int, ...] = (),
) -> None:
    """Run the internal runtime in the foreground until stop or a signal."""
    _configure_logging(paths.log)
    import fcntl

    if bool(inherited_bootstrap_fds) != bool(inherited_lifetime_fds):
        raise LeanRuntimeError(
            "inherited bootstrap and lifetime locks must be provided together"
        )
    inherited_fds = (*inherited_bootstrap_fds, *inherited_lifetime_fds)
    if (
        any(isinstance(descriptor, bool) or descriptor < 3 for descriptor in inherited_fds)
        or len(set(inherited_fds)) != len(inherited_fds)
    ):
        raise LeanRuntimeError("inherited runtime lock descriptors are invalid")

    bootstrap_fds = list(inherited_bootstrap_fds)
    lifetime_fds = list(inherited_lifetime_fds)
    try:
        expected_bootstrap_paths = tuple(
            dict.fromkeys((paths.lock, *paths.compatibility_locks))
        )
        expected_lifetime_paths = tuple(
            dict.fromkeys(
                (paths.lifetime_lock, *paths.compatibility_lifetime_locks)
            )
        )
        if bootstrap_fds and (
            len(bootstrap_fds) != len(expected_bootstrap_paths)
            or len(lifetime_fds) != len(expected_lifetime_paths)
        ):
            raise LeanRuntimeError(
                "inherited runtime locks do not match configured paths"
            )
        for kind, descriptors, paths_to_verify in (
            ("bootstrap", bootstrap_fds, expected_bootstrap_paths),
            ("lifetime", lifetime_fds, expected_lifetime_paths),
        ):
            for descriptor, path in zip(descriptors, paths_to_verify):
                try:
                    descriptor_info = os.fstat(descriptor)
                    path_info = path.stat()
                except OSError as error:
                    raise LeanRuntimeError(
                        f"inherited runtime {kind} lock is unavailable"
                    ) from error
                if (
                    not stat.S_ISREG(descriptor_info.st_mode)
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                ):
                    raise LeanRuntimeError(
                        f"inherited runtime {kind} lock does not match its path"
                    )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError) as error:
                    raise LeanRuntimeError(
                        f"inherited runtime {kind} lock is not exclusively owned"
                    ) from error

        # Claim the complete compatibility set exclusively before publishing
        # this daemon, then retain shared ownership through cleanup. Existing
        # clients still request these locks exclusively, while this daemon and
        # all of its Lean children can hold shared claims concurrently.
        if not lifetime_fds:
            for path in expected_lifetime_paths:
                lifetime_fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                lifetime_fds.append(lifetime_fd)
                fcntl.flock(lifetime_fd, fcntl.LOCK_EX)
        for lifetime_fd in lifetime_fds:
            fcntl.flock(lifetime_fd, fcntl.LOCK_SH)
    except BaseException:
        for bootstrap_fd in reversed(bootstrap_fds):
            os.close(bootstrap_fd)
        bootstrap_fds.clear()
        for lifetime_fd in reversed(lifetime_fds):
            os.close(lifetime_fd)
        lifetime_fds.clear()
        raise
    services: LeanRuntimeServices | None = None
    server: LeanRuntimeServer | None = None
    bound_identity: tuple[int, int] | None = None
    previous_handlers: dict[int, Any] = {}
    try:
        try:
            info = paths.socket.lstat()
        except FileNotFoundError:
            pass
        else:
            kind = "socket" if stat.S_ISSOCK(info.st_mode) else "non-socket"
            raise LeanRuntimeError(
                f"runtime {kind} already exists at {paths.socket}; use start/status/stop"
            )

        services = LeanRuntimeServices(
            lifetime_locks=tuple(
                dict.fromkeys(
                    (paths.lifetime_lock, *paths.compatibility_lifetime_locks)
                )
            )
        )
        server = LeanRuntimeServer(paths.socket, services)
        info = paths.socket.lstat()
        bound_identity = (info.st_dev, info.st_ino)

        def request_shutdown(signum: int, frame: Any) -> None:
            logger.info("received signal %s; stopping Lean runtime", signum)
            assert server is not None
            server.request_shutdown()

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)

        # The spawning client and this daemon inherited the same exclusive
        # bootstrap locks. Release the daemon copies only after the socket and
        # signal handlers are ready. If the client dies during startup, these
        # copies keep every compatible client behind the completed handoff.
        for bootstrap_fd in reversed(bootstrap_fds):
            os.close(bootstrap_fd)
        bootstrap_fds.clear()

        logger.info("Lean runtime %s listening at %s", os.getpid(), paths.socket)
        server.serve_forever(poll_interval=0.25)
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            try:
                if services is not None:
                    _close_services_until_clean(services)
            finally:
                try:
                    info = paths.socket.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if bound_identity == (info.st_dev, info.st_ino):
                        paths.socket.unlink()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
                logger.info("Lean runtime %s stopped", os.getpid())
                for bootstrap_fd in reversed(bootstrap_fds):
                    os.close(bootstrap_fd)
                for lifetime_fd in reversed(lifetime_fds):
                    os.close(lifetime_fd)


def _paths_from_args(
    socket_path: str | None,
    log_path: str | None,
    lifetime_lock_path: str | None = None,
    compatibility_lifetime_lock_paths: list[str] | None = None,
    compatibility_lock_paths: list[str] | None = None,
    lock_path: str | None = None,
) -> RuntimePaths:
    paths = (
        runtime_paths_for_socket(socket_path)
        if socket_path is not None
        else default_runtime_paths()
    )
    if (
        log_path is None
        and lifetime_lock_path is None
        and compatibility_lifetime_lock_paths is None
        and compatibility_lock_paths is None
        and lock_path is None
    ):
        return paths
    log = paths.log if log_path is None else Path(log_path).expanduser()
    lock = paths.lock if lock_path is None else Path(lock_path).expanduser()
    lifetime_lock = (
        paths.lifetime_lock
        if lifetime_lock_path is None
        else Path(lifetime_lock_path).expanduser()
    )
    if not log.is_absolute():
        raise LeanRuntimeError("Lean runtime log path must be absolute")
    if not lock.is_absolute():
        raise LeanRuntimeError("Lean runtime lock path must be absolute")
    if lock.parent != paths.directory:
        raise LeanRuntimeError(
            "Lean runtime lock must stay in the runtime directory"
        )
    if not lifetime_lock.is_absolute():
        raise LeanRuntimeError("Lean runtime lifetime lock path must be absolute")
    compatibility_lifetime_locks = (
        paths.compatibility_lifetime_locks
        if compatibility_lifetime_lock_paths is None
        else tuple(
            Path(path).expanduser() for path in compatibility_lifetime_lock_paths
        )
    )
    for path in compatibility_lifetime_locks:
        if not path.is_absolute():
            raise LeanRuntimeError(
                "Lean runtime compatibility lifetime lock path must be absolute"
            )
        if path.parent != paths.directory:
            raise LeanRuntimeError(
                "Lean runtime compatibility lifetime locks must stay in the "
                "runtime directory"
            )
    compatibility_locks = (
        paths.compatibility_locks
        if compatibility_lock_paths is None
        else tuple(Path(path).expanduser() for path in compatibility_lock_paths)
    )
    for path in compatibility_locks:
        if not path.is_absolute():
            raise LeanRuntimeError(
                "Lean runtime compatibility locks must be absolute"
            )
        if path.parent != paths.directory:
            raise LeanRuntimeError(
                "Lean runtime compatibility locks must stay in the runtime directory"
            )
    return RuntimePaths(
        directory=paths.directory,
        socket=paths.socket,
        lock=lock,
        lifetime_lock=lifetime_lock,
        log=log,
        compatibility_locks=compatibility_locks,
        compatibility_lifetime_locks=compatibility_lifetime_locks,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", help="override the Unix socket path")
    parser.add_argument("--log", help="override the rotating log path")
    parser.add_argument("--lock", help="override the runtime startup lock")
    parser.add_argument("--lifetime-lock", help="override the runtime lifetime lock")
    parser.add_argument(
        "--compatibility-lock",
        action="append",
        help="additional startup lock held for an older Autoform client",
    )
    parser.add_argument(
        "--compatibility-lifetime-lock",
        action="append",
        help="additional lifetime lock held for an older Autoform client",
    )
    parser.add_argument(
        "--inherited-bootstrap-lock-fd",
        action="append",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--inherited-lifetime-lock-fd",
        action="append",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "command",
        choices=("serve", "start", "status", "stop"),
        nargs="?",
        default="status",
    )
    args = parser.parse_args(argv)
    paths = _paths_from_args(
        socket_path=args.socket,
        log_path=args.log,
        lifetime_lock_path=args.lifetime_lock,
        compatibility_lifetime_lock_paths=args.compatibility_lifetime_lock,
        compatibility_lock_paths=args.compatibility_lock,
        lock_path=args.lock,
    )

    if args.command == "serve":
        serve(
            paths,
            inherited_bootstrap_fds=tuple(args.inherited_bootstrap_lock_fd or ()),
            inherited_lifetime_fds=tuple(args.inherited_lifetime_lock_fd or ()),
        )
        return

    # Preserve default-path semantics so start/stop also discover and replace
    # another code generation from this same Autoform installation.
    client = LeanRuntimeClient(socket_path=args.socket)
    client.paths = paths
    if args.command == "start":
        print(json.dumps(client.ensure_running(), indent=2, sort_keys=True))
        return
    if args.command == "status":
        try:
            result = client.request("daemon.status", autostart=False)
        except LeanRuntimeUnavailable:
            print(json.dumps({"running": False, "socket": str(paths.socket)}, indent=2))
            raise SystemExit(1)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.command == "stop":
        try:
            result = client.stop()
        except LeanRuntimeUnavailable:
            result = {"stopping": False, "running": False}
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
